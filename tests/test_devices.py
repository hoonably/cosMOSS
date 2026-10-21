import asyncio
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.config import TranscriptionConfig
from src.devices import DeviceStore, ResolvedDevice, public_device, resolve_device
from src.ssh_devices import SSHError, discover_hosts, parse_gpu_xml, probe_host, ssh_arguments, gpu_warning_token


GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"
GPU_XML = f"""<nvidia_smi_log><gpu>
<product_name>NVIDIA GeForce RTX 3090</product_name><uuid>{GPU_UUID}</uuid>
<fb_memory_usage><total>24576 MiB</total><used>128 MiB</used><free>24448 MiB</free></fb_memory_usage>
<utilization><gpu_util>0 %</gpu_util></utilization><processes/>
</gpu></nvidia_smi_log>"""


class SSHDiscoveryTests(unittest.TestCase):
    def test_includes_cycles_comments_multiple_aliases_and_patterns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "parts").mkdir()
            (root / "config").write_text('Host gpu-a gpu-b *.cluster !excluded\n  HostName example\nInclude "parts/*"\nHost=gpu-c # comment\nMatch exec "touch should-not-run"\n')
            (root / "parts" / "other").write_text('Host gpu-d\nInclude config\nHost -bad "$(touch bad)"\n')
            self.assertEqual(discover_hosts(root / "config"), ["gpu-a", "gpu-b", "gpu-c", "gpu-d"])
            self.assertFalse((root / "should-not-run").exists())

    def test_ssh_is_noninteractive_and_preserves_configured_host_key_policy(self):
        arguments = ssh_arguments("gpu-a")
        for option in ["BatchMode=yes", "ControlPath=none", "ForwardAgent=no"]:
            self.assertIn(option, arguments)
        self.assertFalse(any('StrictHostKeyChecking=' in option or 'UserKnownHostsFile=' in option for option in arguments))
        with self.assertRaises(SSHError):
            ssh_arguments("-oProxyCommand=malicious")

    def test_idle_and_active_gpu_are_distinguished(self):
        gpu = parse_gpu_xml(GPU_XML)[0]
        self.assertTrue(gpu["idle_candidate"])
        self.assertEqual(gpu["memory_free_mib"], 24448)
        busy = GPU_XML.replace("<processes/>", "<processes><process_info><pid>42</pid><type>C</type><process_name>python</process_name><used_memory>2048 MiB</used_memory></process_info></processes>")
        gpu = parse_gpu_xml(busy)[0]
        self.assertFalse(gpu["idle_candidate"])
        self.assertEqual(gpu["processes"][0]["pid"], "42")

    @unittest.skipUnless(shutil.which('ssh'), 'OpenSSH required')
    def test_port_forward_alias_retains_explicit_known_hosts_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'config'
            config.write_text('Host pod\n  HostName 127.0.0.1\n  Port 2222\n  User hoon\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n')
            result = subprocess.run(ssh_arguments('pod') + ['-F', str(config), '-G', 'pod'], capture_output=True, text=True, check=True)
            settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
            self.assertEqual(settings['hostname'], '127.0.0.1')
            self.assertEqual(settings['port'], '2222')
            self.assertEqual(settings['stricthostkeychecking'], 'false')
            self.assertEqual(settings['userknownhostsfile'], '/dev/null')

    def test_unknown_metrics_and_processes_are_never_reported_idle(self):
        for xml in [GPU_XML.replace("0 %", "N/A"), GPU_XML.replace("<processes/>", "<processes>N/A</processes>"),
                    GPU_XML.replace("24448 MiB", "1024 MiB")]:
            with self.subTest(xml=xml):
                self.assertFalse(parse_gpu_xml(xml)[0]["idle_candidate"])

    def test_graphics_only_process_does_not_count_as_compute_job(self):
        xml = GPU_XML.replace("<processes/>", "<processes><process_info><pid>3</pid><type>G</type><process_name>Xorg</process_name></process_info></processes>")
        self.assertTrue(parse_gpu_xml(xml)[0]["idle_candidate"])


class SSHProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_only_connects_to_selected_alias(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (GPU_XML.encode(), b"")
        with patch("src.ssh_devices.discover_hosts", return_value=["gpu-a", "gpu-b"]), \
             patch("src.ssh_devices.asyncio.create_subprocess_exec", return_value=process) as spawn:
            result = await probe_host("gpu-b")
        arguments = spawn.call_args.args
        self.assertEqual(arguments[-2], "gpu-b")
        self.assertEqual(arguments[-1], "LC_ALL=C nvidia-smi -q -x")
        self.assertEqual(result["gpus"][0]["uuid"], GPU_UUID)

    async def test_unknown_alias_never_runs_a_subprocess(self):
        with patch("src.ssh_devices.discover_hosts", return_value=["gpu-a"]), \
             patch("src.ssh_devices.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(SSHError):
                await probe_host("not-configured")
            spawn.assert_not_called()

    async def test_connected_host_without_nvidia_is_not_a_connection_failure(self):
        process = AsyncMock()
        process.returncode = 127
        process.communicate.return_value = (b"", b"nvidia-smi: not found")
        with patch("src.ssh_devices.discover_hosts", return_value=["gpu-a"]), \
             patch("src.ssh_devices.asyncio.create_subprocess_exec", return_value=process):
            result = await probe_host("gpu-a")
        self.assertTrue(result["reachable"])
        self.assertFalse(result["gpu_supported"])


class DeviceStoreTests(unittest.TestCase):
    def test_default_local_device_and_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "devices.sqlite3"
            store = DeviceStore(database)
            self.assertEqual([device["id"] for device in store.list()], ["local"])
            device = store.add_ssh(name="Lab", host="gpu-a", remote_port=18400,
                                   model="moss", backend="cuda")
            self.assertNotIn("gpu_uuid", device)
            self.assertTrue(device["api_key"])
            self.assertNotIn("api_key", public_device(device))
            self.assertEqual(DeviceStore(database).get(device["id"]), device)
            with self.assertRaises(ValueError):
                store.delete("local")
            with self.assertRaises(ValueError):
                store.add_ssh(name="duplicate", host="gpu-a", remote_port=18401,
                              model="moss", backend="cuda")


class DeviceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cosmoss-devices-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        with patch.dict(os.environ, {"MOSS_DATA_DIR": str(root)}):
            from src import main
        self.main = main
        from src.store import NoteStore
        self.store = NoteStore(root / "notes.sqlite3")
        self.devices = DeviceStore(root / "devices.sqlite3")
        for attribute, value in {"STORE": self.store, "DEVICES": self.devices, "UPLOAD_DIR": root / "uploads", "QUEUE": asyncio.Queue()}.items():
            self.enterContext(patch.object(main, attribute, value))
        self.enterContext(patch.object(main, "discover_hosts", return_value=["gpu-a"]))
        from src.remote_workers import RemoteWorkers
        self.workers = RemoteWorkers()
        self.enterContext(patch.object(main, "REMOTE_WORKERS", self.workers))

        async def emulate_worker(device, action):
            self.workers.states[device["id"]] = {"phase": {"prepare": "ready", "stop": "stopped"}.get(action, "installed"), "action": action}
            if action == 'stop':
                self.main.remote_worker_stopped(device['id'])

        self.worker_run = self.enterContext(patch.object(self.workers, "run", new=AsyncMock(side_effect=emulate_worker)))
        self.probe = self.enterContext(patch.object(main, "probe_host", new=AsyncMock(return_value={"reachable": True, "gpus": parse_gpu_xml(GPU_XML)})))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)
        self.spec = {"name": "Lab", "host": "gpu-a", "remote_port": 18400,
                     "model": "moss-model", "backend": "cuda"}

    async def add(self, select_model=True):
        response = await self.client.post("/api/devices", json=self.spec)
        self.assertEqual(response.status_code, 201, response.text)
        await asyncio.sleep(0)
        self.registration_calls = list(self.worker_run.await_args_list)
        self.registration_probe_calls = self.probe.await_count
        self.worker_run.reset_mock()
        self.probe.reset_mock()
        if select_model:
            response = await self.client.post(f"/api/devices/{response.json()['id']}/model/selection", json={'pipeline':'moss-original'})
            self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def test_registration_only_connects_then_model_preparation_is_explicit(self):
        self.assertEqual([device["id"] for device in (await self.client.get("/api/devices")).json()], ["local"])
        added = await self.add(select_model=False)
        self.assertNotIn("gpu_uuid", added)
        self.assertNotIn("gpu_uuid", self.devices.get(added["id"]))
        self.worker_run.assert_not_awaited()
        self.assertEqual(self.registration_calls, [])
        self.assertEqual(self.registration_probe_calls,1)
        self.assertFalse(added['model_selected'])
        self.assertFalse(added['model_prepared'])
        self.assertEqual(added['setup']['phase'],'unselected')
        prepared = await self.client.post(f"/api/devices/{added['id']}/model/prepare", json={"source": "cache"})
        self.assertEqual(prepared.status_code, 202, prepared.text)
        await asyncio.sleep(0)
        self.worker_run.assert_awaited_once()
        self.assertEqual(self.worker_run.call_args.args[1], "install")
        self.probe.assert_not_awaited()
        self.assertNotIn("api_key", added)
        devices = (await self.client.get("/api/devices")).json()
        self.assertEqual(len(devices), 2)
        self.assertNotIn("api_key", json.dumps(devices))

    async def test_model_variants_and_knobs_are_persisted_and_validated(self):
        device=await self.add()
        response=await self.client.post(f"/api/devices/{device['id']}/model/prepare",json={'source':'cache','model_key':'cuda-original','options':{'precision':'fp16','chunk_minutes':10,'bridge_seconds':0,'max_tokens':4096,'attention':'eager','use_cache':False}})
        self.assertEqual(response.status_code,202,response.text)
        await asyncio.sleep(0)
        saved=self.devices.get(device['id'])
        self.assertEqual(saved['options']['precision'],'fp16')
        self.assertEqual(saved['options']['chunk_minutes'],10)
        self.assertFalse(saved['options']['use_cache'])
        self.assertEqual(saved['model'],'OpenMOSS-Team/MOSS-Transcribe-Diarize')
        invalid=await self.client.post(f"/api/devices/{device['id']}/model/prepare",json={'model_key':'mlx-8bit'})
        self.assertEqual(invalid.status_code,400)

        invalid=await self.client.post(f"/api/devices/{device['id']}/model/prepare",json={'options':{'chunk_minutes':91}})
        self.assertEqual(invalid.status_code,422)
        for options in ({'precision':'int4'}, {'precision':'int8'}, {'cuda_kv_bits':8}):
            invalid=await self.client.post(f"/api/devices/{device['id']}/model/prepare",json={'options':options})
            self.assertEqual(invalid.status_code,422)
        invalid=await self.client.post(f"/api/devices/{device['id']}/model/prepare",json={'model_key':'cuda-8bit'})
        self.assertEqual(invalid.status_code,400)

    async def test_fast_selection_is_persistent_and_read_only_until_download(self):
        device=await self.add(select_model=False)
        with patch.object(self.main.SPEECH,'execute',new=AsyncMock(return_value={'phase':'idle'})) as inspect, \
             patch.object(self.main.SPEECH,'install',return_value={'phase':'connecting'}) as install:
            blocked=await self.client.post(f"/api/devices/{device['id']}/speech-setup",json={'pipeline':'qwen-batch'})
            self.assertEqual(blocked.status_code,409)
            for pipeline in ('moss-batch','qwen-batch'):
                response=await self.client.post(f"/api/devices/{device['id']}/model/selection",json={'pipeline':pipeline})
                self.assertEqual(response.status_code,200,response.text)
                self.assertTrue(response.json()['model_selected'])
                install.assert_not_called()
                self.worker_run.assert_not_awaited()
                response=await self.client.post(f"/api/devices/{device['id']}/status")
                self.assertEqual(response.json()['setup']['phase'],'idle')
                self.assertEqual(inspect.call_args.args[2],'inspect')
            saved=self.devices.get(device['id'])
            self.assertEqual(saved['options']['speech_pipeline'],'qwen-batch')
            mismatch=await self.client.post('/api/notes',data={'device_id':device['id'],'speech_pipeline':'moss-original'},files={'file':('a.wav',b'audio')})
            self.assertEqual(mismatch.status_code,409,mismatch.text)
            self.assertFalse(self.store.list())
            response=await self.client.post(f"/api/devices/{device['id']}/speech-setup",json={'pipeline':'qwen-batch'})
            self.assertEqual(response.status_code,202,response.text)
            install.assert_called_once_with('gpu-a','qwen-batch')

    async def test_no_model_selected_rejects_note_without_consuming_upload(self):
        device=await self.add(select_model=False)
        response=await self.client.post('/api/notes',data={'device_id':device['id']},files={'file':('a.wav',b'audio')})
        self.assertEqual(response.status_code,409,response.text)
        self.assertFalse(self.store.list())
        self.worker_run.assert_not_awaited()

    async def test_environment_status_does_not_install_and_manual_install_preserves_model_settings(self):
        device = await self.add()
        runtime = {"ready": False, "uv": {"ready": True}, "venv": {"ready": False}, "packages": {"ready": False}}
        with patch.object(self.workers, "environment", new=AsyncMock(return_value=runtime)):
            response = await self.client.get(f"/api/devices/{device['id']}/environment")
        self.assertEqual(response.json(), runtime)
        self.worker_run.assert_not_awaited()
        saved = self.devices.set_model(device['id'], 'folder', '/models/moss')
        response = await self.client.post(f"/api/devices/{device['id']}/environment")
        self.assertEqual(response.status_code, 202)
        await asyncio.sleep(0)
        self.assertEqual(self.worker_run.call_args.args[1], 'runtime')
        self.assertEqual(self.devices.get(device['id'])['model_path'], saved['model_path'])
        self.probe.assert_not_awaited()
        self.store.list = lambda: [{'device_id': device['id'], 'status': 'processing'}]
        blocked = await self.client.post(f"/api/devices/{device['id']}/environment")
        self.assertEqual(blocked.status_code, 409)

    async def test_remote_model_dialog_defaults_to_recommended_chunk_and_preserves_manual_value(self):
        device = await self.add()
        hardware = {"gpus": [{"name": "RTX PRO 6000", "index": 0, "memory_gib": 96}]}
        with patch.object(self.workers, "options", new=AsyncMock(return_value={"hardware": hardware})):
            response = await self.client.get(f"/api/devices/{device['id']}/model")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["options"]["chunk_minutes"], 90)
            self.devices.configure(device['id'], options={"chunk_minutes": 20})
            response = await self.client.get(f"/api/devices/{device['id']}/model")
            self.assertEqual(response.json()["options"]["chunk_minutes"], 20)

    async def test_new_notes_use_selected_gpu_chunk_recommendation_without_model_setup(self):
        device = await self.add()
        small = parse_gpu_xml(GPU_XML)[0]
        large = {**small, "uuid": "GPU-aaaaaaaa-2222-3333-4444-555555555555", "index": 1,
                 "memory_total_mib": 96 * 1024}
        self.probe.return_value["gpus"] = [small, large]
        for gpu, expected in ((large, 90), (small, 30)):
            response = await self.client.post("/api/notes", data={"device_id": device['id'], "gpu_uuid": gpu['uuid']},
                                              files={"file": ("voice.wav", b"audio", "audio/wav")})
            self.assertEqual(response.status_code, 202, response.text)
            note = self.store.get(response.json()['id'])
            self.assertEqual(note['chunk_seconds'], expected * 60)
            self.assertEqual(json.loads(note['device_snapshot_json'])['options']['chunk_minutes'], expected)
            self.assertNotIn('chunk_minutes', self.devices.get(device['id']).get('options', {}))
        self.devices.configure(device['id'], options={"chunk_minutes": 20})
        for override, expected in (({}, 20), ({"chunk_minutes": "10"}, 10)):
            response = await self.client.post("/api/notes", data={"device_id": device['id'], "gpu_uuid": large['uuid'], **override},
                                              files={"file": ("voice.wav", b"audio", "audio/wav")})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(self.store.get(response.json()['id'])['chunk_seconds'], expected * 60)

    async def test_prepared_note_chunk_override_is_validated_and_does_not_change_device_defaults(self):
        remote = await self.add()
        for index, device in enumerate((self.devices.get('local'), remote)):
            self.devices.configure(device['id'], options={'chunk_minutes':60})
            upload_id = str(index + 1) * 32
            self.store.create_upload({'id':upload_id, 'original_filename':'recording.m4a',
                                     'media_type':'audio/mp4', 'source_path':str(self.store.upload_directory(upload_id) / 'source.m4a'),
                                     'status':'ready', 'duration':3660})
            values = {'device_id':device['id'], 'upload_id':upload_id}
            if device['backend'] == 'cuda':
                values['gpu_uuid'] = self.probe.return_value['gpus'][0]['uuid']
            for invalid in ('0', '91', 'nan', 'inf'):
                response = await self.client.post('/api/notes', data={**values, 'chunk_minutes':invalid})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.store.get_upload(upload_id)['status'], 'ready')
            response = await self.client.post('/api/notes', data={**values, 'chunk_minutes':'30.5'})
            self.assertEqual(response.status_code, 202, response.text)
            note = self.store.get(response.json()['id'])
            self.assertEqual(note['chunk_seconds'], 30.5 * 60)
            self.assertEqual(note['duration'], 3660)
            self.assertEqual(self.devices.get(device['id'])['options']['chunk_minutes'], 60)

    async def test_prepared_note_count_divides_the_recording_exactly_on_local_and_remote(self):
        remote = await self.add()
        for index, device in enumerate((self.devices.get('local'), remote)):
            self.devices.configure(device['id'], options={'chunk_minutes':60})
            for count in (1, 2, 3, 7):
                upload_id = f'{index}{count}' * 16
                self.store.create_upload({'id':upload_id, 'original_filename':'recording.m4a',
                                         'media_type':'audio/mp4', 'source_path':str(self.store.upload_directory(upload_id) / 'source.m4a'),
                                         'status':'ready', 'duration':3660.123})
                values = {'device_id':device['id'], 'upload_id':upload_id, 'chunk_count':str(count)}
                if device['backend'] == 'cuda':
                    values['gpu_uuid'] = self.probe.return_value['gpus'][0]['uuid']
                for invalid in ({'chunk_count':'0'}, {'chunk_count':'1001'}, {'chunk_count':'1.5'}, {'chunk_minutes':'30'}):
                    response = await self.client.post('/api/notes', data={**values, **invalid})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(self.store.get_upload(upload_id)['status'], 'ready')
                response = await self.client.post('/api/notes', data=values)
                self.assertEqual(response.status_code, 202, response.text)
                note = self.store.get(response.json()['id'])
                self.assertAlmostEqual(note['chunk_seconds'], 3660.123 / count)
                self.assertEqual(note['chunk_count'], count)
                self.assertEqual(self.devices.get(device['id'])['options']['chunk_minutes'], 60)

    async def test_count_limits_keep_prepared_recordings_available(self):
        for index, duration, count in ((1, 10801, 2), (2, 3, 4), (3, None, 2)):
            upload_id = str(index) * 32
            self.store.create_upload({'id':upload_id, 'original_filename':'recording.m4a',
                                     'media_type':'audio/mp4', 'source_path':str(self.store.upload_directory(upload_id) / 'source.m4a'),
                                     'status':'ready', 'duration':duration})
            response = await self.client.post('/api/notes', data={'upload_id':upload_id, 'chunk_count':str(count)})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.store.get_upload(upload_id)['status'], 'ready')

    async def test_local_environment_can_be_checked_without_model_preparation(self):
        with patch.object(self.main.LOCAL_MODELS, 'environment', new=AsyncMock(return_value={'ready': True})), \
             patch.object(self.main.LOCAL_MODELS, 'start_environment', return_value={'phase': 'installing_uv'}) as start:
            checked = await self.client.get('/api/devices/local/environment')
            self.assertTrue(checked.json()['ready'])
            start.assert_not_called()
            installed = await self.client.post('/api/devices/local/environment')
            self.assertEqual(installed.status_code, 202)
            start.assert_called_once()

    async def test_opening_remote_model_options_does_not_install_or_probe_gpus(self):
        device = await self.add()
        with patch.object(self.workers, "options", new=AsyncMock(return_value={"phase": "idle", "cache_dir": "/home/tester/.cache/huggingface/hub", "source": "cache"})):
            response = await self.client.get(f"/api/devices/{device['id']}/model")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['source'], 'cache')
        self.worker_run.assert_not_awaited()
        self.probe.assert_not_awaited()

    async def test_model_folder_selection_persists_and_never_selects_a_gpu(self):
        device = await self.add()
        response = await self.client.post(f"/api/devices/{device['id']}/model/prepare", json={"source": "folder", "path": "~/models/moss"})
        self.assertEqual(response.status_code, 202, response.text)
        await asyncio.sleep(0)
        saved = self.devices.get(device['id'])
        self.assertEqual(saved['model_path'], '~/models/moss')
        self.assertEqual(saved['model_source'], 'folder')
        self.assertEqual(self.worker_run.call_args.args[0]['model_source'], 'folder')
        self.probe.assert_not_awaited()
        self.assertNotIn('gpu_uuid', saved)
        invalid = await self.client.post(f"/api/devices/{device['id']}/model/prepare", json={"source": "folder", "path": "relative/path"})
        self.assertEqual(invalid.status_code, 400)
        self.store.list = lambda: [{'device_id': device['id'], 'status': 'processing'}]
        blocked = await self.client.post(f"/api/devices/{device['id']}/model/prepare", json={"source": "cache"})
        self.assertEqual(blocked.status_code, 409)

    async def test_registration_requires_configured_alias_and_rejects_fixed_gpu(self):
        response = await self.client.post("/api/devices", json={**self.spec, "host": "not-in-config"})
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/devices", json={**self.spec, "gpu_uuid": GPU_UUID})
        self.assertEqual(response.status_code, 422)
        self.worker_run.assert_not_awaited()

    async def test_remote_clients_and_dns_rebinding_hosts_cannot_manage_ssh(self):
        for client, hostname in [("10.0.0.2", "localhost"), ("127.0.0.1", "attacker.example")]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app, client=(client, 10000)), base_url=f"http://{hostname}") as outsider:
                self.assertEqual((await outsider.get("/api/ssh/hosts")).status_code, 403)
        self.assertEqual((await self.client.get("/api/devices", headers={"Sec-Fetch-Site": "cross-site"})).status_code, 403)

    async def test_note_pins_device_and_hides_worker_token(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": GPU_UUID}, files={"file": ("voice.wav", b"audio", "audio/wav")})
        self.assertEqual(response.status_code, 202, response.text)
        note = response.json()
        self.assertEqual(note["device_id"], device["id"])
        self.assertEqual(note["device_name"], "Lab · GPU #0")
        self.assertNotIn("device_snapshot_json", note)
        snapshot = json.loads(self.store.get(note["id"])["device_snapshot_json"])
        self.assertEqual(snapshot["gpu_uuid"], GPU_UUID)
        self.assertEqual(snapshot["remote_port"], 18400)
        self.assertNotIn(snapshot["api_key"], response.text)
        exported = await self.client.get(f"/api/notes/{note['id']}/export/json")
        self.assertNotIn(snapshot["api_key"], exported.text)

    async def test_inference_receives_the_pinned_gpu_even_if_registry_changes(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": GPU_UUID}, files={"file": ("voice.wav", b"audio", "audio/wav")})
        note = response.json()
        source = self.store.resolve_path(note["id"], self.store.get(note["id"])["source_path"])
        config = TranscriptionConfig.from_env({"MOSS_TRANSCRIPTION_BACKEND": "remote", "MOSS_TRANSCRIPTION_URL": "http://127.0.0.1:23456/v1"})
        target = ResolvedDevice(config, "moss-model", device["name"], device["id"], GPU_UUID)
        changed = self.devices.get(device["id"])
        changed["remote_port"] = 19999
        with self.devices._connect() as connection:
            connection.execute("UPDATE devices SET config = ? WHERE id = ?", (json.dumps(changed), device["id"]))
        segments = [{"id": 0, "start": 0, "end": 1, "speaker": "S01", "text": "test"}]
        with patch.object(self.main, "resolve_device", new=AsyncMock(return_value=target)) as resolver, \
             patch.object(self.main, "model_health", new=AsyncMock(return_value={"moss": {"ok": True}})), \
             patch.object(self.main, "run_audio_work", new=AsyncMock(side_effect=[None, 1.0, [(source, 0.0)], []])), \
             patch.object(self.main, "transcribe_chunk", new=AsyncMock(return_value=("[0][S01]test[1]", segments))) as inference:
            await self.main.transcribe(note["id"])
        self.assertEqual(self.store.get(note["id"])["status"], "done")
        self.assertEqual(resolver.call_args.args[0]["remote_port"], 18400)
        self.assertEqual(inference.call_args.kwargs["target"].gpu_uuid, GPU_UUID)
        self.assertEqual([call.args[1] for call in self.worker_run.await_args_list], ["prepare", "stop"])
        self.assertEqual(self.worker_run.await_args_list[-2].args[0]["gpu_uuid"], GPU_UUID)

    async def test_model_preparation_has_no_gpu_and_device_can_be_removed(self):
        device = await self.add()
        with patch.object(self.workers, "start", return_value={"phase": "connecting"}) as start:
            response = await self.client.post(f"/api/devices/{device['id']}/worker/install")
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("gpu_uuid", start.call_args.args[0])
        self.assertEqual(start.call_args.args[1], "install")
        self.assertIn("api_key", start.call_args.args[0])
        self.assertEqual((await self.client.delete(f"/api/devices/{device['id']}")).status_code, 204)

    async def test_each_job_requires_its_own_available_gpu(self):
        device = await self.add()
        files = {"file": ("voice.wav", b"audio", "audio/wav")}
        response = await self.client.post("/api/notes", data={"device_id": device["id"]}, files=files)
        self.assertEqual(response.status_code, 400)

        other_uuid = "GPU-aaaaaaaa-2222-3333-4444-555555555555"
        self.probe.return_value["gpus"].append({**parse_gpu_xml(GPU_XML)[0], "uuid": other_uuid, "index": 2})
        for gpu_uuid, index in [(GPU_UUID, 0), (other_uuid, 2)]:
            response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": gpu_uuid}, files=files)
            self.assertEqual(response.status_code, 202, response.text)
            snapshot = json.loads(self.store.get(response.json()["id"])["device_snapshot_json"])
            self.assertEqual((snapshot["gpu_uuid"], snapshot["gpu_index"]), (gpu_uuid, index))
        self.assertNotIn("gpu_uuid", self.devices.get(device["id"]))

    async def test_failed_initial_setup_can_be_removed_without_connecting_or_stopping_a_gpu(self):
        device = await self.add()
        self.workers.states[device['id']] = {'phase': 'error', 'action': 'install', 'error': 'SSH unavailable'}
        with patch.object(self.workers, 'perform', new=AsyncMock(side_effect=SSHError('offline'))) as stop:
            response = await self.client.delete(f"/api/devices/{device['id']}")
        self.assertEqual(response.status_code, 204)
        stop.assert_not_awaited()

    async def test_possible_live_job_must_be_cleaned_before_removal(self):
        device = await self.add()
        self.devices.configure(device['id'], worker_cleanup_pending=True)
        with patch.object(self.workers, 'perform', new=AsyncMock(side_effect=SSHError('offline'))):
            response = await self.client.delete(f"/api/devices/{device['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.devices.get(device['id'])['host'], 'gpu-a')

    async def test_busy_or_missing_gpu_is_rejected_without_consuming_audio(self):
        device = await self.add()
        for gpus in [[], [{**parse_gpu_xml(GPU_XML)[0], "idle_candidate": False}]]:
            self.probe.return_value["gpus"] = gpus
            response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": GPU_UUID}, files={"file": ("voice.wav", b"audio")})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list(), [])

    async def test_busy_gpu_requires_confirmation_for_current_processes_and_only_this_job(self):
        device = await self.add()
        gpu = {**parse_gpu_xml(GPU_XML)[0], 'idle_candidate': False,
               'processes': [{'pid': '42', 'name': 'dummy.py', 'type': 'C', 'memory_mib': 2000}]}
        self.probe.return_value['gpus'] = [gpu]
        data = {'device_id': device['id'], 'gpu_uuid': GPU_UUID}
        files = {'file': ('voice.wav', b'audio')}
        response = await self.client.post('/api/notes', data=data, files=files)
        self.assertEqual(response.status_code, 409)
        detail = response.json()['detail']
        self.assertEqual(detail['code'], 'gpu_confirmation_required')
        self.assertEqual(detail['gpu']['processes'][0]['name'], 'dummy.py')
        token = detail['gpu']['busy_confirmation']
        response = await self.client.post('/api/notes', data={**data, 'gpu_confirmation': '확인'}, files=files)
        self.assertEqual(response.status_code, 409)
        gpu['processes'][0]['pid'] = '43'
        response = await self.client.post('/api/notes', data={**data, 'gpu_confirmation': token}, files=files)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list(), [])
        response = await self.client.post('/api/notes', data={**data, 'gpu_confirmation': gpu_warning_token(gpu)}, files=files)
        self.assertEqual(response.status_code, 202)
        snapshot = json.loads(self.store.get(response.json()['id'])['device_snapshot_json'])
        self.assertTrue(snapshot['allow_busy_gpu'])
        self.assertNotIn('allow_busy_gpu', self.devices.get(device['id']))
        response = await self.client.post('/api/notes', data=data, files=files)
        self.assertEqual(response.status_code, 409, 'Confirmation leaked into a subsequent job')
        self.probe.return_value['gpus'] = []
        response = await self.client.post('/api/notes', data={**data, 'gpu_confirmation': token}, files=files)
        self.assertEqual(response.status_code, 409, 'Confirmation bypassed missing-GPU validation')

    async def test_gpu_inventory_is_scoped_to_registered_device(self):
        device = await self.add()
        response = await self.client.post(f"/api/devices/{device['id']}/gpus")
        self.assertEqual(response.status_code, 200)
        self.probe.assert_awaited_once_with("gpu-a")
        self.assertEqual((await self.client.post("/api/devices/local/gpus")).status_code, 400)
        self.assertEqual((await self.client.post("/api/devices/missing/gpus")).status_code, 404)

    async def test_gpu_is_released_after_transcription_failure(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": GPU_UUID}, files={"file": ("voice.wav", b"audio")})
        with patch.object(self.main, "resolve_device", new=AsyncMock()), \
             patch.object(self.main, "model_health", new=AsyncMock(return_value={"moss": {"ok": True}})), \
             patch.object(self.main, "run_audio_work", new=AsyncMock(side_effect=RuntimeError("bad audio"))):
            await self.main.transcribe(response.json()["id"])
        self.assertEqual(self.store.get(response.json()["id"])["status"], "error")
        self.assertEqual([call.args[1] for call in self.worker_run.await_args_list], ["prepare", "stop"])

    async def test_cancel_during_gpu_loading_stops_that_job_worker(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"], "gpu_uuid": GPU_UUID}, files={"file": ("voice.wav", b"audio")})
        note_id = response.json()["id"]
        loading = asyncio.Event()

        async def block_loading(snapshot, action):
            self.workers.states[snapshot["id"]] = {"phase": "loading" if action == "prepare" else "stopped", "action": action}
            if action == "prepare":
                loading.set()
                await asyncio.Event().wait()

        self.worker_run.side_effect = block_loading
        task = asyncio.create_task(self.main.transcribe(note_id))
        await asyncio.wait_for(loading.wait(), 2)
        self.main.CANCEL_REQUESTED.add(note_id)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            self.main.CANCEL_REQUESTED.discard(note_id)
        self.assertEqual(self.store.get(note_id)["status"], "cancelled")
        self.assertEqual(self.worker_run.await_args_list[-1].args[1], "stop")
        self.assertEqual(self.worker_run.await_args_list[-1].args[0]["gpu_uuid"], GPU_UUID)

    async def test_unknown_device_is_rejected_instead_of_falling_back_local(self):
        response = await self.client.post("/api/notes", data={"device_id": "missing"}, files={"file": ("voice.wav", b"audio", "audio/wav")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list(), [])


class CUDAWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_authentication_and_selected_gpu_headers(self):
        from src import cuda_server as worker
        with patch.object(worker, "API_TOKEN", "private-test-token"), patch.object(worker, "DEVICE_ID", "device-a"), \
             patch.object(worker, "GPU_UUID", GPU_UUID), patch.object(worker, "generate", return_value={"text": "[0][S01]hello[1]"}) as generate:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://localhost") as client:
                self.assertEqual((await client.get("/v1/models")).status_code, 401)
                headers = {"Authorization": "Bearer private-test-token", "X-CosMOSS-Device-ID": "device-a", "X-CosMOSS-GPU-UUID": "GPU-wrong"}
                response = await client.post("/v1/audio/transcriptions", headers=headers, files={"file": ("clip.wav", b"audio")})
                self.assertEqual(response.status_code, 409)
                generate.assert_not_called()
                headers["X-CosMOSS-GPU-UUID"] = GPU_UUID
                response = await client.post("/v1/audio/transcriptions", headers=headers, files={"file": ("clip.wav", b"audio")})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["text"], "[0][S01]hello[1]")
                self.assertFalse(Path(generate.call_args.args[0]).exists())

    async def test_device_resolver_keeps_uuid_and_private_token(self):
        tunnels = AsyncMock()
        tunnels.endpoint.return_value = "http://127.0.0.1:23456/v1"
        device = {"id": "device-a", "kind": "ssh", "host": "gpu-a", "remote_port": 18400,
                  "name": "Lab GPU", "model": "moss", "backend": "cuda", "gpu_uuid": GPU_UUID, "api_key": "private"}
        target = await resolve_device(device, tunnels)
        self.assertEqual(target.gpu_uuid, GPU_UUID)
        self.assertEqual(target.config.headers, {"Authorization": "Bearer private"})
        self.assertFalse(target.config.stream)

    async def test_wrong_gpu_identity_is_not_reported_ready(self):
        from src import main
        target = ResolvedDevice(TranscriptionConfig.from_env({
            "MOSS_TRANSCRIPTION_BACKEND": "remote", "MOSS_TRANSCRIPTION_URL": "http://127.0.0.1:23456/v1",
        }), "moss", "GPU 0", "device-a", GPU_UUID)
        queried = []

        def respond(request):
            queried.append(request.url.path)
            return httpx.Response(200, json={"device_id": "device-a", "gpu_uuid": "GPU-another"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            result = await main.model_health(target)
        self.assertFalse(result["moss"]["ok"])
        self.assertEqual(queried, ["/healthz"])


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        from src import remote_bootstrap as bootstrap
        self.enterContext(patch.object(bootstrap, "ensure_uv", return_value="/tools/uv"))
        self.enterContext(patch.object(bootstrap, "inspect_environment", return_value={
            "ready": True, "uv": {"ready": True}, "venv": {"ready": True}, "packages": {"ready": True}}))

    def spec(self):
        return {"id": "a" * 32, "gpu_uuid": GPU_UUID, "remote_port": 18400,
                "model": "moss", "api_key": "private-test-token"}

    def test_launch_masks_the_physical_gpu_and_binds_loopback(self):
        self._assert_launch(allow_busy=False)

    def test_confirmed_busy_gpu_launches_without_stopping_existing_processes(self):
        self._assert_launch(allow_busy=True)

    def _assert_launch(self, allow_busy):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
        xml = GPU_XML
        if allow_busy:
            device['allow_busy_gpu'] = True
            xml = xml.replace('<processes/>', '<processes><process_info><type>C</type><pid>42</pid><process_name>dummy.py</process_name></process_info></processes>')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".local/share/cosmoss/workers" / device["id"]
            (root / ".venv/bin").mkdir(parents=True)
            (root / ".venv/bin/python").touch()
            (root / "runtime-version").write_text("cuda128-torch280-moss-e3bdd93f-v1")
            worker = MagicMock()
            worker.pid = 123456
            worker.poll.return_value = None
            identity = {"app": "cosMOSS CUDA worker", "device_id": device["id"], "gpu_uuid": GPU_UUID, "ok": True, "model": "moss"}
            fake_socket = MagicMock()
            fake_socket.__enter__.return_value.connect_ex.return_value = 1
            with patch.object(bootstrap.Path, "home", return_value=Path(temporary)), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "source": "# worker", "action": "prepare", "lease_protocol": 1, "lifecycle_source": "# guard"}))), \
                 patch.object(bootstrap.sys, "stdout", io.StringIO()), \
                 patch.object(bootstrap.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(identity).encode())), \
                 patch.object(bootstrap.socket, "socket", return_value=fake_socket), \
                 patch.object(bootstrap.subprocess, "check_output", return_value=xml.encode()), \
                 patch.object(bootstrap.subprocess, "run") as install, \
                 patch.object(bootstrap, "prepare_model", return_value={"model_path": "/cache/model", "model_cache": "/cache"}), \
                 patch.object(bootstrap, "WorkerLease") as lease, \
                 patch.object(bootstrap.subprocess, "Popen", return_value=worker) as launch, \
                 self.assertRaisesRegex(RuntimeError, 'test lease ended'):
                lease.return_value.check.side_effect = [None, None, None, RuntimeError('test lease ended')]
                bootstrap.main()
            self.assertEqual(launch.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], GPU_UUID)
            self.assertEqual(launch.call_args.kwargs["env"]["COSMOSS_WORKER_TOKEN"], "private-test-token")
            self.assertEqual(launch.call_args.kwargs["env"]["MOSS_MODEL_PATH"], "/cache/model")
            self.assertEqual(launch.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(launch.call_args.kwargs["env"]["COSMOSS_WORKER_PARENT_PID"], str(os.getpid()))
            worker.terminate.assert_called_once()
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index("--port") + 1], "18400")
            self.assertIn("127.0.0.1", launch.call_args.args[0])
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            install.assert_not_called()

    def test_busy_gpu_is_not_taken_over(self):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
        busy = GPU_XML.replace("<processes/>", "<processes><process_info><type>C</type><pid>42</pid></process_info></processes>")
        fake_socket = MagicMock()
        fake_socket.__enter__.return_value.connect_ex.return_value = 1
        with patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "source": "# worker", "action": "prepare", "lease_protocol": 1}))), \
             patch.object(bootstrap.urllib.request, "urlopen", side_effect=urllib.error.URLError("not running")), \
             patch.object(bootstrap.socket, "socket", return_value=fake_socket), \
             patch.object(bootstrap.subprocess, "check_output", return_value=busy.encode()), \
             patch.object(bootstrap.subprocess, "Popen") as launch, \
             self.assertRaisesRegex(RuntimeError, "active or unknown"):
            bootstrap.main()
        launch.assert_not_called()

    def test_registration_install_reads_vram_without_loading_any_gpu(self):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
        del device["gpu_uuid"]
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with patch.object(bootstrap.Path, "home", return_value=Path(temporary)), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "action": "install"}))), \
                 patch.object(bootstrap.sys, "stdout", output), \
                 patch.object(bootstrap, "prepare_runtime", return_value=Path(temporary) / "python"), \
                 patch.object(bootstrap, "prepare_model", return_value={"model_path": "/cache/model", "model_cache": "/cache", "reused": True}), \
                 patch.object(bootstrap.subprocess, "check_output", return_value=GPU_XML.encode()) as gpu_probe, \
                 patch.object(bootstrap.subprocess, "Popen") as launch:
                bootstrap.main()
            gpu_probe.assert_called_once_with(['nvidia-smi', '-q', '-x'], timeout=8)
            launch.assert_not_called()
            self.assertIn('"phase": "installed"', output.getvalue())
            self.assertIn('"reused": true', output.getvalue())

    def test_stale_pid_does_not_stop_an_unrelated_process(self):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
        identity = {"app": "cosMOSS CUDA worker", "device_id": device["id"], "gpu_uuid": GPU_UUID}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".local/share/cosmoss/workers" / device["id"]
            root.mkdir(parents=True)
            (root / "worker.pid").write_text("123456789")
            processes = Path(temporary) / "proc"
            (processes / "123456789").mkdir(parents=True)
            (processes / "123456789/cwd").symlink_to(Path(temporary))
            with patch.object(bootstrap.Path, "home", return_value=Path(temporary)), \
                 patch.object(bootstrap, "PROCESSES", processes), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "action": "stop"}))), \
                 patch.object(bootstrap.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(identity).encode())), \
                 patch.object(bootstrap.os, "kill") as kill, \
                 self.assertRaisesRegex(RuntimeError, "ownership"):
                bootstrap.main()
            kill.assert_not_called()

    def test_device_cleanup_recovers_gpu_identity_from_its_own_worker_record(self):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
        del device["gpu_uuid"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".local/share/cosmoss/workers" / device["id"]
            root.mkdir(parents=True)
            (root / "worker.pid").write_text("123456789")
            (root / "worker.json").write_text(json.dumps({"gpu_uuid": GPU_UUID}))
            processes = Path(temporary) / "proc"
            process = processes / "123456789"
            process.mkdir(parents=True)
            (process / "cwd").symlink_to(root)
            (process / "environ").write_bytes(b"\0".join([
                b"CUDA_VISIBLE_DEVICES=" + GPU_UUID.encode(), b"COSMOSS_DEVICE_ID=" + device["id"].encode(),
                b"COSMOSS_WORKER_TOKEN=" + device["api_key"].encode(),
            ]))
            with patch.object(bootstrap.Path, "home", return_value=Path(temporary)), \
                 patch.object(bootstrap, "PROCESSES", processes), \
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "action": "stop"}))), \
                 patch.object(bootstrap.sys, "stdout", io.StringIO()), \
                 patch.object(bootstrap.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")), \
                 patch.object(bootstrap.os, "kill", side_effect=lambda *_: shutil.rmtree(process)) as kill:
                bootstrap.main()
            kill.assert_called_once_with(123456789, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
