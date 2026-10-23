"""Deleting exports also removes their private audio, history and stale index entries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.diarization import Diarization
from src.note_cleanup import MISSING_GRACE_SECONDS, NoteCleanup
from src.store import NoteStore
from src.summaries import Summaries
from src.text_files import digest
from src.text_library import TextLibrary


class NoteCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'exports'; self.folder.mkdir()
        self.trash = self.root / 'Trash'; self.trash.mkdir()
        self.data = self.root / 'data'
        self.store = NoteStore(self.data / 'notes.sqlite3')
        self.library = TextLibrary(self.data / 'library.sqlite3')
        self.summaries = Summaries(self.data / 'summaries')
        self.diarization = Diarization(self.data, self.root)
        self.cleaner = NoteCleanup(self.store, self.library, self.summaries, self.diarization, self.move)
        self.export = self.folder / '회의.txt'; self.export.write_text('개인 전사문')

    def move(self, path):
        path.rename(self.trash / path.name)

    def note(self, key='a', *, path=None, status='done'):
        key *= 32
        folder = self.store.upload_directory(key); folder.mkdir(parents=True)
        for name in ('source.m4a', 'normalized.wav', 'speech-result.json'):
            (folder / name).write_bytes(b'private data')
        self.store.create(dict(id=key, title='회의', status=status, source_path=str(folder / 'source.m4a'), original_filename='회의.m4a'))
        path = path or self.export
        self.store.update(key, text_path=str(path), text_hash=digest(path.read_bytes()) if path.exists() else 'missing', normalized_path=str(folder / 'normalized.wav'))
        return key, folder

    def records(self, key):
        self.summaries._save(key, {'phase': 'done', 'private': 'summary'})
        self.diarization.directory.mkdir(exist_ok=True)
        (self.diarization.directory / (key + '.json')).write_text('private speakers')
        (self.diarization.directory / (key + '.tmp')).write_text('interrupted write')
        self.diarization.states['note:' + key] = {'phase': 'done'}

    def test_trash_cleans_all_path_owners_audio_json_states_and_index_only(self):
        keys = [self.note(key)[0] for key in ('a', 'b')]
        document = self.library.scan(str(self.folder))[0]
        for key in keys + [document['id']]: self.records(key)
        other = self.folder / '다른 파일.txt'; other.write_text('keep')
        other_key, other_folder = self.note('c', path=other)
        self.records(other_key)
        summary = self.folder / '회의_요약.md'; summary.write_text('keep exported summary')
        original = self.root / '회의.m4a'; original.write_bytes(b'user original outside app')
        profile = self.data / 'voice-profiles' / 'profile.json'; profile.parent.mkdir(); profile.write_text('keep profile')
        result = self.cleaner.remove(self.export, trash_transcript=True)
        self.assertEqual(result['notes'], 2)
        self.assertGreater(result['audio_bytes'], 0)
        self.assertEqual([n['id'] for n in self.store.list()], [other_key])
        self.assertEqual(self.library.entries(), [])
        self.assertFalse(self.export.exists())
        self.assertTrue((self.trash / self.export.name).is_file())
        for key in keys:
            self.assertFalse(self.store.upload_directory(key).exists())
            self.assertTrue((self.trash / key / 'source.m4a').is_file())
        for key in keys + [document['id']]:
            self.assertFalse(self.summaries._path(key).exists())
            self.assertNotIn(key, self.summaries.states)
            self.assertFalse((self.diarization.directory / (key + '.json')).exists())
            self.assertNotIn('note:' + key, self.diarization.states)
        self.assertTrue(other_folder.exists()); self.assertTrue(self.summaries._path(other_key).exists())
        self.assertTrue(summary.exists()); self.assertTrue(profile.exists()); self.assertTrue(original.exists())

    def test_busy_owner_or_related_document_blocks_before_any_move(self):
        key, folder = self.note()
        document = self.library.scan(str(self.folder))[0]
        for attribute, identity in ((self.summaries, document['id']), (self.diarization, 'note:' + key)):
            with patch.object(attribute, 'busy', side_effect=lambda value: value == identity):
                with self.assertRaises(ValueError): self.cleaner.remove(self.export, trash_transcript=True)
            self.assertTrue(folder.exists()); self.assertTrue(self.export.exists())
        self.store.update(key, status='processing')
        with self.assertRaises(ValueError): self.cleaner.remove(self.export, trash_transcript=True)
        self.store.update(key, status='done')
        self.cleaner.active = lambda value: value == key
        with self.assertRaises(ValueError): self.cleaner.remove(self.export, trash_transcript=True)
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_failed_export_trash_keeps_everything_and_internal_failure_can_retry(self):
        key, folder = self.note(); self.records(key)
        with patch.object(self.cleaner, 'trash', side_effect=OSError('denied')):
            with self.assertRaises(OSError): self.cleaner.remove(self.export, trash_transcript=True)
        self.assertTrue(self.export.exists()); self.assertTrue(folder.exists()); self.store.get(key)
        def fail_audio(path):
            if path == folder: raise OSError('audio Trash failure')
            self.move(path)
        with patch.object(self.cleaner, 'trash', side_effect=fail_audio):
            with self.assertRaises(OSError): self.cleaner.remove(self.export, trash_transcript=True)
        self.assertFalse(self.export.exists()); self.assertTrue(folder.exists()); self.store.get(key)
        self.assertTrue(self.summaries._path(key).exists())
        result = self.cleaner.sweep(force=True)
        self.assertEqual(len(result['removed']), 1)
        self.assertFalse(folder.exists()); self.assertEqual(self.store.list(), [])
        self.assertEqual(self.cleaner.sweep(force=True)['removed'], [])

    def test_missing_file_grace_restore_and_existing_changed_file_are_preserved(self):
        key, folder = self.note(); self.library.scan(str(self.folder))
        self.export.unlink()
        self.assertEqual(self.cleaner.sweep(now=0)['removed'], [])
        self.assertEqual(self.cleaner.sweep(now=MISSING_GRACE_SECONDS - 1)['removed'], [])
        self.export.write_text('restored with edits')
        self.assertEqual(self.cleaner.sweep(now=100)['removed'], [])
        self.assertTrue(folder.exists()); self.store.get(key)
        self.export.unlink()
        self.cleaner.sweep(now=110)
        self.assertEqual(len(self.cleaner.sweep(now=110 + MISSING_GRACE_SECONDS)['removed']), 1)

    def test_missing_parent_permission_error_and_symlink_are_not_deletions(self):
        key, folder = self.note(); self.export.unlink()
        self.folder.rename(self.root / 'offline')
        self.assertEqual(self.cleaner.sweep(force=True)['removed'], [])
        (self.root / 'offline').rename(self.folder)
        with patch.object(Path, 'iterdir', side_effect=PermissionError('no access')):
            self.assertEqual(self.cleaner.sweep(force=True)['removed'], [])
        outside = self.root / 'outside.txt'; outside.write_text('keep')
        self.export.symlink_to(outside)
        self.assertEqual(self.cleaner.sweep(force=True)['removed'], [])
        self.assertTrue(folder.exists()); self.store.get(key)

    def test_same_folder_rename_relinks_managed_and_imported_history(self):
        key, folder = self.note()
        document = self.library.scan(str(self.folder))[0]; self.records(document['id'])
        target = self.export.with_name('Finder 새 이름.txt'); self.export.rename(target)
        # A scan can discover the new filename before the cleanup pass.
        self.library.scan(str(self.folder))
        result = self.cleaner.sweep(force=True)
        self.assertEqual(result['removed'], [])
        self.assertEqual(result['relocated'], [{'source': str(self.export), 'target': str(target)}])
        self.assertEqual(self.store.get(key)['text_path'], str(target))
        self.assertEqual(self.library.get(document['id'])['text_path'], str(target))
        self.assertTrue(folder.exists()); self.assertTrue(self.summaries._path(document['id']).exists())

    def test_legacy_hash_rename_and_ambiguous_identical_copies_are_preserved(self):
        key, folder = self.note()  # No index fingerprint: use the managed text hash.
        target = self.folder / 'copy.txt'; target.write_bytes(self.export.read_bytes()); self.export.unlink()
        other = self.folder / 'copy2.txt'; other.write_bytes(target.read_bytes())
        self.assertEqual(self.cleaner.sweep(force=True), {'removed': [], 'relocated': []})
        self.assertTrue(folder.exists())
        other.unlink()
        self.assertEqual(len(self.cleaner.sweep(force=True)['relocated']), 1)
        self.assertEqual(self.store.get(key)['text_path'], str(target))

    def test_escaped_audio_paths_block_trash_and_dry_run_changes_nothing(self):
        key, folder = self.note(); self.export.unlink()
        self.assertEqual(len(self.cleaner.sweep(force=True, dry_run=True)['removed']), 1)
        self.assertTrue(folder.exists()); self.store.get(key)
        self.export.write_text('private')
        outside = self.root / 'outside'; outside.mkdir()
        (folder / 'source.m4a').unlink(); (folder / 'source.m4a').symlink_to(outside / 'keep.wav')
        with self.assertRaises(ValueError): self.cleaner.remove(self.export, trash_transcript=True)
        self.assertTrue(self.export.exists()); self.assertTrue(folder.exists())
        self.assertEqual(list(self.trash.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
