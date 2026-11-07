"""Removal preserves transcripts, edited identities and unrelated model files."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.retired_features import remove_retired_desktop_runtime, remove_retired_voice_data
from src.store import NoteStore


class RetiredFeaturesTests(unittest.TestCase):
    def test_old_schema_drops_only_suggestions_and_preserves_manual_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'notes.sqlite3'
            store = NoteStore(database)
            store.create(dict(id='old', title='note', original_filename='original.m4a', source_path='/original.m4a', status='done'))
            store.update('old', segments=[dict(id=0, speaker='S01', start=0, end=1, text='수정한 본문')], speaker_names={'S01':'직접 수정한 이름'})
            with sqlite3.connect(database) as connection:
                for column in ('voice_matches_json', 'voice_merge_candidates_json', 'voice_match_error'):
                    connection.execute(f'ALTER TABLE notes ADD COLUMN {column} TEXT')
                    connection.execute(f'UPDATE notes SET {column} = ?', ('retired private data',))
            migrated = NoteStore(database)
            note = migrated.get('old')
            self.assertEqual(note['segments'][0]['text'], '수정한 본문')
            self.assertEqual(note['speaker_names'], {'S01':'직접 수정한 이름'})
            self.assertFalse(any(key.startswith('voice_') for key in note))
            self.assertEqual(NoteStore(database).get('old'), note)

    def test_cleanup_is_scoped_idempotent_and_skips_active_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); data = root / 'data'
            profiles = data / 'voice-profiles'; profiles.mkdir(parents=True)
            (profiles / 'profiles.sqlite3').write_text('retired')
            runtime = root / '.cache/voice-engine'; runtime.mkdir(parents=True)
            (runtime / 'campplus.onnx').write_text('retired')
            retained = root / '.cache/diarization/model'; retained.parent.mkdir()
            retained.write_text('keep')
            with patch('src.retired_features.subprocess.check_output', return_value=str(runtime / 'runtime/bin/python')):
                remove_retired_voice_data(data, root)
            self.assertFalse(profiles.exists()); self.assertTrue(runtime.exists())
            with patch('src.retired_features.subprocess.check_output', return_value=''):
                remove_retired_voice_data(data, root); remove_retired_voice_data(data, root)
            self.assertFalse(runtime.exists()); self.assertEqual(retained.read_text(), 'keep')

    def test_cleanup_does_not_follow_profile_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = root / 'data'; data.mkdir()
            outside = root / 'originals'; outside.mkdir(); (outside / 'audio.wav').write_text('keep')
            (data / 'voice-profiles').symlink_to(outside, target_is_directory=True)
            with patch('src.retired_features.subprocess.check_output', return_value=''):
                remove_retired_voice_data(data, root)
            self.assertEqual((outside / 'audio.wav').read_text(), 'keep')

    def test_bundled_backend_retires_only_unused_legacy_desktop_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            resources = root / 'cosMOSS.app/Contents/Resources'
            target = root / 'desktop-runtimes'
            slot = target / ('a' * 24)
            slot.mkdir(parents=True)
            (slot / 'ready').touch()
            shared_python = root / '.cache/uv/python'
            shared_python.mkdir(parents=True)
            (slot / 'shared-python').symlink_to(shared_python, target_is_directory=True)
            with patch.dict('os.environ', {'COSMOSS_BUNDLED_RESOURCES': str(resources)}), \
                 patch('src.retired_features.subprocess.check_output') as processes:
                remove_retired_desktop_runtime(root)
                processes.assert_not_called()
                with patch('src.retired_features.sys.executable', str(resources / 'python/bin/python3.12')):
                    for command in (str(slot / 'venv/bin/python'), f'install-desktop-runtime.sh Resources {root} --install'):
                        processes.return_value = command
                        remove_retired_desktop_runtime(root)
                        self.assertTrue(slot.is_dir())
                    processes.return_value = ''
                    extra = target / 'other-file'
                    extra.touch()
                    remove_retired_desktop_runtime(root)
                    self.assertTrue(extra.is_file())
                    extra.unlink()
                    remove_retired_desktop_runtime(root)
                    remove_retired_desktop_runtime(root)
                    self.assertFalse(target.exists())
                    self.assertTrue(shared_python.is_dir())
                    target.symlink_to(shared_python, target_is_directory=True)
                    remove_retired_desktop_runtime(root)
                    self.assertTrue(target.is_symlink())
                    self.assertTrue(shared_python.is_dir())
