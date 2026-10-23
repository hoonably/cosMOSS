"""Estimated model progress boundaries, without inference or model downloads."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import main
from src.store import NoteStore


class InferenceProgressTests(unittest.TestCase):
    def test_job_estimate_uses_selected_gpu_snapshot_and_chunk_count(self):
        note = {'device_snapshot_json': json.dumps({'backend':'cuda', 'model_key':'cuda-original',
            'options':{}, 'gpu_hardware':{'name':'NVIDIA H200', 'memory_total_mib':143771}})}
        self.assertEqual(main.estimate_note_inference(note, 3937.1306875, 1), 529)
        self.assertEqual(main.estimate_note_inference(note, 3937.1306875, 2), 428)
        with patch.object(main.PERFORMANCE, 'profiles', side_effect=OSError('history unavailable')):
            self.assertIsNone(main.estimate_note_inference(note, 3937.1306875, 1))

    def test_model_clock_freezes_before_voice_matching_and_saving(self):
        note = {'status':'processing', 'progress_phase':'identifying_speakers',
                'processing_started_at':'2026-10-21T00:00:00+00:00',
                'inference_started_at':'2026-10-21T00:01:00+00:00',
                'inference_finished_at':'2026-10-21T00:09:49+00:00'}
        self.assertEqual(main.public_note(note)['inference_elapsed_seconds'], 529)
        self.assertEqual(main.public_note({**note, 'status':'done', 'processing_seconds':900})['inference_elapsed_seconds'], 529)

    def test_recovery_resets_model_clock_and_estimate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = NoteStore(Path(folder) / 'notes.sqlite3')
            note = store.create({'id':'fixture', 'status':'processing', 'title':'test',
                                 'original_filename':'audio.wav', 'source_path':str(Path(folder) / 'audio.wav')})
            store.update(note['id'], inference_started_at='2026-10-21T00:00:00+00:00',
                         inference_finished_at='2026-10-21T00:02:00+00:00', estimated_inference_seconds=100)
            self.assertEqual(store.pending_ids(), ['fixture'])
            reset = store.get(note['id'])
            for key in ('inference_started_at', 'inference_finished_at', 'estimated_inference_seconds'):
                self.assertIsNone(reset[key])


class InferenceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_clock_finishes_before_optional_voice_comparison(self):
        from tests.test_automatic_output import AutomaticOutputTests
        case = AutomaticOutputTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addAsyncCleanup(case.client.aclose)
        created = await case.create()
        async def identify(*args):
            note = case.store.get(created.json()['id'])
            self.assertEqual(note['progress_phase'], 'identifying_speakers')
            self.assertIsNotNone(note['inference_finished_at'])
            self.assertGreater(note['estimated_inference_seconds'], 0)
            self.assertLessEqual(datetime.fromisoformat(note['inference_started_at']), datetime.fromisoformat(note['inference_finished_at']))
            return {'matches':[], 'merge_candidates':[]}
        with patch.object(main.VOICE_PROFILES, 'documents', return_value=[{}]), \
             patch.object(main.VOICE_PROFILES, 'settings', return_value={'enabled':True}), \
             patch.object(main.VOICE_PROFILES, 'identify', new=AsyncMock(side_effect=identify)):
            note = await case.run_inference(created.json()['id'])
        self.assertEqual(note['status'], 'done', note.get('error'))
        self.assertIsNone(note['voice_match_error'])
        self.assertIsNotNone(note['inference_finished_at'])
