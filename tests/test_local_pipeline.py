"""Mac uses the same speaker attribution and note publication as server jobs."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.devices import DeviceStore
from src.model_catalog import DEFAULT_OPTIONS, MLX_8BIT
from src.store import NoteStore
from src.summaries import Summaries


class LocalPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = NoteStore(self.root / 'notes.sqlite3')
        self.devices = DeviceStore(self.root / 'devices.sqlite3')
        self.device = self.devices.configure('local', model_key='mlx-8bit', model_id=MLX_8BIT,
            model_selected=True, options={**DEFAULT_OPTIONS, 'local_diarization': 'nemotron-3'})
        self.events = []
        @asynccontextmanager
        async def session(device, callback):
            self.events.append(('load', device['model_key']))
            callback({'phase': 'loading'})
            try:
                yield
            finally:
                self.events.append(('release', device['model_key']))
        self.models = SimpleNamespace(model_key='mlx-8bit', model_id=MLX_8BIT,
            saved={'model_key':'mlx-8bit', 'source':'cache'}, task=None,
            inference_session=session, readiness_for=Mock(return_value={
                'ready': True, 'asr_ready':True, 'diarization_ready':True}),
            model_path_for=Mock(return_value=self.root / 'model'),
            status=Mock(return_value={'phase':'installed'}), start=Mock(return_value={'phase':'checking'}))
        self.diarization = SimpleNamespace(execute=AsyncMock(side_effect=self.diarize))
        self.identity = 'a' * 32
        self.audio = self.store.upload_directory(self.identity) / 'source.wav'
        self.audio.parent.mkdir(parents=True)
        self.audio.write_bytes(b'fixture')
        self.store.create({'id':self.identity, 'title':'Mac meeting', 'original_filename':'source.wav',
            'source_path':str(self.audio), 'normalized_path':str(self.audio), 'duration':12,
            'device_id':'local', 'device_snapshot_json':json.dumps(self.device), 'chunk_seconds':6,
            'status':'queued', 'output_directory':str(self.root / 'exports')})
        self.chunks = [(self.audio, 0), (self.audio, 6)]
        self.asr = AsyncMock(side_effect=[
            ('first raw', [{'start':0, 'end':2, 'speaker':'S01', 'text':'첫 사람입니다.'},
                           {'start':2, 'end':4, 'speaker':'S01', 'text':'다른 사람입니다.'}]),
            ('second raw', [{'start':0, 'end':2, 'speaker':'S02', 'text':'다시 첫 사람입니다.'},
                            {'start':2, 'end':4, 'speaker':'S01', 'text':'겹쳐 말합니다.'}])])
        values = {'STORE':self.store, 'DEVICES':self.devices, 'LOCAL_MODELS':self.models,
            'LOCAL_DIARIZATION':self.diarization, 'QUEUE':asyncio.Queue(), 'CANCEL_REQUESTED':set(),
            'SUMMARIES':Summaries(self.root / 'summaries'),
            'VOICE_PROFILES':SimpleNamespace(settings=lambda:{'enabled':False}),
            'MEDIA_TOOLS':SimpleNamespace(require_ready=AsyncMock()),
            'split_audio':Mock(return_value=self.chunks), 'make_bridge_chunks':Mock(return_value=[]),
            'transcribe_chunk':self.asr, 'estimate_note_inference':Mock(return_value=30)}
        for key, value in values.items():
            self.enterContext(patch.object(main, key, value))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def diarize(self, key, action, callback, **kwargs):
        self.assertEqual(self.events[-1], ('release', 'mlx-8bit'))
        self.assertEqual(kwargs['audio'], self.audio)
        self.events.append(('diarize', key))
        callback({'phase':'running'})
        return {'result': {'turns':[{'start':0,'end':2,'speaker':'0'}, {'start':2,'end':4,'speaker':'1'},
            {'start':6,'end':10,'speaker':'0'}, {'start':8,'end':10,'speaker':'1'}],
            'load_seconds':1, 'inference_seconds':2, 'peak_host_rss_gib':.8, 'device':'mps'}}

    async def test_unloads_before_nemotron_and_publishes_same_conservative_attribution(self):
        await self.main.transcribe(self.identity)
        note = self.store.get(self.identity)
        self.assertEqual(note['status'], 'done', note.get('error'))
        self.assertEqual(self.events, [('load','mlx-8bit'), ('release','mlx-8bit'), ('diarize','nemotron-3')])
        self.assertEqual([s['speaker'] for s in note['segments']], ['S01','S02','S01','미확인'])
        self.assertTrue(note['segments'][-1]['speaker_uncertain'])
        self.assertEqual([s['text'] for s in note['segments']],
            ['첫 사람입니다.','다른 사람입니다.','다시 첫 사람입니다.','겹쳐 말합니다.'])
        self.assertEqual(note['segments'], note['raw_segments'])
        self.assertIn('다른 사람입니다.', Path(note['text_path']).read_text())
        self.assertEqual(note['pipeline_result']['pipeline'], 'moss-mlx')
        self.assertIsNone(note['pipeline_result']['peak_process_gib'])
        self.assertNotIn('asr_segments', note['pipeline_result'])
        diagnostic = json.loads((self.audio.parent / 'speech-result.json').read_text())
        self.assertEqual(diagnostic['asr_chunks'][0]['text'], 'first raw')
        self.assertEqual(diagnostic['asr_segments'][0]['speaker'], diagnostic['asr_segments'][1]['speaker'])
        self.assertEqual(diagnostic['diarization_metrics']['peak_host_rss_gib'], .8)
        self.assertIsNotNone(note['inference_finished_at'])

    async def test_diarizer_failure_keeps_asr_diagnostic_and_does_not_publish_success(self):
        self.diarization.execute.side_effect = RuntimeError('Nemotron failed')
        await self.main.transcribe(self.identity)
        note = self.store.get(self.identity)
        self.assertEqual(note['status'], 'error')
        self.assertIn('Nemotron failed', note['error'])
        self.assertFalse(note['text_path'])
        self.assertEqual(self.events[-1], ('release','mlx-8bit'))
        self.assertEqual(len(json.loads((self.audio.parent / 'speech-result.json').read_text())['asr_chunks']), 2)

    async def test_legacy_mac_job_still_launches_and_releases_asr_without_diarization(self):
        device = {**self.device, 'options': DEFAULT_OPTIONS}
        self.store.update(self.identity, device_snapshot_json=json.dumps(device))
        await self.main.transcribe(self.identity)
        note = self.store.get(self.identity)
        self.assertEqual(note['status'], 'done', note.get('error'))
        self.assertEqual(self.events, [('load','mlx-8bit'), ('release','mlx-8bit')])
        self.diarization.execute.assert_not_awaited()

    async def test_cancel_during_diarization_retains_cancel_state_without_export(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.diarization.execute.side_effect = pending
        task = asyncio.create_task(self.main.transcribe(self.identity))
        await asyncio.wait_for(entered.wait(), 2)
        self.main.CANCEL_REQUESTED.add(self.identity)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.store.get(self.identity)['status'], 'cancelled')
        self.assertFalse(self.store.get(self.identity)['text_path'])
        self.assertEqual(self.events[-1], ('release','mlx-8bit'))

    async def test_per_note_model_snapshot_does_not_change_device_default(self):
        self.store.update(self.identity, status='done')
        upload_id = 'b' * 32
        source = self.store.upload_directory(upload_id) / 'audio.wav'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'fixture')
        self.store.create_upload({'id':upload_id, 'status':'ready','original_filename':'audio.wav',
            'source_path':str(source), 'normalized_path':str(source), 'duration':60})
        response = await self.client.post('/api/notes', data={'upload_id':upload_id, 'device_id':'local',
            'local_model_key':'mlx-16bit', 'chunk_count':2})
        self.assertEqual(response.status_code, 202, response.text)
        snapshot = json.loads(self.store.get(upload_id)['device_snapshot_json'])
        self.assertEqual(snapshot['model_key'], 'mlx-16bit')
        self.assertEqual(snapshot['options']['local_diarization'], 'nemotron-3')
        self.assertEqual(snapshot['model_source'], 'cache')
        self.assertEqual(self.devices.get('local')['model_key'], 'mlx-8bit')
        self.assertEqual(self.models.saved['model_key'], 'mlx-8bit')

    async def test_per_note_preparation_is_explicit_and_temporary(self):
        self.store.update(self.identity, status='done')
        response = await self.client.get('/api/devices/local/model-readiness?model_key=mlx-16bit')
        self.assertTrue(response.json()['ready'])
        self.models.start.assert_not_called()
        response = await self.client.post('/api/devices/local/model-readiness', json={'model_key':'mlx-16bit'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(self.models.start.call_args.kwargs['remember'])
        self.assertEqual(self.models.start.call_args.kwargs['model_key'], 'mlx-16bit')
        self.assertEqual(self.devices.get('local')['model_key'], 'mlx-8bit')
