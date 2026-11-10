"""Visible transcript outputs; recording copies exist only while a job needs them."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil

from src.text_files import digest, write_text


DEFAULT_DOWNLOADS = Path.home() / 'Downloads'
PENDING_AUXILIARY = '_pending_auxiliary_text'


class JobArtifacts:
    def __init__(self, store, downloads=None):
        self.store = store
        self.downloads = Path(downloads or DEFAULT_DOWNLOADS).expanduser().resolve()

    def output_folder(self, note):
        folder = Path(note.get("output_directory") or self.downloads).expanduser()
        if not folder.is_absolute():
            raise ValueError("저장 폴더는 절대 경로로 지정해 주세요.")
        return folder.resolve()

    def _save_auxiliary(self, note, text, metrics):
        folder = Path(note["text_path"]).parent
        content = text.encode('utf-8')
        expected = digest(content)
        previous = metrics.get('auxiliary_path')
        if previous:
            path = Path(previous)
            if (path.parent == folder and not path.is_symlink() and path.is_file()
                    and digest(path.read_bytes()) == expected):
                return {'auxiliary_path': str(path), 'auxiliary_hash': expected}
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(note['text_path']).stem + ' - Qwen 보조 대본'
        suffix = 0
        while True:
            path = folder / f'{stem}{"-"+str(suffix) if suffix else ""}.txt'
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                suffix += 1
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {'auxiliary_path': str(path), 'auxiliary_hash': expected}

    def publish(self, note, auxiliary_text=None, *, preserve_existing=False):
        metrics = dict(note.get('pipeline_result') or {})
        if auxiliary_text is None:
            auxiliary_text = metrics.get(PENDING_AUXILIARY)
        if auxiliary_text is not None:
            # Only an unsuccessful file save temporarily needs this small value
            # in the job record. Successful outputs live in the selected output folder.
            metrics[PENDING_AUXILIARY] = auxiliary_text
        changes = {'pipeline_result': metrics}
        try:
            existing = Path(note['text_path']) if note.get('text_path') else None
            if preserve_existing and existing and existing.is_file() and not existing.is_symlink():
                saved = note
            else:
                metadata = write_text(note, str(self.output_folder(note)))
                changes.update(metadata)
                saved = {**note, **metadata}
            if auxiliary_text is not None:
                metrics.update(self._save_auxiliary(saved, auxiliary_text, metrics))
                metrics['has_auxiliary_transcript'] = True
                metrics.pop(PENDING_AUXILIARY, None)
            changes.update(output_directory=str(Path(saved["text_path"]).parent), text_save_error=None)
        except (OSError, ValueError) as error:
            changes['text_save_error'] = f'전사는 완료했지만 지정한 폴더에 저장하지 못했습니다. 다시 저장해 주세요: {error}'
        return changes

    def discard_working_files(self, note_id):
        note = self.store.get(note_id)
        if note['status'] in {'queued', 'processing'}:
            return False
        # upload_directory rejects traversal and symlinked job directories.
        # Never delete an external original referenced by an older note.
        folder = self.store.upload_directory(note_id)
        if folder.exists():
            shutil.rmtree(folder)
        if note.get('normalized_path'):
            self.store.update(note_id, normalized_path=None)
        return True

    def auxiliary(self, note):
        metrics = note.get('pipeline_result') or {}
        path = Path(metrics.get('auxiliary_path') or '')
        if (not path.is_absolute() or path.parent != (Path(note['text_path']).parent if note.get('text_path') else self.output_folder(note)) or path.is_symlink()
                or not path.is_file() or path.stat().st_size > 32 * 1024**2):
            raise FileNotFoundError('저장 폴더의 Qwen 보조 대본 파일을 찾을 수 없습니다.')
        return path.read_text()

    def migrate(self, hidden_ids=()):
        hidden_ids = set(hidden_ids)
        for note in self.store.list():
            try:
                self._migrate_note(note, hidden_ids)
            except (OSError, ValueError, KeyError):
                logging.getLogger(__name__).warning('Deferred temporary recording cleanup for %s', note['id'], exc_info=True)

    def _migrate_note(self, note, hidden_ids):
        if note['status'] in {'queued', 'processing'}:
            return
        folder = self.store.upload_directory(note['id'])
        pending = PENDING_AUXILIARY in (note.get('pipeline_result') or {})
        if not folder.exists() and not pending and not note.get('text_save_error'):
            return
        if note['status'] == 'done' and note['id'] not in hidden_ids:
            text_path = Path(note['text_path']) if note.get('text_path') else None
            # A user-deleted export is handled by the recycle-bin policy,
            # never silently recreated by storage cleanup.
            if not text_path or text_path.is_file() or note.get('text_save_error'):
                auxiliary_text = None
                diagnostic = folder / 'speech-result.json'
                if diagnostic.is_file() and not diagnostic.is_symlink():
                    result = json.loads(diagnostic.read_text())
                    auxiliary = result.get('auxiliary_transcript')
                    if isinstance(auxiliary, dict) and isinstance(auxiliary.get('text'), str):
                        auxiliary_text = auxiliary['text']
                changes = self.publish(note, auxiliary_text, preserve_existing=True)
                self.store.update(note['id'], **changes)
                # Failed exports keep segments/pending auxiliary text in the
                # note record; saving them again never needs the audio copy.
        self.discard_working_files(note['id'])
