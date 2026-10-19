"""Byte totals, cached files, resume offsets, and per-second event reporting."""
import json
import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.model_download import DownloadProgress, download_model


class DownloadProgressTests(unittest.TestCase):
    def test_fixed_byte_total_handles_resume_retry_and_completion(self):
        progress = DownloadProgress({'small': 10, 'weights': 90}, Mock())
        progress.update('small', 10)
        progress.update('weights', 45)
        self.assertEqual(progress.snapshot()['percent'], 55)
        progress.update('weights', 20)  # retry reset, not a second file/total
        self.assertEqual(progress.snapshot()['downloaded_bytes'], 30)
        progress.update('weights', 999)
        self.assertEqual(progress.snapshot()['total_bytes'], 100)
        self.assertEqual(progress.snapshot()['percent'], 99.9)
        progress.finished = True
        self.assertEqual(progress.snapshot()['percent'], 100)

    def test_cached_and_resumed_files_report_bytes_every_second(self, legacy=False):
        module = types.ModuleType('huggingface_hub')
        utils = types.ModuleType('huggingface_hub.utils')

        class Tqdm:
            def __init__(self, *args, **kwargs):
                self.n = kwargs.get('initial', 0)

        utils.tqdm = Tqdm
        files = [types.SimpleNamespace(rfilename=name, size=size, blob_id=name, lfs=None)
                 for name, size in [('cached', 40), ('weights', 60)]]
        module.HfApi = Mock(return_value=Mock(model_info=Mock(return_value=types.SimpleNamespace(siblings=files, sha='revision'))))
        events = []

        def fetch(*, tqdm_class, **kwargs):
            self.assertEqual(kwargs['revision'], 'revision')
            if kwargs['filename'] == 'weights':
                bar = tqdm_class(initial=10, total=60)
                bar.update(20)
                self.assertEqual(bar.n, 30)
                time.sleep(1.1)
                bar.update(30)
            return '/cache/snapshot/' + kwargs['filename']

        internal = types.ModuleType('huggingface_hub.file_download')
        internal.tqdm = Tqdm
        module.file_download = internal
        def old_fetch(**kwargs):
            return fetch(tqdm_class=internal.tqdm, **kwargs)
        module.hf_hub_download = old_fetch if legacy else fetch
        module.snapshot_download = Mock(return_value='/cache/snapshot')
        with tempfile.TemporaryDirectory() as temporary:
            blobs = Path(temporary) / 'models--owner--model/blobs'
            blobs.mkdir(parents=True)
            (blobs / 'cached').write_bytes(bytes(40))
            with patch.dict('sys.modules', {'huggingface_hub': module, 'huggingface_hub.utils': utils}):
                result = download_model('owner/model', temporary, lambda item: events.append((time.monotonic(), item)))
        self.assertIs(internal.tqdm, Tqdm)
        self.assertEqual(result, '/cache/snapshot')
        self.assertEqual(events[0][1]['downloaded_bytes'], 40)
        self.assertEqual(events[1][1]['downloaded_bytes'], 70)
        self.assertAlmostEqual(events[1][0] - events[0][0], 1, delta=.2)
        self.assertEqual(events[-1][1]['percent'], 100)
        self.assertTrue(module.snapshot_download.call_args.kwargs['local_files_only'])

    def test_legacy_hub_tracks_bytes_and_restores_the_compatibility_hook(self):
        self.test_cached_and_resumed_files_report_bytes_every_second(legacy=True)


class RemoteProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssh_stream_exposes_progress_before_remote_installation_finishes(self):
        from unittest.mock import AsyncMock
        from src.remote_workers import RemoteWorkers
        workers = RemoteWorkers()
        device = {'id': 'a' * 32, 'host': 'gpu-a'}
        advanced = asyncio.Event()
        finish = asyncio.Event()

        async def output():
            yield b'COSMOSS:{"phase":"downloading","download":{"percent":42,"downloaded_bytes":42,"total_bytes":100}}\n'
            advanced.set()
            await finish.wait()
            yield b'COSMOSS:{"phase":"installed"}\n'

        process = Mock()
        process.stdout = output()
        process.stdin.drain = AsyncMock()
        process.returncode = 0
        process.wait = AsyncMock(return_value=0)
        with patch('src.remote_workers.discover_hosts', return_value=['gpu-a']), patch('src.remote_workers.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)):
            workers.start(device)
            await asyncio.wait_for(advanced.wait(), 2)
            self.assertEqual(workers.states[device['id']]['download']['percent'], 42)
            self.assertFalse(workers.tasks[device['id']].done())
            payload = json.loads(process.stdin.write.call_args.args[0])
            self.assertIn('download_source', payload)
            finish.set()
            await workers.tasks[device['id']]
        self.assertEqual(workers.states[device['id']]['phase'], 'installed')
