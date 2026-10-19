"""Existing user TXT files stay in place, preserve their bytes, and reject stale edits."""
import asyncio
import codecs
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.store import NoteStore
from src.summaries import Summaries
from src.text_files import TextConflict
from src.text_library import TextLibrary


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / 'transcripts'
        self.folder.mkdir()
        self.library = TextLibrary(self.root / 'index.sqlite3')

    def test_scan_all_txt_without_copying_or_modifying_and_reopen_index(self):
        paths = [self.folder / name for name in ('old recording.txt', '한국어.TXT', 'empty.txt')]
        for path in paths:
            path.write_text('original\n' if path.stem != 'empty' else '')
        (self.folder / 'audio.wav').touch()
        (self.folder / 'nested.txt').mkdir()
        outside = self.root / 'outside.txt'
        outside.write_text('outside')
        (self.folder / 'shortcut.txt').symlink_to(outside)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        notes = self.library.scan(str(self.folder))
        self.assertEqual(len(notes), 3)
        self.assertNotIn('text_body', notes[0])
        again = TextLibrary(self.library.database).scan(str(self.folder))
        self.assertEqual({n['id'] for n in notes}, {n['id'] for n in again})
        for note in notes:
            opened = self.library.get(note['id'])
            self.assertEqual(opened['source_type'], 'text_file')
            path = Path(opened['text_path'])
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before[path.name])

    def test_managed_txt_is_not_duplicated_and_files_refresh_from_disk(self):
        file = self.folder / 'note.txt'
        file.write_text('before')
        self.assertEqual(self.library.scan(str(self.folder), [str(file)]), [])
        note = self.library.scan(str(self.folder))[0]
        file.write_text('after')
        self.assertEqual(self.library.get(note['id'])['text_body'], 'after')
        file.unlink()
        self.assertEqual(self.library.scan(str(self.folder)), [])
        with self.assertRaises(ValueError):
            self.library.get(note['id'])

    def test_edits_preserve_bom_crlf_and_permissions_and_reject_outside_changes(self):
        file = self.folder / 'note.txt'
        file.write_bytes(codecs.BOM_UTF8 + '첫 줄\r\n둘째 줄\r\n'.encode())
        file.chmod(0o640)
        note = self.library.get(self.library.scan(str(self.folder))[0]['id'])
        saved = self.library.update(note['id'], {'text_body': '수정\n둘째 줄\n'}, note['text_hash'])
        self.assertEqual(file.read_bytes(), codecs.BOM_UTF8 + '수정\r\n둘째 줄\r\n'.encode())
        self.assertEqual(file.stat().st_mode & 0o777, 0o640)
        file.write_text('edited elsewhere')
        with self.assertRaises(TextConflict):
            self.library.update(note['id'], {'text_body': 'stale draft'}, saved['text_hash'])
        self.assertEqual(file.read_text(), 'edited elsewhere')
        self.assertEqual(list(self.folder.glob('.cosmoss-*')), [])

    def test_utf16_and_cp949_files_keep_original_encoding(self):
        for encoding, prefix in [('utf-16-le', codecs.BOM_UTF16_LE), ('utf-16-be', codecs.BOM_UTF16_BE), ('cp949', b'')]:
            file = self.folder / (encoding + '.txt')
            file.write_bytes(prefix + '기존 내용'.encode(encoding))
            note_id = next(n['id'] for n in self.library.scan(str(self.folder)) if n['text_path'] == str(file))
            note = self.library.get(note_id)
            self.assertEqual(note['text_body'], '기존 내용')
            self.library.update(note_id, {'text_body': '바꾼 내용'}, note['text_hash'])
            self.assertEqual(file.read_bytes(), prefix + '바꾼 내용'.encode(encoding))

    def test_speakers_change_only_labels_and_keep_timestamps_and_whitespace(self):
        file = self.folder / 'speakers.txt'
        original = '[00:00:00] 00: first\ncontinuation\n\n[00:01:23] 01: second\n'
        file.write_text(original)
        note = self.library.get(self.library.scan(str(self.folder))[0]['id'])
        self.assertEqual(len(note['segments']), 2)
        self.library.update(note['id'], {'speaker_names': {'S01': '지민'}}, note['text_hash'])
        self.assertEqual(file.read_text(), original.replace('] 00:', '] 지민:'))

    def test_merge_changes_only_headers_and_preserves_encoding_and_multiline_body(self):
        file = self.folder / 'merge.txt'
        original = '[00:00:01] 지민: 첫 발언\r\n이 문장의 S02: 표기는 본문입니다.\r\n\r\n[00:00:05] 수빈: 다음 발언\r\n'
        file.write_bytes(codecs.BOM_UTF16_LE + original.encode('utf-16-le'))
        note = self.library.get(self.library.scan(str(self.folder))[0]['id'])
        saved = self.library.update(note['id'], {'speaker_names':{'S01':'민수'},'speaker_merges':{'S02':'S01'}}, note['text_hash'])
        expected = original.replace('] 지민:', '] 민수:').replace('] 수빈:', '] 민수:')
        self.assertEqual(file.read_bytes(), codecs.BOM_UTF16_LE + expected.encode('utf-16-le'))
        self.assertEqual(saved['speaker_names'], {'S01':'민수'})
        self.assertEqual({s['speaker'] for s in saved['segments']}, {'S01'})
        with self.assertRaises(TextConflict):
            self.library.update(note['id'], {'speaker_names':{},'speaker_merges':{'S02':'S01'}}, note['text_hash'])

    def test_symlink_replacement_unknown_ids_and_stale_writes_are_rejected(self):
        file = self.folder / 'note.txt'
        file.write_text('original')
        note = self.library.get(self.library.scan(str(self.folder))[0]['id'])
        outside = self.root / 'outside.txt'
        outside.write_text('protected')
        file.unlink()
        file.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.library.update(note['id'], {'text_body': 'overwrite'}, note['text_hash'])
        self.assertEqual(outside.read_text(), 'protected')
        with self.assertRaises(KeyError):
            self.library.get('txt_unknown')


class LibraryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src import main
        self.main = main
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / 'files'
        self.folder.mkdir()
        self.file = self.folder / 'old meeting.txt'
        self.file.write_text('[00:00:01] S01: first\n[00:00:03] S02: second\n')
        self.library = TextLibrary(self.root / 'library.sqlite3')
        self.store = NoteStore(self.root / 'data/notes.sqlite3')
        self.summaries = Summaries(self.root / 'summaries')
        for attribute, value in {'TEXT_LIBRARY': self.library, 'STORE': self.store, 'SUMMARIES': self.summaries}.items():
            self.enterContext(patch.object(main, attribute, value))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def opened(self):
        response = await self.client.get('/api/text-library', params={'directory': str(self.folder)})
        self.assertEqual(response.status_code, 200, response.text)
        note_id = response.json()[0]['id']
        return (await self.client.get('/api/notes/' + note_id)).json()

    async def test_index_open_edit_export_and_finder_path(self):
        original = self.file.read_bytes()
        note = await self.opened()
        self.assertEqual(note['text_path'], str(self.file))
        response = await self.client.post('/api/notes/' + note['id'] + '/text-file', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.file.read_bytes(), original)
        response = await self.client.patch('/api/notes/' + note['id'], json={'text_body': 'edited text\n', 'text_hash': note['text_hash']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.file.read_text(), 'edited text\n')
        response = await self.client.get('/api/notes/' + note['id'] + '/export/txt')
        self.assertEqual(response.text, 'edited text\n')
        self.assertEqual((await self.client.delete('/api/notes/' + note['id'])).status_code, 409)
        self.assertTrue(self.file.exists())

    async def test_speaker_save_uses_file_hash_and_external_changes_conflict(self):
        note = await self.opened()
        response = await self.client.post('/api/notes/' + note['id'] + '/speakers', json={'speaker_names': {'S01': '지민'}, 'text_hash': note['text_hash']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('지민: first', self.file.read_text())
        response = await self.client.patch('/api/notes/' + note['id'], json={'text_body': 'stale', 'text_hash': note['text_hash']})
        self.assertEqual(response.status_code, 409)
        self.assertIn('지민: first', self.file.read_text())

    async def test_merge_route_for_existing_txt_keeps_the_same_file(self):
        note = await self.opened()
        response = await self.client.post('/api/notes/'+note['id']+'/speakers', json={
            'speaker_names':{'S02':'수빈'},'speaker_merges':{'S01':'S02'},'text_hash':note['text_hash']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['text_path'], str(self.file))
        self.assertEqual(self.file.read_text(), '[00:00:01] 수빈: first\n[00:00:03] 수빈: second\n')
        self.assertEqual(len(response.json()['speaker_names']), 1)

    async def test_summary_uses_existing_file_without_normalization_or_extra_export(self):
        original = self.file.read_bytes()
        note = await self.opened()
        with patch.object(self.main, 'provider_status', new=AsyncMock(return_value={'ready': True})), patch.object(self.summaries, 'start', return_value={'phase': 'running'}) as start:
            response = await self.client.post('/api/notes/' + note['id'] + '/summary', json={'provider': 'codex', 'directory': str(self.folder)})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(start.call_args.args[0]['text_path'], str(self.file))
        self.assertEqual(start.call_args.args[3], str(self.folder))
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.file])
        self.summaries.states[note['id']] = {'phase': 'done', 'result': {'source_hash': note['text_hash']}}
        self.assertFalse((await self.client.get('/api/notes/' + note['id'] + '/summary')).json()['stale'])
        self.file.write_text('externally changed')
        self.assertTrue((await self.client.get('/api/notes/' + note['id'] + '/summary')).json()['stale'])

    async def test_summary_decodes_legacy_file_and_keeps_raw_source_hash(self):
        self.file.write_bytes(codecs.BOM_UTF16_BE + '예전 회의 내용'.encode('utf-16-be'))
        note = await self.opened()
        self.file.write_bytes(codecs.BOM_UTF16_BE + '요약 직전 수정된 내용'.encode('utf-16-be'))
        with patch('src.summaries.executable', return_value='/fake/codex'), patch.object(self.summaries, '_run', new=AsyncMock()) as run:
            self.summaries.start(note, 'codex', 'summarize', str(self.folder))
            await self.summaries.tasks[note['id']]
        self.assertEqual(run.call_args.args[4], '요약 직전 수정된 내용')
        self.assertEqual(run.call_args.args[5], hashlib.sha256(self.file.read_bytes()).hexdigest())
