"""Visible exports are durable before temporary recording copies disappear."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.job_artifacts import JobArtifacts, PENDING_AUXILIARY
from src.store import NoteStore


class JobArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = NoteStore(self.root/'data/notes.sqlite3')
        self.downloads = self.root/'Downloads'
        self.artifacts = JobArtifacts(self.store, self.downloads)

    def note(self, identity='a'*32, status='done'):
        folder = self.store.upload_directory(identity)
        folder.mkdir(parents=True)
        (folder/'source.m4a').write_bytes(b'original copy')
        (folder/'normalized.wav').write_bytes(b'working PCM')
        self.store.create(dict(id=identity,title='회의',original_filename='voice.m4a',
            status=status,source_path=str(folder/'source.m4a'),normalized_path=str(folder/'normalized.wav')))
        return self.store.update(identity,segments=[dict(id=0,start=0,end=3,speaker='S01',text='전사문')],
                                 speaker_names={'S01':'발표자'})

    def test_success_saves_both_texts_in_downloads_and_discards_audio_json(self):
        note=self.note()
        folder=self.store.upload_directory(note['id'])
        (folder/'speech-result.json').write_text('{}')
        note=self.store.update(note['id'],**self.artifacts.publish(note,'Qwen 비교용 대본'))
        self.assertIn('발표자: 전사문',Path(note['text_path']).read_text())
        self.assertEqual(Path(note['text_path']).parent,self.downloads)
        self.assertEqual(self.artifacts.auxiliary(note),'Qwen 비교용 대본')
        self.assertNotIn(PENDING_AUXILIARY,note['pipeline_result'])
        self.assertTrue(self.artifacts.discard_working_files(note['id']))
        self.assertFalse(folder.exists())
        self.assertIsNone(self.store.get(note['id'])['normalized_path'])
        self.assertEqual(len(list(self.downloads.glob('*.txt'))),2)

    def test_name_collisions_and_user_files_are_never_overwritten(self):
        note=self.note();self.downloads.mkdir()
        (self.downloads/'회의.txt').write_text('user file')
        (self.downloads/'회의-1 - Qwen 보조 대본.txt').write_text('other auxiliary')
        saved={**note,**self.artifacts.publish(note,'new auxiliary')}
        self.assertEqual((self.downloads/'회의.txt').read_text(),'user file')
        self.assertEqual((self.downloads/'회의-1 - Qwen 보조 대본.txt').read_text(),'other auxiliary')
        self.assertEqual(self.artifacts.auxiliary(saved),'new auxiliary')

    def test_processing_and_queued_jobs_are_not_cleaned(self):
        for identity,status in [('b'*32,'processing'),('c'*32,'queued')]:
            note=self.note(identity,status)
            self.assertFalse(self.artifacts.discard_working_files(note['id']))
            self.assertTrue(self.store.upload_directory(note['id']).exists())

    def test_failed_and_cancelled_copies_are_removed_but_external_original_survives(self):
        original=self.root/'original.m4a';original.write_bytes(b'keep')
        for identity,status in [('d'*32,'error'),('e'*32,'cancelled')]:
            note=self.note(identity,status)
            self.store.update(identity,source_path=str(original))
            self.artifacts.discard_working_files(identity)
            self.assertFalse(self.store.upload_directory(identity).exists())
        self.assertEqual(original.read_bytes(),b'keep')

    def test_auxiliary_save_failure_can_retry_after_audio_cleanup_without_duplicate_main(self):
        note=self.note()
        with patch.object(self.artifacts,'_save_auxiliary',side_effect=OSError('disk unavailable')):
            note=self.store.update(note['id'],**self.artifacts.publish(note,'recoverable auxiliary'))
        self.assertIn('저장하지 못했습니다',note['text_save_error'])
        self.assertEqual(note['pipeline_result'][PENDING_AUXILIARY],'recoverable auxiliary')
        self.artifacts.discard_working_files(note['id'])
        note=self.store.update(note['id'],**self.artifacts.publish(note,preserve_existing=True))
        self.assertIsNone(note['text_save_error'])
        self.assertNotIn(PENDING_AUXILIARY,note['pipeline_result'])
        self.assertEqual(len(list(self.downloads.glob('*.txt'))),2)

    def test_legacy_diagnostic_is_migrated_once_then_removed(self):
        note=self.note();folder=self.store.upload_directory(note['id'])
        (folder/'speech-result.json').write_text(json.dumps({'auxiliary_transcript':{'text':'old comparison'}}))
        self.artifacts.migrate()
        saved=self.store.get(note['id'])
        self.assertEqual(self.artifacts.auxiliary(saved),'old comparison')
        self.assertFalse(folder.exists())
        self.artifacts.migrate()
        self.assertEqual(len(list(self.downloads.glob('*.txt'))),2)

    def test_cleanup_does_not_recreate_a_user_deleted_export(self):
        note=self.note()
        self.store.update(note['id'],text_path=str(self.downloads/'deleted.txt'))
        self.artifacts.migrate()
        self.assertFalse(self.downloads.exists())
        self.assertFalse(self.store.upload_directory(note['id']).exists())

    def test_legacy_export_failure_keeps_text_for_retry_but_removes_recording(self):
        note=self.note();folder=self.store.upload_directory(note['id'])
        (folder/'speech-result.json').write_text(json.dumps({'auxiliary_transcript':{'text':'recoverable'}}))
        with patch('src.job_artifacts.write_text',side_effect=OSError('disk unavailable')):
            self.artifacts.migrate()
        saved=self.store.get(note['id'])
        self.assertTrue(saved['text_save_error'])
        self.assertEqual(saved['pipeline_result'][PENDING_AUXILIARY],'recoverable')
        self.assertFalse(folder.exists())
        self.artifacts.migrate()
        self.assertEqual(self.artifacts.auxiliary(self.store.get(note['id'])),'recoverable')

    def test_migration_preserves_external_text_edits_and_active_jobs(self):
        note=self.note();self.downloads.mkdir()
        text=self.downloads/'회의.txt';text.write_text('edited outside app')
        self.store.update(note['id'],text_path=str(text),text_hash='old hash')
        active=self.note('b'*32,'processing')
        self.artifacts.migrate()
        self.assertEqual(text.read_text(),'edited outside app')
        self.assertTrue(self.store.upload_directory(active['id']).exists())

    def test_symlinked_job_directory_or_auxiliary_never_follows_external_target(self):
        note=self.note();folder=self.store.upload_directory(note['id'])
        import shutil
        shutil.rmtree(folder)
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        folder.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.artifacts.discard_working_files(note['id'])
        self.assertEqual((outside/'keep').read_text(),'keep')
        self.downloads.mkdir()
        auxiliary=self.downloads/'aux.txt';auxiliary.symlink_to(outside/'keep')
        with self.assertRaises(FileNotFoundError):
            self.artifacts.auxiliary({'pipeline_result':{'auxiliary_path':str(auxiliary)}})
