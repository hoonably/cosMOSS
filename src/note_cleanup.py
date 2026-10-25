"""Remove a deleted transcript's managed audio and private per-note records."""
from __future__ import annotations

import logging
import stat
import time
from pathlib import Path

from src.text_files import digest
from src.text_library import SUPPORTED_FORMATS


MISSING_GRACE_SECONDS = 60
logger = logging.getLogger(__name__)


class NoteCleanup:
    def __init__(self, store, library, summaries, speaker_records, trash, *, active=None, missing_since=None):
        self.store, self.library = store, library
        self.summaries, self.speaker_records, self.trash = summaries, speaker_records, trash
        self.active = active or (lambda _: False)
        self.missing_since = missing_since if missing_since is not None else {}

    def _idle(self, notes, document_ids):
        ids = {note['id'] for note in notes} | set(document_ids)
        if (any(note['status'] in {'queued', 'processing'} for note in notes)
                or any(self.active(key) or self.summaries.busy(key) for key in ids)):
            raise ValueError('이 파일의 전사·AI 작업이 끝난 뒤 다시 시도해 주세요.')

    def _group(self, path):
        notes = [n for n in self.store.list() if n.get('text_path') == str(path)]
        documents = [row for row in self.library.entries() if row['path'] == str(path)]
        return notes, documents

    def remove(self, path, *, trash_transcript=False):
        """Preflight every owner before touching files; retain DB records on a Trash failure."""
        path = Path(path)
        with self.store.lock, self.library.lock:
            notes, documents = self._group(path)
            self._idle(notes, [row['id'] for row in documents])
            folders = []
            for note in notes:
                folder = self.store.upload_directory(note['id'])
                # Never follow a legacy/external source path or delete another
                # note's directory. Original files outside app storage stay put.
                for field in ('source_path', 'normalized_path'):
                    if note.get(field):
                        self.store.resolve_path(note['id'], note[field])
                if folder.exists() and not folder.is_dir():
                    raise ValueError('노트의 녹음 저장 폴더를 확인해 주세요.')
                folders.append(folder)
            ids = {n['id'] for n in notes} | {row['id'] for row in documents}
            for key in ids:
                # Check paths before moving any file, including JSON locations.
                self.summaries._path(key)
                for directory in (self.summaries.directory, self.speaker_records.directory):
                    if directory.resolve() != directory:
                        raise ValueError('앱 내부 기록의 저장 경로를 확인해 주세요.')
            if not trash_transcript:
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('전사 파일이 다시 나타나 정리를 중단했습니다.')
            # Export first: if its Trash operation fails, all internal data and
            # records remain usable. Partial internal failures can be retried by
            # the missing-file sweep, without permanently deleting recordings.
            if trash_transcript:
                self.trash(path)
            size = 0
            for folder in folders:
                if folder.exists():
                    size += sum(p.lstat().st_size for p in folder.rglob('*') if not p.is_symlink() and p.is_file())
                    self.trash(folder)
            for key in ids:
                self.summaries.forget(key)
                self.speaker_records.forget(key)
            for note in notes:
                self.store.delete(note['id'])
            for row in documents:
                self.library.forget(row['id'])
            self.missing_since.pop(str(path), None)
            return {'path': str(path), 'notes': len(notes), 'documents': len(documents), 'audio_bytes': size}

    def _replacement(self, path, notes, documents, siblings):
        """Recognize a same-folder rename before treating a path as deleted."""
        identities = {(row['device'], row['inode'], row['mtime_ns'], row['size'])
                      for row in documents if row['inode'] is not None}
        inode_matches = [p for p, info in siblings
                         if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) in identities]
        if inode_matches:
            return inode_matches
        hashes = {n.get('text_hash') for n in notes} - {None, ''}
        if not hashes:
            return []
        return [p for p, info in siblings if info.st_size <= 32 * 1024**2 and digest(p.read_bytes()) in hashes]

    def sweep(self, *, force=False, now=None, dry_run=False):
        """Collect missing completed files after a grace period; unavailable folders are skipped."""
        now = time.monotonic() if now is None else now
        removed, relocated = [], []
        with self.store.lock, self.library.lock:
            paths = {n['text_path'] for n in self.store.list() if n['status'] == 'done' and n.get('text_path')}
            paths.update(row['path'] for row in self.library.entries())
            for stale in set(self.missing_since) - paths:
                self.missing_since.pop(stale, None)
            folders = {}
            for value in sorted(paths):
                path = Path(value)
                try:
                    if (not path.is_absolute() or path.suffix.lower()[1:] not in SUPPORTED_FORMATS
                            or path.parent.resolve() != path.parent):
                        continue
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        self.missing_since.pop(value, None)
                        continue
                    # Enumerating a real, accessible parent avoids mistaking an
                    # unmounted drive or a permissions problem for deletion.
                    if path.parent not in folders:
                        siblings = []
                        for candidate in path.parent.iterdir():
                            try:
                                info = candidate.lstat()
                            except FileNotFoundError:
                                continue
                            if stat.S_ISREG(info.st_mode) and candidate.suffix.lower()[1:] in SUPPORTED_FORMATS:
                                siblings.append((candidate, info))
                        folders[path.parent] = siblings
                    notes, documents = self._group(path)
                    self._idle(notes, [row['id'] for row in documents])
                    matches = self._replacement(path, notes, documents, folders[path.parent])
                    if len(matches) == 1:
                        target = matches[0]
                        others, target_documents = self._group(target)
                        self._idle(others, [row['id'] for row in target_documents])
                        # A separately summarized destination is ambiguous.
                        if any(self.summaries._path(row['id']).exists() for row in target_documents):
                            continue
                        if not dry_run:
                            self.library.relocate(path, target)
                            for note in notes:
                                self.store.update(note['id'], title=target.stem, text_path=str(target))
                        relocated.append({'source': value, 'target': str(target)})
                        self.missing_since.pop(value, None)
                        continue
                    if matches:  # Multiple identical files: don't guess.
                        continue
                    first = self.missing_since.setdefault(value, now)
                    if not force and now - first < MISSING_GRACE_SECONDS:
                        continue
                    removed.append({'path': value, 'notes': len(notes), 'documents': len(documents)}
                                   if dry_run else self.remove(path))
                except FileNotFoundError:
                    self.missing_since.pop(value, None)
                except (ValueError, OSError):
                    # Busy tasks, inaccessible folders and failed Trash moves
                    # retain all remaining records for a later retry.
                    self.missing_since.pop(value, None)
                    logger.debug('Deferred note cleanup: %s', value, exc_info=True)
        return {'removed': removed, 'relocated': relocated}
