"""Retired rerun routes stay absent; the transcription worker remains usable."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from src import diarization_worker as worker
from src.local_diarization import LocalDiarization
from src.speaker_records import SpeakerRecords


class SpeakerAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_routes_are_not_exposed(self):
        from src import main
        app = main.app
        self.enterContext(patch.object(main, 'note_cleanup', return_value=SimpleNamespace(hidden_note_ids=lambda: set())))
        self.assertFalse(any('/diarization' in route.path for route in app.routes))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            for path in ('/api/diarization/models', '/api/diarization/setup',
                         '/api/notes/' + 'a' * 32 + '/diarization',
                         '/api/notes/' + 'a' * 32 + '/diarization/approve'):
                self.assertEqual((await client.get(path)).status_code, 404)
                # The static-file fallback rejects non-GET requests with 405.
                self.assertIn((await client.post(path, json={})).status_code, (404, 405))

    async def test_local_worker_cancellation_reaps_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = LocalDiarization(Path(directory))
            started, child = asyncio.Event(), []
            def event(value):
                child.append(value['pid'])
                started.set()
            program = "import os,time,json; print('COSMOSS_DIARIZATION:'+json.dumps({'pid':os.getpid()}),flush=True); time.sleep(60)"
            task = asyncio.create_task(engine.command([sys.executable, '-u', '-c', program], event))
            try:
                await asyncio.wait_for(started.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with self.assertRaises(ProcessLookupError):
                os.kill(child[0], 0)

    def test_nemotron_cache_reuse_and_download(self):
        class CacheMiss(Exception): pass
        class HTTPError(Exception): pass
        hub, errors = ModuleType('huggingface_hub'), ModuleType('huggingface_hub.errors')
        errors.LocalEntryNotFoundError, errors.HfHubHTTPError = CacheMiss, HTTPError
        hub.snapshot_download = Mock(return_value='/cached/model')
        with patch.dict(sys.modules, {'huggingface_hub': hub, 'huggingface_hub.errors': errors}), patch.object(worker, 'emit'):
            self.assertEqual(worker.setup('nemotron-3'), '/cached/model')
            self.assertTrue(hub.snapshot_download.call_args.kwargs['local_files_only'])
            hub.snapshot_download.side_effect = [CacheMiss(), '/downloaded/model']
            self.assertEqual(worker.setup('nemotron-3'), '/downloaded/model')
            self.assertFalse(hub.snapshot_download.call_args.kwargs['local_files_only'])
        self.assertEqual(set(worker.MODELS), {'nemotron-3'})

    def test_legacy_record_cleanup_rejects_external_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            records = SpeakerRecords(Path(directory))
            for name in ('', '.', '..', '../outside', '/outside'):
                with self.assertRaises(ValueError):
                    records.forget(name)
