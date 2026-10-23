"""Library status tracks completed summaries, including edits and app restarts."""
import tempfile
import unittest
from pathlib import Path

from src.store import NoteStore
from src.summaries import Summaries
from src.text_files import digest, save_text
from src.text_library import TextLibrary
from tests.test_workflow import seed


class NoteMarkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = NoteStore(self.root / 'data' / 'notes.sqlite3')
        self.note = save_text(self.store, seed(self.store)['id'], str(self.root / 'exports'))
        self.manager = Summaries(self.root / 'data' / 'summaries')

    def complete(self, note=None):
        note = note or self.note
        self.manager._save(note['id'], {'phase': 'done', 'result': {
            'source_hash': digest(Path(note['text_path']).read_bytes()),
            'completed_at': '2026-10-24T00:00:00+00:00', 'text': '# 요약',
        }})

    def test_status_survives_restart_and_detects_external_or_internal_edits(self):
        self.assertEqual(self.manager.marker(self.note)['summary_status'], 'not_started')
        self.complete()
        manager = Summaries(self.manager.directory)
        self.assertEqual(manager.marker(self.note), {
            'summary_status': 'done', 'summary_completed_at': '2026-10-24T00:00:00+00:00'})
        edited = {**self.note, 'segments': [{**self.note['segments'][0], 'text': '수정한 내용'}]}
        self.assertEqual(manager.marker(edited)['summary_status'], 'stale')
        Path(self.note['text_path']).write_text('외부에서 수정')
        self.assertEqual(manager.marker(self.note)['summary_status'], 'stale')

    def test_imported_file_metadata_can_be_checked_without_loading_editor(self):
        library = TextLibrary(self.root / 'data' / 'library.sqlite3')
        note = library.scan(str(self.root / 'exports'))[0]
        self.complete(note)
        self.assertEqual(self.manager.marker(note)['summary_status'], 'done')
        Path(note['text_path']).write_text('새 내용')
        self.assertEqual(self.manager.marker(note)['summary_status'], 'stale')

    def test_running_retry_and_failure_are_not_mislabeled_as_done(self):
        self.complete()
        for phase in ('running', 'error', 'cancelled'):
            self.manager.states[self.note['id']]['phase'] = phase
            marker = self.manager.marker(self.note)
            self.assertEqual(marker['summary_status'], phase)
            self.assertTrue(marker['summary_completed_at'])
            self.assertNotIn('text', marker)

    def test_interrupted_summary_is_visible_as_error_after_restart(self):
        self.manager._save(self.note['id'], {'phase': 'running'})
        manager = Summaries(self.manager.directory)
        self.assertEqual(manager.marker(self.note)['summary_status'], 'error')
