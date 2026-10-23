"""Local setup tests use tiny model fixtures; no downloads or MLX required."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.local_models import DEFAULT_MODEL, RUNTIME_VERSION, LocalModels, find_cached_model, validate_model_folder
from src.model_catalog import DEFAULT_OPTIONS


def model_folder(path, bits=8):
    path.mkdir(parents=True, exist_ok=True)
    for name in ('config.json', 'processor_config.json', 'preprocessor_config.json', 'tokenizer_config.json', 'tokenizer.json'):
        (path / name).write_text(json.dumps({'model_type': 'moss_transcribe_diarize', 'quantization': {'bits': bits}} if name == 'config.json' else {}))
    header = json.dumps({'weight': {'dtype': 'F32', 'shape': [1], 'data_offsets': [0, 4]}}).encode()
    (path / 'model.safetensors').write_bytes(len(header).to_bytes(8, 'little') + header + bytes(4))
    return path.resolve()


class ModelFilesTests(unittest.TestCase):
    def test_existing_folder_and_hf_symlink_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = model_folder(root / ('models--' + DEFAULT_MODEL.replace('/', '--')) / 'snapshots/revision')
            blob = root / 'blob'
            (snapshot / 'model.safetensors').rename(blob)
            (snapshot / 'model.safetensors').symlink_to(blob)
            self.assertEqual(find_cached_model(root, DEFAULT_MODEL), snapshot)

    def test_reject_partial_wrong_model_and_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = model_folder(Path(temporary) / 'model')
            weight = folder / 'model.safetensors'
            weight.write_bytes(weight.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, '완전하지'):
                validate_model_folder(folder)
            (folder / 'config.json').write_text('{"model_type": "whisper"}')
            with self.assertRaisesRegex(ValueError, 'MLX'):
                validate_model_folder(folder)
            (folder / 'config.json').write_text('[]')
            with self.assertRaises(ValueError):
                validate_model_folder(folder)


class ModelPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = LocalModels(self.root, self.root / 'data', 19290, 'moss-mtd')
        self.manager.runtime = self.root / 'runtime'
        self.manager.cache = self.root / 'cache'
        self.manager.prepare_runtime = AsyncMock(return_value=self.root / 'python')
        self.manager.launch = AsyncMock()
        (self.manager.runtime / 'bin').mkdir(parents=True)
        (self.manager.runtime / 'bin/python').touch()
        (self.manager.runtime / 'cosmoss-runtime-version').write_text(RUNTIME_VERSION)

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def test_cache_hit_does_not_download(self):
        snapshot = model_folder(self.manager.cache / ('models--' + DEFAULT_MODEL.replace('/', '--')) / 'snapshots/abc')
        self.manager.command = AsyncMock(side_effect=AssertionError('Must reuse cached weights'))
        await self.manager.run('cache', None)
        self.assertEqual(self.manager.status()['phase'], 'installed')
        self.assertTrue(self.manager.status()['reused'])
        self.manager.launch.assert_not_awaited()
        self.assertEqual(self.manager.model_path_for(self.manager.current_selection()), snapshot)

    async def test_folder_used_in_place_and_restored_without_installation(self):
        folder = model_folder(self.root / 'existing model')
        self.manager.download = AsyncMock(side_effect=AssertionError('Must not copy/download'))
        with patch('src.local_models.platform.system', return_value='Darwin'), patch('src.local_models.platform.machine', return_value='arm64'):
            self.manager.start('folder', str(folder))
            await self.manager.task
        self.assertEqual(json.loads(self.manager.settings_file.read_text())['model_path'], str(folder))
        restored = LocalModels(self.root, self.root / 'data', 19290, 'moss-mtd')
        restored.runtime = self.manager.runtime
        restored.prepare_runtime = AsyncMock(side_effect=AssertionError('No install on startup'))
        restored.download = AsyncMock(side_effect=AssertionError('No download on startup'))
        restored.launch = AsyncMock()
        await restored.restore()
        self.assertIsNone(restored.task)
        self.assertEqual(restored.status()['phase'], 'installed')
        restored.launch.assert_not_awaited()
        await restored.close()

    async def test_model_choice_and_runtime_knobs_survive_restart(self):
        from src.model_catalog import DEFAULT_OPTIONS
        folder = model_folder(self.root / 'four bit model')
        config = json.loads((folder/'config.json').read_text())
        config['quantization'] = {'bits':4}
        (folder/'config.json').write_text(json.dumps(config))
        with patch('src.local_models.platform.system',return_value='Darwin'), patch('src.local_models.platform.machine',return_value='arm64'):
            self.manager.start('folder',str(folder),model_id='vanch007/mlx-MOSS-Transcribe-Diarize-4bit',model_key='mlx-4bit',options={**DEFAULT_OPTIONS,'kv_bits':0,'prefill_step_size':256})
            await self.manager.task
        restored=LocalModels(self.root,self.root/'data',19290,'moss-mtd')
        self.assertTrue(restored.model_id.endswith('4bit'))
        self.assertEqual(restored.options['kv_bits'],0)
        self.assertEqual(restored.options['prefill_step_size'],256)
        self.assertEqual(restored.status()['model_key'],'mlx-4bit')

    async def test_missing_folder_rejected_before_any_installation(self):
        with patch('src.local_models.platform.system', return_value='Darwin'), patch('src.local_models.platform.machine', return_value='arm64'):
            for folder in ['', str(self.root / 'missing')]:
                with self.assertRaises(ValueError):
                    self.manager.start('folder', folder)
        self.manager.prepare_runtime.assert_not_awaited()

    async def test_error_and_shutdown_leave_no_owned_process(self):
        self.manager.prepare_runtime.side_effect = RuntimeError('runtime failed')
        await self.manager.run('folder', model_folder(self.root / 'model'))
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIn('runtime failed', self.manager.status()['error'])
        self.assertFalse(self.manager.settings_file.exists())
        # A genuinely pending preparation must be cancellable during app shutdown.
        self.manager.prepare_runtime = AsyncMock(side_effect=asyncio.CancelledError)
        self.manager.task = asyncio.create_task(self.manager.run('cache', None))
        await asyncio.sleep(0)
        await self.manager.close()
        self.assertIsNone(self.manager.process)

    async def test_existing_unrelated_service_is_not_terminated(self):
        server = await asyncio.start_server(lambda reader, writer: writer.close(), '127.0.0.1', 0)
        self.manager.port = server.sockets[0].getsockname()[1]
        self.manager.probe = AsyncMock(return_value={'app': 'other'})
        try:
            with self.assertRaisesRegex(RuntimeError, '이미 사용 중'):
                await LocalModels.launch(self.manager, self.root / 'python', self.root / 'model')
            self.assertIsNone(self.manager.process)
            self.assertTrue(server.is_serving())
        finally:
            server.close()
            await server.wait_closed()

    async def test_cancelling_install_command_stops_its_subprocess(self):
        spawned = []
        create_process = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            process = await create_process(*args, **kwargs)
            spawned.append(process)
            return process

        with patch('src.local_models.asyncio.create_subprocess_exec', side_effect=capture):
            task = asyncio.create_task(self.manager.command([sys.executable, '-c', 'import time; time.sleep(60)']))
            while not spawned:
                await asyncio.sleep(.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsNotNone(spawned[0].returncode)

    async def test_download_events_are_available_before_subprocess_finishes(self):
        script = "import json,time; print('COSMOSS_DOWNLOAD:' + json.dumps({'percent':37,'downloaded_bytes':37,'total_bytes':100}), flush=True); time.sleep(60)"
        task = asyncio.create_task(self.manager.command([sys.executable, '-c', script]))
        try:
            async with asyncio.timeout(3):
                while self.manager.status()['phase'] != 'downloading':
                    await asyncio.sleep(.01)
            self.assertEqual(self.manager.status()['download']['percent'], 37)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_api_blocks_switch_while_local_job_is_running(self):
        from src import main
        with patch.object(main, 'LOCAL_MODELS', self.manager), patch.object(main.STORE, 'list', return_value=[{'device_id': 'local', 'status': 'processing'}]):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1') as client:
                response = await client.post('/api/local-model/prepare', json={'source': 'cache'})
                self.assertEqual(response.status_code, 409)
        self.manager.prepare_runtime.assert_not_awaited()

    def attach_diarization(self, ready=True):
        engine = Mock()
        engine.lock = asyncio.Lock()
        engine.ready.return_value = ready
        engine.execute = AsyncMock(return_value={'phase': 'installed'})
        self.manager.diarization = engine
        self.manager.lock = engine.lock
        return engine

    async def test_bundle_preparation_and_restore_require_nemotron(self):
        engine = self.attach_diarization()
        folder = model_folder(self.root / 'model')
        await self.manager.run('folder', folder)
        engine.execute.assert_awaited_once()
        self.assertEqual(engine.execute.call_args.args[:2], ('nemotron-3', 'install'))
        self.assertTrue(self.manager.status()['ready'])
        self.manager.launch.assert_not_awaited()
        engine.ready.return_value = False
        await self.manager.restore()
        self.assertEqual(self.manager.status()['phase'], 'idle')
        self.assertTrue(self.manager.status()['asr_ready'])
        self.assertFalse(self.manager.status()['ready'])
        self.assertEqual(engine.execute.await_count, 1, 'Restore must not install missing components')

    async def test_install_auth_failure_does_not_mark_bundle_prepared(self):
        engine = self.attach_diarization(False)
        engine.execute.return_value = {'phase': 'auth_required', 'error': 'login required'}
        await self.manager.run('folder', model_folder(self.root / 'model'))
        self.assertEqual(self.manager.status()['phase'], 'auth_required')
        self.assertFalse(self.manager.settings_file.exists())

    async def test_prepare_alternative_without_rewriting_saved_selection(self):
        engine = self.attach_diarization()
        original = model_folder(self.root / 'custom model')
        await self.manager.run('folder', original, model_id='org/custom', model_key='custom')
        saved = self.manager.settings_file.read_bytes()
        other_id = 'vanch007/mlx-MOSS-Transcribe-Diarize'
        cached = model_folder(self.manager.cache / ('models--' + other_id.replace('/', '--')) / 'snapshots/alt', bits=16)
        with patch('src.local_models.platform.system', return_value='Darwin'), patch('src.local_models.platform.machine', return_value='arm64'):
            self.manager.start('cache', model_id=other_id, model_key='mlx-16bit', options={**DEFAULT_OPTIONS, 'kv_bits': 0}, remember=False)
            await self.manager.task
        self.assertEqual(self.manager.model_id, 'org/custom')
        self.assertEqual(self.manager.model_key, 'custom')
        self.assertEqual(self.manager.settings_file.read_bytes(), saved)
        selection = {'model_key': 'mlx-16bit', 'model_id': other_id}
        self.assertTrue(self.manager.readiness_for(selection)['ready'])
        self.assertEqual(self.manager.model_path_for(selection), cached)
        self.manager.launch.assert_not_awaited()

    async def test_session_uses_snapshot_options_and_unloads_on_success_and_error(self):
        self.attach_diarization()
        path = model_folder(self.root / 'snapshot model')
        snapshot = {'model_source': 'folder', 'model_path': str(path), 'model_key': 'custom', 'model_id': 'org/snapshot',
                    'options': {**DEFAULT_OPTIONS, 'kv_bits': 0, 'max_tokens': 1024}}
        before = self.manager.current_selection()
        self.manager.stop_process = AsyncMock()
        events = []
        async with self.manager.inference_session(snapshot, events.append):
            self.assertTrue(self.manager.lock.locked())
            snapshot['options']['kv_bits'] = 4
            self.manager.launch.assert_awaited_once_with(self.manager.runtime / 'bin/python', path,
                                                       options={**DEFAULT_OPTIONS, 'kv_bits': 0, 'max_tokens': 1024})
        self.manager.stop_process.assert_awaited_once()
        self.assertEqual(self.manager.current_selection(), before)
        self.assertFalse(self.manager.lock.locked())
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            async with self.manager.inference_session(snapshot, events.append):
                raise RuntimeError('failed')
        self.assertEqual(self.manager.stop_process.await_count, 2)

    async def test_repeated_cancellation_holds_lease_until_cleanup_finishes(self):
        self.attach_diarization()
        folder = model_folder(self.root / 'model')
        snapshot = {'model_source': 'folder', 'model_path': str(folder)}
        running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stop():
            cleaning.set()
            await release.wait()
        self.manager.stop_process = AsyncMock(side_effect=stop)
        async def infer():
            async with self.manager.inference_session(snapshot, lambda _: None):
                running.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(infer())
        await asyncio.wait_for(running.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.manager.lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager.lock.locked())

    async def test_unprepared_snapshot_does_not_launch_or_download(self):
        self.attach_diarization()
        self.manager.download = AsyncMock()
        with self.assertRaisesRegex(ValueError, '먼저 준비'):
            async with self.manager.inference_session({'model_key': 'mlx-16bit'}, lambda _: None):
                self.fail('unprepared variant ran')
        self.manager.launch.assert_not_awaited()
        self.manager.download.assert_not_awaited()

    async def test_loading_failure_reaps_owned_child(self):
        self.attach_diarization()
        path = model_folder(self.root / 'model')
        child = None
        async def launch(*_, **__):
            nonlocal child
            child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
            self.manager.process = child
            raise RuntimeError('load failed')
        self.manager.launch = launch
        with self.assertRaisesRegex(RuntimeError, 'load failed'):
            async with self.manager.inference_session({'model_source': 'folder', 'model_path': str(path)}, lambda _: None):
                self.fail('Failed model entered inference')
        self.assertIsNotNone(child.returncode)
        self.assertIsNone(self.manager.process)
        self.assertFalse(self.manager.lock.locked())

    async def test_launch_option_override_does_not_modify_default_choice(self):
        captured = {}
        original_options = self.manager.options.copy()
        create_process = asyncio.create_subprocess_exec
        async def spawn(*_, **kwargs):
            captured.update(kwargs['env'])
            return await create_process(sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
        async def probe():
            return {'instance_id': captured['COSMOSS_LOCAL_MODEL_INSTANCE'], 'model_path': str(self.root / 'model')}
        self.manager.probe = probe
        with patch('src.local_models.asyncio.open_connection', side_effect=OSError), \
             patch('src.local_models.asyncio.create_subprocess_exec', side_effect=spawn):
            await LocalModels.launch(self.manager, self.root / 'python', self.root / 'model',
                                     options={**DEFAULT_OPTIONS, 'kv_bits': 0, 'max_tokens': 1024})
        self.assertEqual(captured['MLX_KV_BITS'], '0')
        self.assertEqual(captured['MLX_MAX_TOKENS'], '1024')
        self.assertEqual(self.manager.options, original_options)
        child = self.manager.process
        await self.manager.stop_process()
        self.assertIsNotNone(child.returncode)

    async def test_queued_snapshot_pins_revision_and_does_not_fallback_if_removed(self):
        repository = self.manager.cache / ('models--' + DEFAULT_MODEL.replace('/', '--'))
        original = model_folder(repository / 'snapshots/original')
        selection = {'model_key': 'mlx-8bit', 'model_id': DEFAULT_MODEL, 'model_source': 'cache', 'model_path': str(original)}
        newer = model_folder(repository / 'snapshots/newer')
        (repository / 'refs').mkdir()
        (repository / 'refs/main').write_text('newer')
        self.assertEqual(self.manager.model_path_for(selection), original)
        self.assertEqual(self.manager.model_path_for({'model_key': 'mlx-8bit'}), newer)
        (original / 'config.json').unlink()
        self.assertIsNone(self.manager.model_path_for(selection))
        self.assertFalse(self.manager.readiness_for(selection)['ready'])

    async def test_changed_folder_precision_invalidates_readiness_and_installed_status(self):
        folder = model_folder(self.root / 'model')
        await self.manager.run('folder', folder)
        self.assertTrue(self.manager.status()['ready'])
        (folder / 'config.json').write_text(json.dumps({'model_type': 'moss_transcribe_diarize', 'quantization': {'bits': 4}}))
        self.assertFalse(self.manager.status()['ready'])
        self.assertEqual(self.manager.status()['phase'], 'idle')

    async def test_legacy_snapshot_runs_asr_without_new_diarization_install(self):
        engine = self.attach_diarization(False)
        folder = model_folder(self.root / 'legacy model')
        snapshot = {'model_key': 'mlx-8bit', 'model_source': 'folder', 'model_path': str(folder),
                    'options': dict(DEFAULT_OPTIONS)}
        self.manager.stop_process = AsyncMock()
        async with self.manager.inference_session(snapshot, lambda _: None):
            self.manager.launch.assert_awaited_once()
        engine.execute.assert_not_awaited()
        self.manager.stop_process.assert_awaited_once()
        self.manager.launch.reset_mock()
        snapshot['options']['local_diarization'] = 'nemotron-3'
        with self.assertRaisesRegex(ValueError, '먼저 준비'):
            async with self.manager.inference_session(snapshot, lambda _: None):
                self.fail('New combined pipeline ran without Nemotron')
        self.manager.launch.assert_not_awaited()
