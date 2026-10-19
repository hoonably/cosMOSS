"""Updates must wait for actual work while permitting an idle loaded model."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import main


class UpdateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_and_loaded_models_allow_update(self):
        with patch.object(main, 'STORE', SimpleNamespace(list=lambda: [])), \
             patch.object(main, 'LOCAL_MODELS', SimpleNamespace(task=None, state={'phase': 'ready'})), \
             patch.object(main, 'PREPARATION_TASKS', {}), patch.object(main, 'TRANSCRIPTION_TASKS', {}), \
             patch.object(main, 'SUMMARIES', SimpleNamespace(tasks={})), \
             patch.object(main, 'REMOTE_WORKERS', SimpleNamespace(tasks={})):
            self.assertEqual(await main.update_status(), {'busy': False})

    async def test_each_running_job_blocks_update_and_completion_unblocks(self):
        for kind in ('upload', 'transcription', 'summary', 'remote', 'model'):
            with self.subTest(kind=kind):
                task = asyncio.create_task(asyncio.Event().wait())
                completed = asyncio.get_running_loop().create_future()
                completed.set_result(None)
                active = {'job': task}
                with patch.object(main, 'STORE', SimpleNamespace(list=lambda: [])), \
                     patch.object(main, 'LOCAL_MODELS', SimpleNamespace(task=task if kind == 'model' else completed)), \
                     patch.object(main, 'PREPARATION_TASKS', active if kind == 'upload' else {}), \
                     patch.object(main, 'TRANSCRIPTION_TASKS', active if kind == 'transcription' else {}), \
                     patch.object(main, 'SUMMARIES', SimpleNamespace(tasks=active if kind == 'summary' else {})), \
                     patch.object(main, 'REMOTE_WORKERS', SimpleNamespace(tasks=active if kind == 'remote' else {})):
                    try:
                        self.assertEqual(await main.update_status(), {'busy': True})
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    self.assertEqual(await main.update_status(), {'busy': False})

    async def test_queued_notes_block_even_before_worker_starts(self):
        for status in ('queued', 'processing'):
            with self.subTest(status=status), patch.object(main, 'STORE', SimpleNamespace(list=lambda: [{'status': status}])):
                self.assertEqual(await main.update_status(), {'busy': True})
