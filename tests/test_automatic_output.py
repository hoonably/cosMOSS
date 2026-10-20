"""Save the selected result format without a browser or a second export step."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.config import TranscriptionConfig
from src.devices import DeviceStore, ResolvedDevice
from src.store import NoteStore


class AutomaticOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = NoteStore(self.root / 'data/notes.sqlite3')
        for key, value in {'STORE': self.store, 'DEVICES': DeviceStore(self.root / 'devices.sqlite3'),
                           'UPLOAD_DIR': self.root / 'data/uploads', 'QUEUE': asyncio.Queue()}.items():
            self.enterContext(patch.object(main, key, value))
        self.enterContext(patch.object(main.MEDIA_TOOLS, 'require_ready', new=AsyncMock()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def create(self, format_name='txt', directory=None, headers=None):
        return await self.client.post('/api/notes', data={'title': '회의', 'output_format': format_name,
                                      'output_directory': str(directory or self.root / 'output')},
                                      files={'file': ('recording.m4a', b'audio', 'audio/mp4')}, headers=headers)

    async def run_inference(self, note_id):
        source = self.store.resolve_path(note_id, self.store.get(note_id)['source_path'])
        target = ResolvedDevice(TranscriptionConfig.from_env({}), 'moss', '이 Mac', 'local')
        segments = [{'id': 0, 'start': 1, 'end': 3, 'speaker': 'S01', 'text': '회의 내용입니다.'}]
        with patch.object(self.main, 'resolve_device', new=AsyncMock(return_value=target)), \
             patch.object(self.main, 'run_audio_work', new=AsyncMock(side_effect=[None, 4., [(source, 0.)], []])), \
             patch.object(self.main, 'transcribe_chunk', new=AsyncMock(return_value=('[1][S01]회의 내용입니다.[3]', segments))) as inference:
            await self.main.transcribe(note_id)
            self.assertEqual(inference.await_count, 1)
        return self.store.get(note_id)

    async def test_selected_format_and_directory_are_saved_on_completion_and_edits_use_same_file(self):
        for format_name in ('txt', 'srt', 'vtt'):
            with self.subTest(format=format_name):
                folder = self.root / format_name
                folder.mkdir()
                original = folder / f'회의.{format_name}'
                original.write_text('이전 파일은 보존합니다.')
                response = await self.create(format_name, folder)
                self.assertEqual(response.status_code, 202, response.text)
                note = await self.run_inference(response.json()['id'])
                self.assertEqual(note['status'], 'done', note.get('error'))
                self.assertEqual(note['output_directory'], str(folder))
                self.assertFalse(note['text_save_error'])
                result = Path(note['text_path'])
                self.assertEqual(result.parent, folder)
                self.assertEqual(result.suffix, '.' + format_name)
                self.assertNotEqual(result, original)
                self.assertIn('회의 내용입니다.', result.read_text())
                self.assertEqual(original.read_text(), '이전 파일은 보존합니다.')
                response = await self.client.post(f"/api/notes/{note['id']}/speakers", json={'speaker_names': {'S01': '민수'}})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['text_path'], str(result))
                self.assertIn('민수', result.read_text())
                self.assertEqual(len(list(folder.iterdir())), 2)

    async def test_failed_save_keeps_completed_transcript_and_retry_does_not_repeat_inference(self):
        response = await self.create('srt')
        note_id = response.json()['id']
        with patch.object(self.main, 'write_text', side_effect=PermissionError('쓰기 권한 없음')):
            note = await self.run_inference(note_id)
        self.assertEqual(note['status'], 'done')
        self.assertEqual(len(note['segments']), 1)
        self.assertIn('쓰기 권한 없음', note['text_save_error'])
        self.assertFalse(note['text_path'])
        with patch.object(self.main, 'transcribe_chunk', new=AsyncMock()) as inference:
            response = await self.client.post(f'/api/notes/{note_id}/text-file', json={'directory': str(self.root / 'recovered')})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['text_save_error'])
            self.assertTrue(Path(response.json()['text_path']).is_file())
            inference.assert_not_awaited()

    async def test_invalid_directory_and_cross_site_requests_do_not_write_audio(self):
        for directory in ('relative/path', '/bad\x00path'):
            response = await self.create(directory=directory)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.create(headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.main.UPLOAD_DIR.exists())
