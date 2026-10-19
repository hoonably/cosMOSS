import asyncio
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.config import TranscriptionConfig
from src.devices import DeviceStore, ResolvedDevice, public_device, resolve_device
from src.ssh_devices import SSHError, discover_hosts, parse_gpu_xml, probe_host, ssh_arguments


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

    def test_ssh_uses_strict_noninteractive_arguments(self):
        arguments = ssh_arguments("gpu-a")
        for option in ["BatchMode=yes", "StrictHostKeyChecking=yes", "ControlPath=none", "ForwardAgent=no"]:
            self.assertIn(option, arguments)
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
            device = store.add_ssh(name="GPU 0", host="gpu-a", remote_port=18400,
                                   model="moss", backend="cuda", gpu_uuid=GPU_UUID, gpu_index=0)
            self.assertTrue(device["api_key"])
            self.assertNotIn("api_key", public_device(device))
            self.assertEqual(DeviceStore(database).get(device["id"]), device)
            with self.assertRaises(ValueError):
                store.delete("local")
            with self.assertRaises(ValueError):
                store.add_ssh(name="duplicate", host="gpu-a", remote_port=18401,
                              model="moss", backend="cuda", gpu_uuid=GPU_UUID, gpu_index=0)


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
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)
        self.spec = {"name": "Lab GPU 0", "host": "gpu-a", "remote_port": 18400,
                     "model": "moss-model", "backend": "cuda", "gpu_uuid": GPU_UUID, "gpu_index": 0}

    async def add(self):
        response = await self.client.post("/api/devices", json=self.spec)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_default_is_local_and_adding_needs_no_connection(self):
        self.assertEqual([device["id"] for device in (await self.client.get("/api/devices")).json()], ["local"])
        added = await self.add()
        self.assertEqual(added["gpu_uuid"], GPU_UUID)
        self.assertNotIn("api_key", added)
        devices = (await self.client.get("/api/devices")).json()
        self.assertEqual(len(devices), 2)
        self.assertNotIn("api_key", json.dumps(devices))

    async def test_cuda_requires_gpu_selection_and_configured_alias(self):
        for change in [{"gpu_uuid": None}, {"gpu_index": None}, {"host": "not-in-config"}]:
            response = await self.client.post("/api/devices", json={**self.spec, **change})
            self.assertEqual(response.status_code, 400, response.text)

    async def test_remote_clients_and_dns_rebinding_hosts_cannot_manage_ssh(self):
        for client, hostname in [("10.0.0.2", "localhost"), ("127.0.0.1", "attacker.example")]:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app, client=(client, 10000)), base_url=f"http://{hostname}") as outsider:
                self.assertEqual((await outsider.get("/api/ssh/hosts")).status_code, 403)
        self.assertEqual((await self.client.get("/api/devices", headers={"Sec-Fetch-Site": "cross-site"})).status_code, 403)

    async def test_note_pins_device_and_hides_worker_token(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"]}, files={"file": ("voice.wav", b"audio", "audio/wav")})
        self.assertEqual(response.status_code, 202, response.text)
        note = response.json()
        self.assertEqual(note["device_id"], device["id"])
        self.assertEqual(note["device_name"], "Lab GPU 0")
        self.assertNotIn("device_snapshot_json", note)
        snapshot = json.loads(self.store.get(note["id"])["device_snapshot_json"])
        self.assertEqual(snapshot["gpu_uuid"], GPU_UUID)
        self.assertEqual(snapshot["remote_port"], 18400)
        self.assertNotIn(snapshot["api_key"], response.text)
        exported = await self.client.get(f"/api/notes/{note['id']}/export/json")
        self.assertNotIn(snapshot["api_key"], exported.text)

    async def test_inference_receives_the_pinned_gpu_even_if_registry_changes(self):
        device = await self.add()
        response = await self.client.post("/api/notes", data={"device_id": device["id"]}, files={"file": ("voice.wav", b"audio", "audio/wav")})
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

    async def test_preparation_receives_selected_gpu_and_stopping_is_explicit(self):
        device = await self.add()
        with patch.object(self.workers, "start", return_value={"phase": "connecting"}) as start:
            response = await self.client.post(f"/api/devices/{device['id']}/worker/prepare")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(start.call_args.args[0]["gpu_uuid"], GPU_UUID)
        self.assertIn("api_key", start.call_args.args[0])
        self.assertEqual((await self.client.delete(f"/api/devices/{device['id']}")).status_code, 409)
        self.workers.states[device["id"]] = {"phase": "stopped"}
        self.assertEqual((await self.client.delete(f"/api/devices/{device['id']}")).status_code, 204)

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
    def spec(self):
        return {"id": "a" * 32, "gpu_uuid": GPU_UUID, "remote_port": 18400,
                "model": "moss", "api_key": "private-test-token"}

    def test_launch_masks_the_physical_gpu_and_binds_loopback(self):
        from src import remote_bootstrap as bootstrap
        device = self.spec()
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
                 patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "source": "# worker", "action": "prepare"}))), \
                 patch.object(bootstrap.sys, "stdout", io.StringIO()), \
                 patch.object(bootstrap.urllib.request, "urlopen", side_effect=[urllib.error.URLError("not running"), io.BytesIO(json.dumps(identity).encode())]), \
                 patch.object(bootstrap.socket, "socket", return_value=fake_socket), \
                 patch.object(bootstrap.subprocess, "check_output", return_value=GPU_XML.encode()), \
                 patch.object(bootstrap.subprocess, "run") as install, \
                 patch.object(bootstrap.subprocess, "Popen", return_value=worker) as launch:
                bootstrap.main()
            self.assertEqual(launch.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], GPU_UUID)
            self.assertEqual(launch.call_args.kwargs["env"]["COSMOSS_WORKER_TOKEN"], "private-test-token")
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
        with patch.object(bootstrap.sys, "stdin", io.StringIO(json.dumps({"device": device, "source": "# worker"}))), \
             patch.object(bootstrap.urllib.request, "urlopen", side_effect=urllib.error.URLError("not running")), \
             patch.object(bootstrap.socket, "socket", return_value=fake_socket), \
             patch.object(bootstrap.subprocess, "check_output", return_value=busy.encode()), \
             patch.object(bootstrap.subprocess, "Popen") as launch, \
             self.assertRaisesRegex(RuntimeError, "active or unknown"):
            bootstrap.main()
        launch.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
