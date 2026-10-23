"""Mac bundle isolation and finite process lifetime, without model downloads."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.local_diarization import LocalDiarization


class LocalDiarizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = LocalDiarization(Path(self.temporary.name))

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_inference_waits_for_shared_mlx_lease(self):
        self.engine.ready = lambda: True
        self.engine.worker = AsyncMock(return_value={'phase': 'done', 'result': {}})
        async with self.engine.lock:
            task = asyncio.create_task(self.engine.execute('nemotron-3', 'run', lambda _: None, audio='audio.wav'))
            await asyncio.sleep(0)
            self.engine.worker.assert_not_awaited()
        self.assertEqual((await task)['phase'], 'done')
        self.engine.worker.assert_awaited_once()

    async def test_two_inferences_do_not_overlap(self):
        self.engine.ready = lambda: True
        entered, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0
        async def worker(*_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            await release.wait()
            active -= 1
            return {'phase': 'done'}
        self.engine.worker = worker
        tasks = [asyncio.create_task(self.engine.execute('nemotron-3', 'run', lambda _: None, audio='audio.wav')) for _ in range(2)]
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(peak, 1)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak, 1)

    async def test_install_auth_or_error_event_cannot_write_ready_stamp(self):
        self.engine.python.parent.mkdir(parents=True)
        self.engine.python.touch()
        for phase in ('auth_required', 'error'):
            self.engine.command = AsyncMock(side_effect=[{}, {'phase': phase, 'error': 'not available'}])
            with patch('src.local_diarization.ensure_uv', return_value='uv'):
                result = await self.engine.execute('nemotron-3', 'install', lambda _: None)
            self.assertEqual(result['phase'], phase)
            self.assertFalse((self.engine.root / 'ready').exists())

    async def test_missing_install_completion_is_not_success(self):
        self.engine.command = AsyncMock(return_value={})
        with self.assertRaisesRegex(RuntimeError, '설치 완료'):
            await self.engine.worker({'action': 'install', 'model_key': 'nemotron-3'}, lambda _: None)

    def test_manual_mlx_server_has_no_parent_watchdog(self):
        from src import mlx_server
        with patch.dict('os.environ', {}, clear=True), patch.object(mlx_server.threading, 'Thread') as thread:
            mlx_server.watch_managed_parent()
        thread.assert_not_called()

    def test_managed_mlx_watchdog_is_armed_before_loading(self):
        from src import mlx_server
        with patch.dict('os.environ', {'COSMOSS_LOCAL_MODEL_PARENT_PID': '1234'}), patch.object(mlx_server.threading, 'Thread') as thread:
            mlx_server.watch_managed_parent()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    async def test_orphan_managed_mlx_exits_without_loading_weights(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; from src.mlx_server import watch_managed_parent; watch_managed_parent(); time.sleep(10)',
            env={**os.environ, 'COSMOSS_LOCAL_MODEL_PARENT_PID': '2147483647'})
        try:
            self.assertEqual(await asyncio.wait_for(process.wait(), 3), 1)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
