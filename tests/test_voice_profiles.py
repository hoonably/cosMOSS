"""Profile file boundaries and optional matching; no model download or inference."""
import asyncio
import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.voice_engine import VoiceEngine, MODEL_ID
from src.voice_profiles import VoiceProfiles, MAX_PROFILE_BYTES, unit_vector


def sample():
    # Format fixture only. Functional inference uses an actual recording.
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x01' * 16000)
    return output.getvalue()


class VoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = VoiceEngine(self.root)
        self.profiles = VoiceProfiles(self.root, self.engine)
        self.vector = [1.] + [0.] * 191

    def test_portable_roundtrip_uses_local_ids_and_preserves_sample(self):
        item = self.profiles.store('정훈', self.vector, sample())
        content = self.profiles.export(item['id'])
        self.profiles.delete(item['id'])
        imported = self.profiles.import_file(content)
        self.assertNotEqual(item['id'], imported['id'])
        self.assertEqual(self.profiles.import_file(content), imported)
        self.assertEqual(self.profiles.get(imported['id'])['sample_wav'], base64.b64encode(sample()).decode())
        self.profiles.rename(imported['id'], '재민')
        self.assertEqual(VoiceProfiles(self.root, self.engine).listing()['profiles'][0]['name'], '재민')
        self.assertNotIn('sample_wav', self.profiles.listing()['profiles'][0])
        self.assertEqual(self.profiles.database.stat().st_mode & 0o777, 0o600)

    def test_rejects_corrupt_oversized_foreign_and_nonfinite_profiles(self):
        item = self.profiles.store('정훈', self.vector, sample())
        valid = json.loads(self.profiles.export(item['id']))
        for changes in ({'version': True}, {'version': 2}, {'model_id': 'remote-model'},
                        {'sample_sha256': 'bad'}, {'sample_wav': 'bad%'},
                        {'embedding': [float('nan')] * 192}, {'embedding': [0.] * 192},
                        {'name': 'a\nb'}, {'sample_wav': 7}, {'embedding': [True] * 192}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self.profiles.import_file(json.dumps({**valid, **changes}).encode())
        for content in (b'[]', b'not json', b'x' * (MAX_PROFILE_BYTES + 1)):
            with self.assertRaises(ValueError): self.profiles.import_file(content)
        self.assertEqual(len(self.profiles.documents()), 1)

    def test_threshold_margin_grouping_and_settings_persist(self):
        a = self.profiles.store('정훈', self.vector, sample())
        b = self.profiles.store('재민', [0.99, 0.1] + [0.] * 190, sample())
        self.assertFalse(self.profiles.compare({'S01': self.vector})['suggested_names'])
        self.profiles.delete(b['id'])
        self.assertEqual(self.profiles.compare({'S01': self.vector})['suggested_names'], {'S01': '정훈'})
        self.assertFalse(self.profiles.compare({'S01': [0., 1.] + [0.] * 190})['suggested_names'])
        self.profiles.configure(False, .88)
        self.assertEqual(VoiceProfiles(self.root, self.engine).settings()['threshold'], .88)
        self.assertFalse(self.profiles.settings()['enabled'])
        with self.assertRaises(ValueError): self.profiles.configure(True, float('nan'))
        self.assertEqual(self.profiles.get(a['id'])['model_id'], MODEL_ID)

    def test_merge_proposals_work_without_profiles_and_skip_overlapping_speakers(self):
        vectors = {'S01': self.vector, 'S02': self.vector, 'S03': [0., 1.] + [0.] * 190}
        result = self.profiles.compare(vectors, profiles=[])
        self.assertEqual(result['matches'], [])
        self.assertEqual([(item['source'], item['target']) for item in result['merge_candidates']], [('S02', 'S01')])
        self.assertTrue(result['merge_candidates'][0]['recommended'])
        self.assertEqual(result['suggested_names'], {})
        self.assertFalse(self.profiles.compare(vectors, [], overlapping_pairs={frozenset(('S01', 'S02'))})['merge_candidates'])

    def test_mixed_samples_are_marked_for_additional_review(self):
        self.profiles.store('정훈', self.vector, sample())
        different = [0., 1.] + [0.] * 190
        result = self.profiles.compare({'S01': self.vector, 'S02': self.vector},
            sample_vectors={'S01': [self.vector, different], 'S02': [self.vector]})
        self.assertFalse(result['matches'][0]['recommended'])
        self.assertFalse(result['merge_candidates'][0]['recommended'])
        self.assertEqual(result['matches'][0]['sample_similarity_min'], 0)
        self.assertNotIn('accepted', result['matches'][0])

    def test_ranges_skip_overlap_and_short_speech_and_bound_samples(self):
        segments = [{'speaker': 'S01', 'start': 0., 'end': 10.},
                    {'speaker': 'S02', 'start': 2., 'end': 8.},
                    {'speaker': 'S03', 'start': 20., 'end': 20.2}]
        self.assertEqual(self.profiles.ranges(segments), {'S01': [], 'S02': [], 'S03': []})
        segments = [{'speaker': 'S01', 'start': i * 20., 'end': i * 20. + 10} for i in range(8)]
        self.assertEqual(len(self.profiles.ranges(segments)['S01']), 3)
        self.assertTrue(all(seconds == 5 for _, seconds in self.profiles.ranges(segments)['S01']))


class VoiceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src import main
        self.main = main
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = VoiceEngine(self.root)
        self.profiles = VoiceProfiles(self.root, self.engine)
        self.enterContext(patch.object(main, 'VOICE_ENGINE', self.engine))
        self.enterContext(patch.object(main, 'VOICE_PROFILES', self.profiles))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def test_setup_runs_in_event_loop_and_origin_is_guarded(self):
        with patch.object(self.engine, 'prepare', new=AsyncMock()) as prepare:
            response = await self.client.post('/api/voice-profiles/setup')
            self.assertEqual(response.status_code, 202, response.text)
            await self.engine.task
            prepare.assert_awaited_once()
        for path in ('/api/voice-profiles', '/api/voice-profiles/setup'):
            response = await self.client.request('GET' if path.endswith('profiles') else 'POST', path,
                                                 headers={'Origin': 'https://other.example'})
            self.assertEqual(response.status_code, 403)
        response = await self.client.get('/')
        self.assertIn('microphone=(self)', response.headers['Permissions-Policy'])
        self.assertIn('camera=()', response.headers['Permissions-Policy'])

    async def test_identification_without_enrollment_preserves_segments_and_returns_bounded_samples(self):
        segments = [{'speaker': speaker, 'start': start, 'end': start + 10}
                    for speaker, start in [('S01', 0), ('S02', 20), ('S03', 40)]]
        original = json.dumps(segments)
        async def clip(source, destination, start, seconds, ffmpeg):
            destination.write_bytes(sample())
            return sample()
        vectors = [[1.] + [0.] * 191, [1.] + [0.] * 191, [0., 1.] + [0.] * 190]
        with patch.object(self.engine, 'require_ready', new=AsyncMock()), \
             patch.object(self.profiles, 'clip', new=AsyncMock(side_effect=clip)), \
             patch.object(self.engine, 'embeddings', new=AsyncMock(return_value=vectors)):
            result = await self.profiles.identify(self.root / 'source.wav', segments, 'ffmpeg')
        self.assertEqual(json.dumps(segments), original)
        self.assertEqual(result['matches'], [])
        self.assertEqual(len(result['merge_candidates']), 1)
        pair = result['merge_candidates'][0]
        self.assertEqual(pair['source_samples'], [{'start': 22.5, 'end': 27.5}])
        self.assertEqual(pair['target_samples'], [{'start': 2.5, 'end': 7.5}])

    async def test_crud_and_import_have_no_unexpected_download(self):
        item = self.profiles.store('정훈', [1.] + [0.] * 191, sample())
        response = await self.client.get(f"/api/voice-profiles/{item['id']}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn('.mossvoice', response.headers['Content-Disposition'])
        content = response.content
        self.assertEqual((await self.client.delete(f"/api/voice-profiles/{item['id']}")).status_code, 204)
        with patch.object(self.engine, 'ready', return_value=True), patch.object(self.engine, 'start') as start:
            response = await self.client.post('/api/voice-profiles/import', files={'file': ('voice.mossvoice', content)})
            self.assertEqual(response.status_code, 200, response.text)
            start.assert_not_called()
        profile_id = response.json()['id']
        response = await self.client.patch(f'/api/voice-profiles/{profile_id}', json={'name': '재민'})
        self.assertEqual(response.json()['name'], '재민')
        response = await self.client.get(f'/api/voice-profiles/{profile_id}/sample')
        self.assertEqual(response.content, sample())


class VoiceFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_only_stores_proposals_and_preview_does_not_modify_note_or_file(self):
        from tests.test_automatic_output import AutomaticOutputTests
        case = AutomaticOutputTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addAsyncCleanup(case.client.aclose)
        response = await case.create('txt')
        main = case.main
        proposal = {'suggested_names': {'S01': '정훈'},
                    'matches': [{'speaker': 'S01', 'name': '정훈', 'similarity': .96, 'recommended': True}],
                    'merge_candidates': [{'source': 'S02', 'target': 'S01', 'similarity': .91, 'recommended': True}]}
        with patch.object(main.VOICE_PROFILES, 'documents', return_value=[{'name': '등록된 목소리'}]), \
             patch.object(main.VOICE_PROFILES, 'settings', return_value={'enabled': True}), \
             patch.object(main.VOICE_PROFILES, 'identify', new=AsyncMock(return_value=proposal)):
            note = await case.run_inference(response.json()['id'])
            self.assertEqual(note['speaker_names'], {'S01': 'S01'})
            self.assertEqual(note['speaker_merges'], {})
            self.assertEqual(note['voice_matches'], proposal['matches'])
            self.assertEqual(note['voice_merge_candidates'], proposal['merge_candidates'])
            saved = Path(note['text_path']).read_bytes()
            self.assertNotIn('정훈', saved.decode())
            case.store.resolve_path(note['id'], note['normalized_path']).write_bytes(b'fixture audio')
            result = await case.client.post(f"/api/notes/{note['id']}/identify-speakers", json={})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), proposal)
            self.assertEqual(case.store.get(note['id'])['speaker_names'], {'S01': 'S01'})
            self.assertEqual(Path(note['text_path']).read_bytes(), saved)
        result = await case.client.post(f"/api/notes/{note['id']}/speakers", json={'speaker_names': {'S01': '정훈'}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn('정훈', Path(note['text_path']).read_text())

    async def test_matching_failure_keeps_transcript_and_saved_result(self):
        from tests.test_automatic_output import AutomaticOutputTests
        case = AutomaticOutputTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addAsyncCleanup(case.client.aclose)
        response = await case.create('txt')
        main = case.main
        with patch.object(main.VOICE_PROFILES, 'documents', return_value=[{'name': '등록된 목소리'}]), \
             patch.object(main.VOICE_PROFILES, 'settings', return_value={'enabled': True}), \
             patch.object(main.VOICE_PROFILES, 'identify', new=AsyncMock(side_effect=RuntimeError('비교 도구 없음'))):
            note = await case.run_inference(response.json()['id'])
        self.assertEqual(note['status'], 'done')
        self.assertEqual(note['speaker_names'], {'S01': 'S01'})
        self.assertIn('비교 도구 없음', note['voice_match_error'])
        self.assertTrue(Path(note['text_path']).is_file())

