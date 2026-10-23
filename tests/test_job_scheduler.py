"""Exercise real asyncio dispatch/cancellation without models or personal data."""
import asyncio
import json
import unittest

from src.job_scheduler import run_transcription_queue, transcription_resources


def cuda_note(identity, gpu='GPU-aaaa1111', pipeline='moss-sglang', host='server'):
    return {'id': identity, 'status': 'queued', 'device_snapshot_json': json.dumps({
        'id': host, 'kind': 'ssh', 'backend': 'cuda', 'host': host,
        'remote_port': 8001, 'gpu_uuid': gpu, 'options': {'speech_pipeline': pipeline},
    })}


class ResourceTests(unittest.TestCase):
    def test_gpu_identity_is_shared_across_aliases_and_pipelines(self):
        first = transcription_resources(cuda_note('a'))
        second = transcription_resources(cuda_note('b', host='other-alias', pipeline='qwen-transformers'))
        self.assertEqual(first, second)
        self.assertFalse(first & transcription_resources(cuda_note('c', gpu='GPU-bbbb2222')))

    def test_legacy_cuda_workers_share_a_slot_and_their_physical_gpu(self):
        first = transcription_resources(cuda_note('a', pipeline='moss-original'))
        second = transcription_resources(cuda_note('b', gpu='GPU-bbbb2222', pipeline='custom'))
        self.assertTrue(first & second)
        self.assertTrue(first & transcription_resources(cuda_note('c')))

    def test_local_mac_and_unknown_destinations_are_conservative(self):
        note = {'device_snapshot_json': json.dumps({'id': 'local', 'kind': 'local', 'backend': 'mlx'})}
        self.assertEqual(transcription_resources(note), frozenset({'local-mac'}))
        for snapshot in ('', '{bad', '[]', '{"backend":"cuda"}', '{"backend":"other"}'):
            self.assertEqual(transcription_resources({'device_snapshot_json': snapshot}), frozenset({'all-resources'}))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue = asyncio.Queue()
        self.notes, self.tasks, self.errors = {}, {}, []
        self.cancel_requested = set()
        self.started = []
        self.releases = {}
        self.dispatcher = None

    async def asyncTearDown(self):
        for release in self.releases.values():
            release.set()
        if self.dispatcher:
            self.dispatcher.cancel()
            await asyncio.gather(self.dispatcher, return_exceptions=True)
        self.assertFalse(self.tasks)

    async def eventually(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(0)

    def add(self, identity, **options):
        self.notes[identity] = cuda_note(identity, **options)
        self.releases[identity] = asyncio.Event()
        self.queue.put_nowait(identity)

    async def transcribe(self, identity):
        self.started.append(identity)
        self.notes[identity]['status'] = 'processing'
        await self.releases[identity].wait()
        self.notes[identity]['status'] = 'done'

    def start(self, transcribe=None, **options):
        self.dispatcher = asyncio.create_task(run_transcription_queue(
            self.queue, get_note=self.notes.__getitem__, transcribe=transcribe or self.transcribe,
            tasks=self.tasks, cancel_requested=self.cancel_requested,
            on_error=lambda identity, error: self.errors.append((identity, str(error))), **options))

    async def joined(self):
        await asyncio.wait_for(self.queue.join(), 2)

    async def test_same_gpu_runs_fifo_while_other_gpu_starts_without_waiting(self):
        self.add('a')
        self.add('b')
        self.add('c', gpu='GPU-bbbb2222')
        self.start()
        await self.eventually(lambda: len(self.started) == 2)
        self.assertEqual(self.started, ['a', 'c'])
        self.assertEqual(self.notes['b']['status'], 'queued')
        self.assertEqual(set(self.tasks), {'a', 'b', 'c'})
        self.releases['a'].set()
        await self.eventually(lambda: 'b' in self.started)
        self.releases['b'].set()
        self.releases['c'].set()
        await self.joined()
        self.assertTrue(all(note['status'] == 'done' for note in self.notes.values()))

    async def test_global_sequential_option_ignores_distinct_gpus(self):
        self.add('a')
        self.add('b', gpu='GPU-bbbb2222')
        self.start(parallel_resources=False)
        await self.eventually(lambda: self.started)
        self.assertEqual(self.started, ['a'])
        self.releases['a'].set()
        await self.eventually(lambda: 'b' in self.started)
        self.releases['b'].set()
        await self.joined()

    async def test_cancelling_waiting_job_never_calls_transcription(self):
        self.add('a')
        self.add('b')
        self.add('c')
        self.start()
        await self.eventually(lambda: self.started == ['a'])
        self.cancel_requested.add('b')
        waiting = self.tasks['b']
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        # The API owns this status transition after the runner has stopped.
        self.assertEqual(self.notes['b']['status'], 'queued')
        self.notes['b']['status'] = 'cancelled'
        self.releases['a'].set()
        self.releases['c'].set()
        await self.joined()
        self.assertEqual(self.started, ['a', 'c'])
        self.assertNotIn('b', self.cancel_requested)

    async def test_immediate_cancel_before_coroutine_start_completes_queue_entry(self):
        requested = self.cancel_requested

        class ImmediatelyCancelled(dict):
            def __setitem__(self, identity, task):
                super().__setitem__(identity, task)
                requested.add(identity)
                task.cancel()

        self.tasks = ImmediatelyCancelled()
        self.add('a')
        self.start()
        await self.joined()
        self.assertFalse(self.started)
        self.assertEqual(self.notes['a']['status'], 'queued')
        self.assertFalse(self.tasks)
        self.assertFalse(self.cancel_requested)

    async def test_running_cancel_keeps_slot_until_cleanup_finishes(self):
        self.add('a')
        self.add('b')
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def transcribe(identity):
            try:
                await self.transcribe(identity)
            finally:
                if identity == 'a':
                    cleanup_started.set()
                    await cleanup_release.wait()

        self.start(transcribe)
        await self.eventually(lambda: self.started == ['a'])
        running = self.tasks['a']
        running.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        self.assertEqual(self.started, ['a'])
        cleanup_release.set()
        await asyncio.gather(running, return_exceptions=True)
        await self.eventually(lambda: 'b' in self.started)
        self.releases['b'].set()
        await self.joined()

    async def test_duplicates_deleted_and_nonqueued_notes_are_acknowledged(self):
        self.add('a')
        self.queue.put_nowait('a')
        self.queue.put_nowait('missing')
        self.notes['old'] = {'status': 'done'}
        self.queue.put_nowait('old')
        self.start()
        await self.eventually(lambda: self.started)
        self.releases['a'].set()
        await self.joined()
        self.assertEqual(self.started, ['a'])
        self.assertFalse(self.errors)

    async def test_failure_releases_resource_for_following_job(self):
        self.add('a')
        self.add('b')

        async def fail_first(identity):
            if identity == 'a':
                self.started.append(identity)
                raise RuntimeError('fixture error')
            await self.transcribe(identity)

        self.start(fail_first)
        await self.eventually(lambda: 'b' in self.started)
        self.assertEqual(self.errors, [('a', 'fixture error')])
        self.releases['b'].set()
        await self.joined()

    async def test_cancelled_job_can_be_requeued_without_old_task_removing_new_one(self):
        self.add('a')
        self.start()
        await self.eventually(lambda: self.started)
        cancelled = self.tasks['a']
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        self.notes['a']['status'] = 'queued'
        self.queue.put_nowait('a')
        await self.eventually(lambda: len(self.started) == 2)
        self.assertIsNot(self.tasks['a'], cancelled)
        self.releases['a'].set()
        await self.joined()
        self.assertEqual(self.started, ['a', 'a'])

    async def test_shutdown_cancels_all_resources_and_waits_for_existing_cleanup(self):
        self.add('a')
        self.add('b')
        self.add('c', gpu='GPU-bbbb2222')
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def transcribe(identity):
            try:
                await self.transcribe(identity)
            finally:
                if identity == 'a':
                    cleanup_started.set()
                    await cleanup_release.wait()

        self.start(transcribe)
        await self.eventually(lambda: len(self.started) == 2)
        self.tasks['a'].cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        self.dispatcher.cancel()
        await asyncio.sleep(0)
        self.assertFalse(self.dispatcher.done())
        self.assertEqual(self.notes['b']['status'], 'queued')
        cleanup_release.set()
        await asyncio.gather(self.dispatcher, return_exceptions=True)
        await self.joined()
        self.assertEqual(self.started, ['a', 'c'])
        self.assertFalse(self.tasks)

    async def test_unknown_destination_waits_exclusively_without_starvation(self):
        self.add('a')
        self.add('old')
        self.notes['old'].pop('device_snapshot_json')
        self.add('c', gpu='GPU-bbbb2222')
        self.start()
        await self.eventually(lambda: self.started)
        self.assertEqual(self.started, ['a'])
        self.releases['a'].set()
        await self.eventually(lambda: 'old' in self.started)
        self.assertEqual(self.started, ['a', 'old'])
        self.releases['old'].set()
        await self.eventually(lambda: 'c' in self.started)
        self.releases['c'].set()
        await self.joined()


if __name__ == '__main__':
    unittest.main()
