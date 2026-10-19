"""Local setup tests use tiny model fixtures; no downloads or MLX required."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.local_models import DEFAULT_MODEL, LocalModels, find_cached_model, validate_model_folder


def model_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ('config.json', 'processor_config.json', 'preprocessor_config.json', 'tokenizer_config.json', 'tokenizer.json'):
        (path / name).write_text(json.dumps({'model_type': 'moss_transcribe_diarize'} if name == 'config.json' else {}))
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

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def test_cache_hit_does_not_download(self):
        snapshot = model_folder(self.manager.cache / ('models--' + DEFAULT_MODEL.replace('/', '--')) / 'snapshots/abc')
        self.manager.command = AsyncMock(side_effect=AssertionError('Must reuse cached weights'))
        await self.manager.run('cache', None)
        self.assertEqual(self.manager.status()['phase'], 'ready')
        self.assertTrue(self.manager.status()['reused'])
        self.manager.launch.assert_awaited_once_with(self.root / 'python', snapshot)

    async def test_folder_used_in_place_and_restored_without_installation(self):
        folder = model_folder(self.root / 'existing model')
        self.manager.download = AsyncMock(side_effect=AssertionError('Must not copy/download'))
        with patch('src.local_models.platform.system', return_value='Darwin'), patch('src.local_models.platform.machine', return_value='arm64'):
            self.manager.start('folder', str(folder))
            await self.manager.task
        self.assertEqual(json.loads(self.manager.settings_file.read_text())['model_path'], str(folder))
        restored = LocalModels(self.root, self.root / 'data', 19290, 'moss-mtd')
        restored.runtime = self.manager.runtime
        (restored.runtime / 'bin').mkdir(parents=True)
        (restored.runtime / 'bin/python').touch()
        restored.prepare_runtime = AsyncMock(side_effect=AssertionError('No install on startup'))
        restored.download = AsyncMock(side_effect=AssertionError('No download on startup'))
        restored.launch = AsyncMock()
        await restored.restore()
        await restored.task
        self.assertEqual(restored.status()['phase'], 'ready')
        restored.launch.assert_awaited_once_with(restored.runtime / 'bin/python', folder)
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
        self.manager.launch.side_effect = RuntimeError('loading failed')
        await self.manager.run('folder', model_folder(self.root / 'model'))
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIn('loading failed', self.manager.status()['error'])
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
