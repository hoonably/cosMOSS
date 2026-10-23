"""Batch submission and background cancellation through the real HTTP routes."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

from src.devices import DeviceStore
from src.speech_jobs import SpeechJobs
from src.store import NoteStore
from src.summaries import Summaries


GPU_UUID = 'GPU-11111111-2222-3333-4444-555555555555'


class BatchNotesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cosmoss-batch-notes-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        with patch.dict(os.environ, {'MOSS_DATA_DIR': str(self.root / 'data')}):
            from src import main
        self.main = main
        self.store = NoteStore(self.root / 'data/notes.sqlite3')
        self.devices = DeviceStore(self.root / 'data/devices.sqlite3')
        self.queue = asyncio.Queue()
        self.tasks = {}
        for key, value in {
            'STORE': self.store, 'DEVICES': self.devices, 'QUEUE': self.queue,
            'UPLOAD_DIR': self.root / 'data/uploads',
            'SUMMARIES': Summaries(self.root / 'data/summaries'),
            'SPEECH': SpeechJobs(self.root / 'data/performance.sqlite3'),
            'TRANSCRIPTION_TASKS': self.tasks, 'CANCEL_REQUESTED': set(),
            'NOTE_ACTION_LOCKS': {},
        }.items():
            self.enterContext(patch.object(main, key, value))
        self.device = self.devices.add_ssh(name='Fixture GPU', host='fixture-host', remote_port=18400,
            backend='cuda', model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
        self.devices.configure(self.device['id'], model_selected=True, model_key='cuda-original',
                               options={'speech_pipeline': 'moss-sglang'})
        self.gpu = {'name': 'NVIDIA H200', 'uuid': GPU_UUID, 'index': 0,
                    'memory_total_mib': 143771, 'memory_used_mib': 0,
                    'idle_candidate': True, 'process_info_known': True, 'processes': []}
        self.probe = self.enterContext(patch.object(main, 'probe_host',
            new=AsyncMock(return_value={'gpus': [self.gpu]})))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                        base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    def upload(self, *, duration=60, status='ready', name='fixture.wav'):
        identity = uuid.uuid4().hex
        audio = self.store.upload_directory(identity) / name
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b'synthetic audio; inference is never executed')
        self.store.create_upload({'id': identity, 'original_filename': name,
            'source_path': str(audio), 'normalized_path': str(audio),
            'status': status, 'duration': duration})
        return identity

    def body(self, identities, **changes):
        return {'items': [{'upload_id': identity} for identity in identities],
                'device_id': self.device['id'], 'gpu_uuid': GPU_UUID,
                'speech_pipeline': 'moss-sglang', **changes}

    def queued_ids(self):
        result = []
        while not self.queue.empty():
            result.append(self.queue.get_nowait())
            self.queue.task_done()
        return result

    async def eventually(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0)

    async def test_same_gpu_snapshot_is_read_once_and_no_job_starts_before_batch_is_consumed(self):
        first, second = self.upload(), self.upload(duration=120, name='second.wav')
        consumed = []
        consume = self.store.consume_upload

        def observe_consume(identity, settings):
            self.assertTrue(self.queue.empty(), 'Earlier batch items must not start while later ones validate')
            consumed.append(identity)
            result = consume(identity, settings)
            self.assertTrue(self.queue.empty())
            return result

        with patch.object(self.store, 'consume_upload', side_effect=observe_consume), \
             patch.object(self.main, 'transcribe', new=AsyncMock()) as inference:
            response = await self.client.post('/api/notes/batch', json=self.body([first, second],
                language='ko', hotwords='AI, Nemotron', output_format='vtt',
                output_directory=str(self.root / 'exports')))
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        self.assertFalse(result['errors'])
        self.assertEqual([entry['upload_id'] for entry in result['notes']], [first, second])
        self.assertEqual(consumed, [first, second])
        self.assertEqual(self.queued_ids(), [first, second])
        self.probe.assert_awaited_once_with('fixture-host')
        inference.assert_not_awaited()
        for identity in (first, second):
            note = self.store.get(identity)
            snapshot = json.loads(note['device_snapshot_json'])
            self.assertEqual(snapshot['gpu_uuid'], GPU_UUID)
            self.assertEqual(snapshot['options']['speech_pipeline'], 'moss-sglang')
            self.assertEqual(note['status'], 'queued')
            self.assertEqual(note['output_format'], 'vtt')
            self.assertEqual(note['hotwords'], 'AI, Nemotron')
            self.assertEqual(note['output_directory'], str(self.root / 'exports'))
            with self.assertRaises(KeyError):
                self.store.get_upload(identity)
        self.assertIsNone(self.main.BATCH_SUBMISSION.get())

    async def test_partial_failure_keeps_successful_jobs_and_unready_uploads_for_retry(self):
        first = self.upload()
        unready = self.upload(status='preparing')
        missing = uuid.uuid4().hex
        last = self.upload(name='last.wav')
        body = self.body([first, unready, missing, last])
        response = await self.client.post('/api/notes/batch', json=body)
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        self.assertEqual([entry['upload_id'] for entry in result['notes']], [first, last])
        self.assertEqual([(entry['upload_id'], entry['status_code']) for entry in result['errors']],
                         [(unready, 409), (missing, 404)])
        self.assertEqual(self.queued_ids(), [first, last])
        self.assertEqual(self.store.get_upload(unready)['status'], 'preparing')

        self.store.update_upload(unready, status='ready')
        retry = await self.client.post('/api/notes/batch', json=body)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual([entry['upload_id'] for entry in retry.json()['notes']], [unready])
        self.assertEqual({entry['upload_id'] for entry in retry.json()['errors']}, {first, missing, last})
        self.assertEqual(self.queued_ids(), [unready])
        self.assertEqual({note['id'] for note in self.store.list()}, {first, unready, last})

    async def test_retry_consumed_batch_never_creates_or_enqueues_duplicates(self):
        identities = [self.upload(), self.upload(name='two.wav')]
        body = self.body(identities)
        first = await self.client.post('/api/notes/batch', json=body)
        self.assertEqual(first.status_code, 202, first.text)
        retry = await self.client.post('/api/notes/batch', json=body)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(retry.json()['notes'], [])
        self.assertEqual([item['status_code'] for item in retry.json()['errors']], [404, 404])
        self.assertEqual(self.queued_ids(), identities)
        self.assertEqual(len(self.store.list()), 2)

    async def test_duplicate_ids_are_rejected_before_consumption_or_gpu_probe(self):
        identity = self.upload()
        response = await self.client.post('/api/notes/batch', json=self.body([identity, identity]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.get_upload(identity)['status'], 'ready')
        self.assertTrue(self.queue.empty())
        self.probe.assert_not_awaited()

    async def test_cross_origin_batch_cannot_consume_uploads_or_probe_server(self):
        identity = self.upload()
        response = await self.client.post('/api/notes/batch', json=self.body([identity]),
                                          headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.get_upload(identity)['status'], 'ready')
        self.assertTrue(self.queue.empty())
        self.probe.assert_not_awaited()

    async def test_mac_preserves_individual_chunk_counts_for_short_and_long_audio(self):
        short, long = self.upload(duration=120), self.upload(duration=3900, name='long.wav')
        response = await self.client.post('/api/notes/batch', json={
            'device_id': 'local', 'speech_pipeline': 'moss-original',
            'items': [{'upload_id': short, 'chunk_count': 1, 'title': '짧은 녹음'},
                      {'upload_id': long, 'chunk_count': 5, 'title': '긴 녹음'}],
        })
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()['errors'])
        self.assertEqual(self.store.get(short)['chunk_count'], 1)
        self.assertEqual(self.store.get(short)['chunk_seconds'], 120)
        self.assertEqual(self.store.get(long)['chunk_count'], 5)
        self.assertEqual(self.store.get(long)['chunk_seconds'], 780)
        self.assertEqual(self.store.get(short)['title'], '짧은 녹음')
        self.assertEqual(self.store.get(long)['title'], '긴 녹음')
        self.probe.assert_not_awaited()
        self.assertEqual(self.queued_ids(), [short, long])

    async def test_cancelling_one_queued_runner_keeps_other_batch_jobs_working(self):
        first, cancelled, last = [self.upload(name=f'{index}.wav') for index in range(3)]
        response = await self.client.post('/api/notes/batch', json=self.body([first, cancelled, last]))
        self.assertEqual(response.status_code, 202, response.text)
        started, gates = [], {identity: asyncio.Event() for identity in (first, cancelled, last)}

        async def fake_transcription(identity):
            started.append(identity)
            self.store.update(identity, status='processing')
            await gates[identity].wait()
            self.store.update(identity, status='done')

        with patch.object(self.main, 'transcribe', side_effect=fake_transcription):
            worker = asyncio.create_task(self.main.queue_worker())
            try:
                await self.eventually(lambda: started == [first] and cancelled in self.tasks)
                running_task = self.tasks[first]
                result = await self.client.post(f'/api/notes/{cancelled}/cancel')
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['status'], 'cancelled')
                self.assertEqual(self.store.get(first)['status'], 'processing')
                self.assertIs(self.tasks[first], running_task)
                self.assertFalse(running_task.cancelling())
                self.assertEqual(self.store.get(last)['status'], 'queued')
                gates[first].set()
                await self.eventually(lambda: started == [first, last])
                gates[last].set()
                await asyncio.wait_for(self.queue.join(), 2)
                self.assertEqual(self.store.get(first)['status'], 'done')
                self.assertEqual(self.store.get(last)['status'], 'done')
                self.assertEqual(self.store.get(cancelled)['status'], 'cancelled')
            finally:
                for gate in gates.values():
                    gate.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(self.tasks)


if __name__ == '__main__':
    unittest.main()
