import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.speaker_records import SpeakerRecords
from src.note_retention import RETENTION_SECONDS, RecoverableNoteCleanup
from src.store import NoteStore
from src.summaries import Summaries
from src.text_files import digest
from src.text_library import TextLibrary


class NoteRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.exports = self.root / 'exports'; self.exports.mkdir()
        self.data = self.root / 'data'
        self.store = NoteStore(self.data / 'notes.sqlite3')
        self.library = TextLibrary(self.data / 'library.sqlite3')
        self.summaries = Summaries(self.data / 'summaries')
        self.diarization = SpeakerRecords(self.data)
        self.cleaner = self.make_cleaner()
        self.transcript = self.exports / '회의.txt'; self.transcript.write_text('개인 전사문')

    def make_cleaner(self):
        return RecoverableNoteCleanup(self.store, self.library, self.summaries, self.diarization)

    def note(self, key='a', *, status='done', path=None):
        key = key * 32
        folder = self.store.upload_directory(key); folder.mkdir(parents=True)
        source = folder / 'source.m4a'; source.write_bytes(b'private recording')
        self.store.create(dict(id=key, title='회의', status=status, source_path=str(source), original_filename='회의.m4a'))
        if status == 'done':
            path = path or self.transcript
            self.store.update(key, text_path=str(path), text_hash=digest(path.read_bytes()))
        self.summaries._save(key, {'phase': 'done', 'result': {'text': 'private summary'}})
        self.diarization.directory.mkdir(exist_ok=True)
        (self.diarization.directory / (key + '.json')).write_text('{}')
        return key, folder

    def test_app_delete_restore_preserves_ids_audio_history_and_file(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        self.summaries._save(doc['id'], {'phase': 'done', 'result': {'text': 'imported summary'}})
        deleted = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        self.assertFalse(self.transcript.exists())
        self.assertTrue(folder.exists()); self.store.get(key)
        self.assertTrue(self.summaries._path(key).exists())
        self.assertEqual(self.cleaner.hidden_note_ids(), {key, doc['id']})
        self.assertEqual(self.library.scan(str(self.exports)), [])
        entry = self.make_cleaner().entries()[0]
        self.assertEqual(entry['kind'], 'app'); self.assertTrue(entry['can_restore'])
        self.assertEqual(entry['deleted_at'], '1970-01-01T00:01:40+00:00')
        restored = self.make_cleaner().restore(deleted['id'])
        self.assertEqual(restored['note_ids'], [key])
        self.assertEqual(self.transcript.read_text(), '개인 전사문')
        self.assertEqual(self.library.scan(str(self.exports))[0]['id'], doc['id'])
        self.assertTrue(self.summaries._path(doc['id']).exists())
        self.assertEqual(self.cleaner.entries(), [])
        self.assertTrue(folder.exists())

    def test_expires_after_seven_days_across_restart_only_removes_owned_data(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        original = self.root / 'original.m4a'; original.write_bytes(b'keep')
        exported_summary = self.exports / '회의_요약.md'; exported_summary.write_text('keep')
        profile = self.data / 'voice-profiles' / 'profile.json'; profile.parent.mkdir(); profile.write_text('keep')
        deleted = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        cleaner = self.make_cleaner()
        self.assertEqual(cleaner.sweep(now=100 + RETENTION_SECONDS - 1)['purged'], [])
        self.assertTrue(folder.exists())
        self.assertEqual(cleaner.sweep(now=100 + RETENTION_SECONDS)['purged'], [deleted['id']])
        self.assertEqual(self.store.list(), []); self.assertEqual(self.library.entries(), [])
        self.assertFalse(folder.exists()); self.assertFalse(self.summaries._path(key).exists())
        self.assertFalse((self.diarization.directory / (key + '.json')).exists())
        self.assertTrue(original.exists()); self.assertTrue(exported_summary.exists()); self.assertTrue(profile.exists())
        self.assertEqual(cleaner.hidden_note_ids(), set()); self.assertEqual(cleaner.entries(), [])

    def test_restore_conflict_keeps_archive_and_new_file_gets_distinct_identity(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        deleted = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        self.transcript.write_text('different new file')
        replacement = self.library.scan(str(self.exports))[0]
        self.assertNotEqual(replacement['id'], doc['id'])
        with self.assertRaises(ValueError): self.cleaner.restore(deleted['id'])
        self.assertEqual(self.transcript.read_text(), 'different new file')
        self.assertEqual(self.cleaner.reconcile_directory(self.exports), [])
        self.assertTrue(self.cleaner.entries()[0]['can_restore'])
        self.transcript.rename(self.exports / 'new.txt')
        self.cleaner.restore(deleted['id'])
        self.assertEqual(self.library.get(doc['id'])['text_path'], str(self.transcript))
        self.assertTrue(folder.exists())

    def test_restore_failure_retains_archive_and_can_retry(self):
        key, folder = self.note()
        deleted = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        with patch('src.note_retention.shutil.copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.cleaner.restore(deleted['id'])
        self.assertFalse(self.transcript.exists())
        self.assertTrue(self.cleaner.entries()[0]['can_restore'])
        self.cleaner.restore(deleted['id'])
        self.assertTrue(self.transcript.exists()); self.assertTrue(folder.exists())

    def test_archive_write_failure_keeps_original_and_no_hidden_record(self):
        key, folder = self.note()
        with patch('src.note_retention.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.cleaner.remove(self.transcript, trash_transcript=True)
        self.assertEqual(self.transcript.read_text(), '개인 전사문')
        self.assertEqual(self.cleaner.entries(), [])
        self.assertTrue(folder.exists())

    def test_interrupted_index_reservation_is_repaired_before_rescan(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        with patch.object(self.cleaner, '_reserve_documents', side_effect=OSError('index locked')):
            with self.assertRaises(OSError):
                self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        self.assertFalse(self.transcript.exists())
        self.assertTrue(self.cleaner.entries()[0]['can_restore'])
        self.transcript.write_text('unrelated new file')
        self.make_cleaner().reconcile_directory(self.exports)
        self.assertNotEqual(self.library.scan(str(self.exports))[0]['id'], doc['id'])
        self.assertTrue(folder.exists())

    def test_external_move_reconnects_metadata_and_history_by_inode(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        moved = elsewhere / self.transcript.name; self.transcript.rename(moved)
        self.cleaner.sweep(now=10)
        self.assertEqual(self.cleaner.entries(), [])
        self.cleaner.sweep(now=70)
        entry = self.cleaner.entries()[0]
        self.assertEqual(entry['kind'], 'missing'); self.assertFalse(entry['can_restore'])
        with self.assertRaises(ValueError): self.cleaner.restore(entry['id'])
        returned = self.exports / '돌아온 회의.txt'; moved.rename(returned)
        returned.write_text('사용자가 편집한 전사문')
        restored = self.make_cleaner().reconcile_directory(self.exports)
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.store.get(key)['text_path'], str(returned))
        self.assertEqual(self.library.get(doc['id'])['text_path'], str(returned))
        self.assertTrue(folder.exists()); self.assertTrue(self.summaries._path(key).exists())
        self.assertEqual(self.cleaner.entries(), [])

    def test_external_exact_copy_reconnects_but_ambiguous_copies_do_not(self):
        key, folder = self.note()
        self.library.scan(str(self.exports))
        content = self.transcript.read_bytes(); self.transcript.unlink()
        self.cleaner.sweep(force=True, now=10)
        first = self.exports / 'a.txt'; second = self.exports / 'b.txt'
        first.write_bytes(content); second.write_bytes(content)
        # Distinct inodes cannot be disambiguated by equal content.
        with patch.object(self.cleaner, '_matches', wraps=self.cleaner._matches):
            entry = self.cleaner._all()[0]
            entry['identities'] = []; self.cleaner._save(entry)
            self.assertEqual(self.cleaner.reconcile_directory(self.exports), [])
        second.unlink()
        self.assertEqual(len(self.cleaner.reconcile_directory(self.exports)), 1)
        self.assertEqual(self.store.get(key)['text_path'], str(first))

    def test_deleted_file_does_not_reappear_due_to_existing_equal_copy(self):
        key, folder = self.note()
        copy = self.exports / 'other.txt'; copy.write_bytes(self.transcript.read_bytes())
        self.cleaner.remove(self.transcript, trash_transcript=True, now=10)
        self.assertEqual(self.cleaner.reconcile_directory(self.exports), [])
        self.assertIn(key, self.cleaner.hidden_note_ids())

    def test_one_returned_copy_does_not_arbitrarily_claim_two_missing_records(self):
        first, _ = self.note()
        other = self.exports / 'other.txt'; other.write_bytes(self.transcript.read_bytes())
        second, _ = self.note('b', path=other)
        self.library.scan(str(self.exports))
        content = self.transcript.read_bytes()
        self.transcript.unlink(); other.unlink()
        self.cleaner.sweep(force=True, now=10)
        for entry in self.cleaner._all():
            entry['identities'] = []; self.cleaner._save(entry)
        returned = self.exports / 'returned.txt'; returned.write_bytes(content)
        self.assertEqual(self.cleaner.reconcile_directory(self.exports), [])
        self.assertEqual(len(self.cleaner.entries()), 2)
        self.assertIn(first, self.cleaner.hidden_note_ids())
        self.assertIn(second, self.cleaner.hidden_note_ids())

    def test_terminal_jobs_can_restore_or_expire_and_active_jobs_are_protected(self):
        key, folder = self.note(status='cancelled')
        deleted = self.cleaner.remove_job(key, now=100)
        self.assertTrue(self.cleaner.entries()[0]['can_restore'])
        self.cleaner.restore(deleted['id']); self.assertEqual(self.store.get(key)['status'], 'cancelled')
        deleted = self.cleaner.remove_job(key, now=100)
        with patch.object(self.cleaner, 'active', return_value=True):
            self.assertEqual(self.cleaner.sweep(now=100 + RETENTION_SECONDS)['purged'], [])
            with self.assertRaises(ValueError): self.cleaner.restore(deleted['id'])
        self.assertTrue(folder.exists())
        self.cleaner.sweep(now=100 + RETENTION_SECONDS)
        self.assertFalse(folder.exists()); self.assertEqual(self.store.list(), [])
        active, _ = self.note('b', status='processing')
        with self.assertRaises(ValueError): self.cleaner.remove_job(active)
        self.store.update(active, status='done', text_path=None, text_save_error='disk full')
        unsaved = self.cleaner.remove_job(active)
        self.cleaner.restore(unsaved['id'])
        self.assertEqual(self.store.get(active)['status'], 'done')

    def test_busy_related_record_and_invalid_source_prevent_delete(self):
        key, folder = self.note()
        doc = self.library.scan(str(self.exports))[0]
        with patch.object(self.summaries, 'busy', side_effect=lambda value: value == doc['id']):
            with self.assertRaises(ValueError): self.cleaner.remove(self.transcript, trash_transcript=True)
        self.assertTrue(self.transcript.exists()); self.assertEqual(self.cleaner.entries(), [])
        external = self.root / 'keep.m4a'; external.write_bytes(b'keep')
        (folder / 'source.m4a').unlink(); (folder / 'source.m4a').symlink_to(external)
        with self.assertRaises(ValueError): self.cleaner.remove(self.transcript, trash_transcript=True)
        self.assertTrue(external.exists()); self.assertTrue(self.transcript.exists())

    def test_inaccessible_parent_and_failed_purge_are_retained(self):
        key, folder = self.note()
        deleted = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        offline = self.root / 'offline'; self.exports.rename(offline)
        self.assertEqual(self.cleaner.sweep(now=100 + RETENTION_SECONDS)['purged'], [])
        self.assertTrue(folder.exists())
        offline.rename(self.exports)
        with patch('src.note_retention.shutil.rmtree', side_effect=PermissionError('locked')):
            self.assertEqual(self.cleaner.sweep(now=100 + RETENTION_SECONDS)['purged'], [])
        self.assertTrue(folder.exists()); self.store.get(key)
        self.assertEqual(self.cleaner.sweep(now=100 + RETENTION_SECONDS)['purged'], [deleted['id']])

    def test_new_file_can_be_deleted_while_older_same_name_is_retained(self):
        key, folder = self.note()
        old = self.library.scan(str(self.exports))[0]
        first = self.cleaner.remove(self.transcript, trash_transcript=True, now=100)
        self.transcript.write_text('new content')
        new = self.library.scan(str(self.exports))[0]
        second = self.cleaner.remove(self.transcript, trash_transcript=True, now=200)
        self.assertEqual(len(self.cleaner.entries()), 2)
        self.cleaner.restore(second['id'])
        self.assertEqual(self.transcript.read_text(), 'new content')
        self.assertNotIn(new['id'], self.cleaner.hidden_note_ids())
        self.assertIn(old['id'], self.cleaner.hidden_note_ids())
        self.assertIn(key, self.cleaner.hidden_note_ids())


if __name__ == '__main__':
    unittest.main()
