"""Read-only Voice Memos discovery and explicit, bounded imports into normal uploads."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.store import NoteStore
from src.voice_memos import VoiceMemos


def fixture(folder):
    folder.mkdir()
    audio = folder / '20261016-test.m4a'
    audio.write_bytes(b'original recording')
    database = folder / 'CloudRecordings.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE ZCLOUDRECORDING (ZPATH TEXT, ZCUSTOMLABEL TEXT, ZDATE REAL, ZDURATION REAL)')
        db.executemany('INSERT INTO ZCLOUDRECORDING VALUES (?,?,?,?)', [
            (audio.name, '연구 미팅', 0, 65.5),
            ('cloud.m4a', 'iCloud 녹음', 60, 30),
            ('../outside.m4a', 'traversal', 0, 1),
            ('linked.m4a', 'symlink', 0, 1),
            ('https://attacker.example/audio.m4a', 'remote', 0, 1),
        ])
    return audio, database


class VoiceMemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'Recordings'
        self.audio, self.database = fixture(self.folder)
        self.library = VoiceMemos([self.folder])

    def test_titles_dates_duration_cloud_and_originals_remain_unchanged(self):
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        result = self.library.list()
        available = next(item for item in result['recordings'] if item['available'])
        self.assertEqual(available['title'], '연구 미팅')
        self.assertEqual(available['created_at'], '2001-01-01T00:00:00+00:00')
        self.assertEqual(available['duration'], 65.5)
        self.assertEqual(available['size'], len(b'original recording'))
        self.assertFalse(next(item for item in result['recordings'] if item['title'] == 'iCloud 녹음')['available'])
        self.assertNotIn('_path', available)
        stream, _ = self.library.open(available['id'], available['version'])
        with stream:
            self.assertEqual(stream.read(), b'original recording')
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.folder.iterdir()})

    def test_symlinks_traversal_unknown_and_changed_recordings_are_rejected(self):
        outside = self.root / 'outside.m4a'
        outside.write_bytes(b'private')
        (self.folder / 'linked.m4a').symlink_to(outside)
        result = self.library.list()['recordings']
        self.assertFalse(any(item['title'] in {'symlink', 'traversal', 'remote'} for item in result))
        item = next(item for item in result if item['available'])
        self.audio.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.library.open(item['id'], item['version'])
        with self.assertRaises(FileNotFoundError):
            self.library.open('0' * 64, '0' * 64)

    def test_displayed_title_overrides_internal_date_and_tracks_renames(self):
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZENCRYPTEDTITLE TEXT')
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZCUSTOMLABELFORSORTING TEXT')
            db.execute('UPDATE ZCLOUDRECORDING SET ZCUSTOMLABEL=?, ZENCRYPTEDTITLE=?, ZCUSTOMLABELFORSORTING=? WHERE ZPATH=?',
                       ('2026-10-20T08:58:57Z', '새로운 녹음 2', '새로운 녹음 2', self.audio.name))
        selected = next(item for item in self.library.list()['recordings'] if item['available'])
        self.assertEqual(selected['title'], '새로운 녹음 2')
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE ZCLOUDRECORDING SET ZENCRYPTEDTITLE=? WHERE ZPATH=?', ('연구 미팅 · HMX', self.audio.name))
        renamed = next(item for item in self.library.list()['recordings'] if item['available'])
        self.assertEqual(renamed['title'], '연구 미팅 · HMX')
        self.assertEqual(renamed['id'], selected['id'])
        stream, imported = self.library.open(selected['id'], selected['version'])
        with stream:
            self.assertEqual(imported['title'], '연구 미팅 · HMX')

    def test_missing_or_unusable_displayed_title_keeps_legacy_fallbacks(self):
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZENCRYPTEDTITLE TEXT')
            db.execute('UPDATE ZCLOUDRECORDING SET ZENCRYPTEDTITLE=? WHERE ZPATH=?', (b'not decoded as text', self.audio.name))
        self.assertEqual(next(item for item in self.library.list()['recordings'] if item['available'])['title'], '연구 미팅')
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE ZCLOUDRECORDING SET ZENCRYPTEDTITLE=?, ZCUSTOMLABEL=NULL WHERE ZPATH=?', ('   ', self.audio.name))
        self.assertEqual(next(item for item in self.library.list()['recordings'] if item['available'])['title'], self.audio.stem)

    def test_schema_changes_fall_back_to_filename_and_missing_libraries_are_empty(self):
        self.database.write_bytes(b'not a sqlite database')
        result = self.library.list()
        self.assertTrue(result['metadata_unavailable'])
        self.assertEqual(result['recordings'][0]['title'], self.audio.stem)
        self.assertEqual(VoiceMemos([self.root / 'missing']).list()['recordings'], [])

    def test_recently_deleted_local_and_cloud_records_cannot_reappear_as_filenames(self):
        original = self.audio.read_bytes()
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZEVICTIONDATE REAL')
            db.execute('UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE=123 WHERE ZPATH IN (?,?)', (self.audio.name, 'cloud.m4a'))
            # A legacy duplicate must not undo the cloud deletion tombstone.
            db.execute('CREATE TABLE ZRECORDING (ZPATH TEXT, ZCUSTOMLABEL TEXT)')
            db.execute('INSERT INTO ZRECORDING VALUES (?,?)', (self.audio.name, '오래된 메타데이터'))
        titles = {item['title'] for item in self.library.list()['recordings']}
        self.assertFalse(titles & {'연구 미팅', 'iCloud 녹음', '오래된 메타데이터'})
        self.assertEqual(self.audio.read_bytes(), original)

    def test_deletion_after_selection_is_rejected_and_restore_reappears(self):
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZEVICTIONDATE REAL')
        selected = next(item for item in self.library.list()['recordings'] if item['available'])
        with sqlite3.connect(self.database) as db:
            # Zero is still a stored deletion date, not NULL.
            db.execute('UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE=0 WHERE ZPATH=?', (self.audio.name,))
        with self.assertRaises(FileNotFoundError):
            self.library.open(selected['id'], selected['version'])
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE=NULL WHERE ZPATH=?', (self.audio.name,))
        restored = next(item for item in self.library.list()['recordings'] if item['available'])
        self.assertEqual(restored['id'], selected['id'])
        stream, _ = self.library.open(restored['id'], restored['version'])
        with stream:
            self.assertEqual(stream.read(), self.audio.read_bytes())

    def test_legacy_deletion_column_and_unrelated_flags(self):
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZDELETIONDATE REAL')
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZFLAGS INTEGER')
            db.execute('UPDATE ZCLOUDRECORDING SET ZFLAGS=1540')
            db.execute('UPDATE ZCLOUDRECORDING SET ZDELETIONDATE=123 WHERE ZPATH=?', ('cloud.m4a',))
        result = self.library.list()['recordings']
        self.assertTrue(any(item['title'] == '연구 미팅' and item['available'] for item in result))
        self.assertFalse(any(item['title'] == 'iCloud 녹음' for item in result))
        self.assertFalse(any(key.startswith('_') for item in result for key in item))


class VoiceMemoAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src import main
        self.main = main
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio, _ = fixture(self.root / 'Recordings')
        self.store = NoteStore(self.root / 'data/notes.sqlite3')
        for attribute, value in {'VOICE_MEMOS': VoiceMemos([self.audio.parent]), 'STORE': self.store,
                                 'UPLOAD_DIR': self.root / 'uploads', 'MAX_UPLOAD_BYTES': 100}.items():
            self.enterContext(patch.object(main, attribute, value))
        self.enterContext(patch.object(main.MEDIA_TOOLS, 'require_ready', AsyncMock()))
        self.enterContext(patch.object(main, 'probe_duration', return_value=65.5))
        self.prepare = self.enterContext(patch.object(main, 'start_upload_preparation'))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def selected(self):
        response = await self.client.get('/api/voice-memos')
        self.assertEqual(response.status_code, 200)
        return next(item for item in response.json()['recordings'] if item['available'])

    async def test_import_uses_displayed_recording_name_instead_of_internal_filename(self):
        with sqlite3.connect(self.audio.parent / 'CloudRecordings.db') as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZENCRYPTEDTITLE TEXT')
            db.execute('UPDATE ZCLOUDRECORDING SET ZCUSTOMLABEL=?, ZENCRYPTEDTITLE=? WHERE ZPATH=?',
                       ('2026-10-20T08:58:57Z', '새로운 녹음 2', self.audio.name))
        item = await self.selected()
        self.assertEqual(item['title'], '새로운 녹음 2')
        response = await self.client.post('/api/voice-memos/import', json={'id':item['id'], 'version':item['version']})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['original_filename'], '새로운 녹음 2.m4a')
        self.assertEqual(self.audio.read_bytes(), b'original recording')

    async def test_explicit_import_uses_normal_upload_pipeline_and_preserves_source(self):
        item = await self.selected()
        self.prepare.assert_not_called()
        self.assertFalse(self.main.UPLOAD_DIR.exists())
        response = await self.client.post('/api/voice-memos/import', json={'id': item['id'], 'version': item['version']})
        self.assertEqual(response.status_code, 202, response.text)
        upload = response.json()
        self.assertEqual(upload['original_filename'], '연구 미팅.m4a')
        self.assertEqual(upload['duration'], 65.5)
        self.assertNotIn('source_path', upload)
        self.assertEqual(Path(self.store.get_upload(upload['id'])['source_path']).read_bytes(), self.audio.read_bytes())
        self.prepare.assert_called_once_with(upload['id'])

    async def test_limits_stale_selection_and_external_requests(self):
        item = await self.selected()
        selection = {'id': item['id'], 'version': item['version']}
        self.audio.write_bytes(b'changed')
        self.assertEqual((await self.client.post('/api/voice-memos/import', json=selection)).status_code, 409)
        item = await self.selected()
        with patch.object(self.main, 'MAX_UPLOAD_BYTES', 2):
            response = await self.client.post('/api/voice-memos/import', json={'id': item['id'], 'version': item['version']})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list(self.main.UPLOAD_DIR.iterdir()), [])
        self.assertEqual((await self.client.post('/api/voice-memos/import', json={**selection, 'path': str(self.audio)})).status_code, 422)
        self.assertEqual((await self.client.get('/api/voice-memos', headers={'Origin': 'https://attacker.example'})).status_code, 403)
        self.assertEqual((await self.client.post('/api/voice-memos/import', json=selection, headers={'Sec-Fetch-Site': 'cross-site'})).status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app, client=('10.0.0.2', 10000)), base_url='http://localhost') as client:
            self.assertEqual((await client.get('/api/voice-memos')).status_code, 403)
        self.prepare.assert_not_called()

    async def test_import_rechecks_recently_deleted_state_without_copying_audio(self):
        item = await self.selected()
        with sqlite3.connect(self.audio.parent / 'CloudRecordings.db') as db:
            db.execute('ALTER TABLE ZCLOUDRECORDING ADD COLUMN ZEVICTIONDATE REAL')
            db.execute('UPDATE ZCLOUDRECORDING SET ZEVICTIONDATE=123 WHERE ZPATH=?', (self.audio.name,))
        response = await self.client.post('/api/voice-memos/import', json={'id': item['id'], 'version': item['version']})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse(self.main.UPLOAD_DIR.exists())
        self.prepare.assert_not_called()
