"""Seven-day app recycle bin for transcripts and their private working data.

External files are never purged: only files explicitly put in this app's bin,
managed recording folders, and private per-note records expire. Missing files
can reconnect by identity or exact content without scanning outside a selected
directory.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from src.note_cleanup import MISSING_GRACE_SECONDS, NoteCleanup
from src.text_files import digest
from src.text_library import SUPPORTED_FORMATS


RETENTION_SECONDS = 7 * 24 * 60 * 60
logger = logging.getLogger(__name__)


class RecoverableNoteCleanup(NoteCleanup):
    def __init__(self, store, library, summaries, diarization, trash=None, *, active=None, missing_since=None):
        super().__init__(store, library, summaries, diarization, trash, active=active, missing_since=missing_since)
        self.directory = store.data_dir / 'recycle-bin'
        self.database = store.data_dir / 'note-retention.sqlite3'
        with self.store.lock, self._connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS retained_notes (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def _all(self):
        with self.store.lock, self._connect() as connection:
            rows = connection.execute('SELECT payload FROM retained_notes').fetchall()
        return [json.loads(row[0]) for row in rows]

    def _save(self, entry):
        with self.store.lock, self._connect() as connection:
            connection.execute('INSERT INTO retained_notes VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                               (entry['id'], json.dumps(entry, ensure_ascii=False)))

    def _forget(self, entry):
        with self.store.lock, self._connect() as connection:
            connection.execute('DELETE FROM retained_notes WHERE id=?', (entry['id'],))

    def _get(self, entry_id):
        with self.store.lock, self._connect() as connection:
            row = connection.execute('SELECT payload FROM retained_notes WHERE id=?', (entry_id,)).fetchone()
        if not row:
            raise KeyError(entry_id)
        return json.loads(row[0])

    def _folder(self, entry):
        key = entry['id']
        if not isinstance(key, str) or len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('보관된 노트의 경로를 확인해 주세요.')
        expected = self.directory / key
        if self.directory.resolve() != self.directory or expected.resolve() != expected:
            raise ValueError('보관된 노트의 경로를 확인해 주세요.')
        return expected

    def _archived(self, entry):
        return self._folder(entry) / ('transcript.' + entry.get('format', 'txt'))

    @staticmethod
    def _ids(entry):
        return set(entry['note_ids']) | {row['id'] for row in entry['documents']}

    def hidden_note_ids(self):
        return set().union(*(self._ids(entry) for entry in self._all()))

    def entries(self):
        result = []
        for entry in sorted(self._all(), key=lambda row: row['deleted_at'], reverse=True):
            archived = self._archived(entry)
            result.append({
                'id': entry['id'], 'title': entry['title'], 'kind': entry['kind'],
                'deleted_at': datetime.fromtimestamp(entry['deleted_at'], UTC).isoformat(),
                'expires_at': datetime.fromtimestamp(entry['deleted_at'] + RETENTION_SECONDS, UTC).isoformat(),
                'can_restore': entry['kind'] == 'job' or (archived.is_file() and not archived.is_symlink()),
            })
        return result

    def _owners(self, entry):
        notes = []
        for key in entry['note_ids']:
            try:
                notes.append(self.store.get(key))
            except KeyError:
                pass
        self._idle(notes, [row['id'] for row in entry['documents']])
        return notes

    def _preflight(self, notes, documents):
        self._idle(notes, [row['id'] for row in documents])
        for note in notes:
            folder = self.store.upload_directory(note['id'])
            for field in ('source_path', 'normalized_path'):
                if note.get(field):
                    self.store.resolve_path(note['id'], note[field])
            if folder.exists() and not folder.is_dir():
                raise ValueError('노트의 녹음 저장 폴더를 확인해 주세요.')
        for key in {note['id'] for note in notes} | {row['id'] for row in documents}:
            self.summaries._path(key)
        for directory in (self.summaries.directory, self.diarization.directory):
            if directory.resolve() != directory:
                raise ValueError('앱 내부 기록의 저장 경로를 확인해 주세요.')

    def _reserve_documents(self, entry):
        # Keep IDs reserved so a new file at the old pathname does not inherit
        # the deleted document's path-hash identity and private summary.
        with self.library.lock, self.library._connect() as connection:
            for row in entry['documents']:
                reserved = str(self._folder(entry) / 'reserved' / (row['id'] + '.txt'))
                connection.execute('UPDATE documents SET path=? WHERE id=?', (reserved, row['id']))

    def _entry(self, notes, documents, *, path=None, kind, now):
        identities = [dict(device=row['device'], inode=row['inode'], mtime_ns=row['mtime_ns'], size=row['size'])
                      for row in documents if row.get('inode') is not None]
        hashes = sorted({note.get('text_hash') for note in notes} - {None, ''})
        return {'id': uuid.uuid4().hex, 'title': path.stem if path else notes[0]['title'],
                'kind': kind, 'path': str(path) if path else None,
                'format': path.suffix.lower()[1:] if path else 'txt', 'deleted_at': now,
                'note_ids': [note['id'] for note in notes], 'documents': documents,
                'identities': identities, 'hashes': hashes}

    def remove(self, path, *, trash_transcript=False, now=None):
        path = Path(path)
        now = time.time() if now is None else now
        if (not path.is_absolute() or path.parent.resolve() != path.parent
                or path.suffix.lower()[1:] not in SUPPORTED_FORMATS or path.is_symlink()):
            raise ValueError('전사 파일의 저장 경로를 확인해 주세요.')
        with self.store.lock, self.library.lock:
            notes, documents = self._group(path)
            hidden = self.hidden_note_ids()
            notes = [note for note in notes if note['id'] not in hidden]
            documents = [row for row in documents if row['id'] not in hidden]
            self._preflight(notes, documents)
            entry = self._entry(notes, documents, path=path, kind='app' if trash_transcript else 'missing', now=now)
            content = None
            if trash_transcript:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024**2:
                    raise ValueError('32MB 이하의 전사 파일을 선택해 주세요.')
                content = path.read_bytes()
                entry['hashes'] = [digest(content)]
                entry['identities'].append(dict(device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns, size=info.st_size))
            elif path.exists():
                raise ValueError('전사 파일이 다시 나타나 정리를 중단했습니다.')
            self._save(entry)  # Durable intent precedes changing any file.
            try:
                if content is not None:
                    archived = self._archived(entry)
                    archived.parent.mkdir(parents=True, exist_ok=False)
                    with archived.open('xb') as stream:
                        stream.write(content); stream.flush(); os.fsync(stream.fileno())
                    shutil.copystat(path, archived, follow_symlinks=False)
                    current = path.lstat()
                    if (not stat.S_ISREG(current.st_mode)
                            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                            or digest(path.read_bytes()) != entry['hashes'][0]):
                        raise ValueError('파일이 변경되었습니다. 다시 불러온 뒤 삭제해 주세요.')
                    path.unlink()
                self._reserve_documents(entry)
            except BaseException:
                if path.exists():
                    folder = self._folder(entry)
                    if folder.exists(): shutil.rmtree(folder)
                    self._forget(entry)
                # If unlink succeeded, keep the durable record/archive so the
                # next sweep or restore can finish after an indexing failure.
                raise
            self.missing_since.pop(str(path), None)
            return {'id': entry['id'], 'path': str(path), 'notes': len(notes), 'documents': len(documents)}

    def remove_job(self, note_id, *, now=None):
        with self.store.lock, self.library.lock:
            note = self.store.get(note_id)
            if note_id in self.hidden_note_ids():
                raise ValueError('이미 보관함에 있는 작업입니다.')
            if note['status'] not in {'done', 'error', 'cancelled'} or note.get('text_path'):
                raise ValueError('완료·실패·취소된 전사 작업만 정리할 수 있습니다.')
            self._preflight([note], [])
            entry = self._entry([note], [], kind='job', now=time.time() if now is None else now)
            self._save(entry)
            return {'id': entry['id'], 'note_ids': [note_id]}

    @staticmethod
    def _regular_files(directory):
        directory = Path(directory).expanduser()
        if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
            raise ValueError('전사 파일 폴더를 찾을 수 없습니다.')
        files = []
        for path in directory.iterdir():
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and path.suffix.lower()[1:] in SUPPORTED_FORMATS:
                files.append((path, info))
        return files

    @staticmethod
    def _matches(entry, files):
        if entry['kind'] == 'app':
            # Explicitly deleted files stay deleted even if an identical copy
            # was already present under another filename.
            files = [(path, info) for path, info in files if str(path) == entry['path']]
        identities = ({(row['device'], row['inode']) for row in entry['identities'] if row.get('inode') is not None}
                      if entry['kind'] == 'missing' else set())
        # Inode matches preserve edits made while a file was temporarily away.
        exact = [path for path, info in files if (info.st_dev, info.st_ino) in identities]
        if exact:
            return exact
        hashes = set(entry['hashes'])
        if not hashes:
            return []
        return [path for path, info in files if info.st_size <= 32 * 1024**2 and digest(path.read_bytes()) in hashes]

    def _reconnect(self, entry, target):
        notes = self._owners(entry)
        self._preflight(notes, entry['documents'])
        current, indexed = self._group(target)
        hidden = self.hidden_note_ids() - self._ids(entry)
        current = [note for note in current if note['id'] not in hidden]
        indexed = [row for row in indexed if row['id'] not in hidden]
        self._idle(current, [row['id'] for row in indexed])
        # Do not absorb a separately created/processed document's history.
        old_ids = self._ids(entry)
        if any(note['id'] not in old_ids for note in current):
            raise ValueError('이 파일은 이미 다른 노트에 연결되어 있습니다.')
        for row in indexed:
            if row['id'] not in old_ids and (self.summaries._path(row['id']).exists()
                    or (self.diarization.directory / (row['id'] + '.json')).exists()):
                raise ValueError('이 파일에는 별도의 AI 기록이 있어 자동으로 연결하지 않았습니다.')
        info = target.stat()
        with self.library._connect() as connection:
            for row in indexed:
                if row['id'] not in old_ids:
                    connection.execute('DELETE FROM documents WHERE id=?', (row['id'],))
            for row in entry['documents']:
                connection.execute('''INSERT INTO documents (id,path,device,inode,mtime_ns,size) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET path=excluded.path,device=excluded.device,inode=excluded.inode,
                    mtime_ns=excluded.mtime_ns,size=excluded.size''',
                    (row['id'], str(target), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size))
        for note in notes:
            self.store.update(note['id'], title=target.stem, text_path=str(target))
        folder = self._folder(entry)
        if folder.exists(): shutil.rmtree(folder)
        self._forget(entry)
        self.missing_since.pop(entry['path'], None)
        return {'id': entry['id'], 'path': str(target), 'note_ids': entry['note_ids'],
                'document_ids': [row['id'] for row in entry['documents']]}

    def restore(self, entry_id):
        with self.store.lock, self.library.lock:
            entry = self._get(entry_id)
            self._owners(entry)
            if entry['kind'] == 'job':
                self._forget(entry)
                return {'id': entry_id, 'path': None, 'note_ids': entry['note_ids'], 'document_ids': []}
            target = Path(entry['path'])
            self._regular_files(target.parent)  # Require an accessible real destination.
            archived = self._archived(entry)
            created = False
            if target.exists() or target.is_symlink():
                matches = self._matches(entry, self._regular_files(target.parent))
                if target not in matches:
                    raise ValueError('같은 이름의 다른 파일이 있습니다. 파일을 옮기거나 이름을 바꾼 뒤 복원해 주세요.')
            else:
                if not archived.is_file() or archived.is_symlink():
                    raise ValueError('외부에서 이동한 파일을 원래 폴더로 돌려놓으면 자동으로 다시 연결합니다.')
                # Exclusive creation never overwrites a user's newer file.
                try:
                    with target.open('xb') as stream:
                        created = True
                        with archived.open('rb') as source: shutil.copyfileobj(source, stream)
                        stream.flush(); os.fsync(stream.fileno())
                    shutil.copystat(archived, target, follow_symlinks=False)
                except BaseException:
                    if created: target.unlink(missing_ok=True)
                    raise
            return self._reconnect(entry, target)

    def reconcile_directory(self, directory):
        restored = []
        with self.store.lock, self.library.lock:
            files = self._regular_files(directory)
            entries = [entry for entry in self._all() if entry['kind'] != 'job']
            candidates = {}
            for entry in entries:
                try:
                    candidates[entry['id']] = self._matches(entry, files)
                except OSError:
                    candidates[entry['id']] = []
            for entry in entries:
                try:
                    matches = candidates[entry['id']]
                    unique_owner = (len(matches) == 1 and sum(matches[0] in paths for paths in candidates.values()) == 1)
                    if unique_owner:
                        restored.append(self._reconnect(entry, matches[0]))
                    else:
                        # Finish an interrupted index reservation before the
                        # caller scans and assigns IDs to replacement files.
                        self._reserve_documents(entry)
                except (OSError, ValueError):
                    logger.debug('Deferred retained-note reconnect', exc_info=True)
        return restored

    def _purge(self, entry):
        notes = self._owners(entry)
        self._preflight(notes, entry['documents'])
        if entry['kind'] != 'job':
            # An unavailable external volume is not evidence of continued
            # deletion. Wait until its original directory is accessible.
            self._regular_files(Path(entry['path']).parent)
        folder = self._folder(entry)
        for note in notes:
            owned = self.store.upload_directory(note['id'])
            if owned.exists(): shutil.rmtree(owned)
        for key in self._ids(entry):
            self.summaries.forget(key)
            self.diarization.forget(key)
        for note in notes:
            self.store.delete(note['id'])
        for row in entry['documents']:
            self.library.forget(row['id'])
        if folder.exists(): shutil.rmtree(folder)
        self._forget(entry)

    def sweep(self, *, force=False, now=None, dry_run=False):
        now = time.time() if now is None else now
        removed, relocated, restored, purged = [], [], [], []
        with self.store.lock, self.library.lock:
            hidden = self.hidden_note_ids()
            paths = {note['text_path'] for note in self.store.list()
                     if note['id'] not in hidden and note['status'] == 'done' and note.get('text_path')}
            paths.update(row['path'] for row in self.library.entries() if row['id'] not in hidden)
            paths.update(entry['path'] for entry in self._all() if entry['path'])
            directories = {str(Path(path).parent) for path in paths}
            if not dry_run:
                for directory in directories:
                    try:
                        restored.extend(self.reconcile_directory(directory))
                    except (OSError, ValueError):
                        pass
            hidden = self.hidden_note_ids()
            for value in sorted(paths):
                path = Path(value)
                try:
                    notes, documents = self._group(path)
                    notes = [note for note in notes if note['id'] not in hidden]
                    documents = [row for row in documents if row['id'] not in hidden]
                    if not notes and not documents: continue
                    if path.exists() or path.is_symlink():
                        self.missing_since.pop(value, None)
                        continue
                    files = self._regular_files(path.parent)
                    self._idle(notes, [row['id'] for row in documents])
                    matches = self._replacement(path, notes, documents, files)
                    if len(matches) == 1 and not dry_run:
                        # Use the same durable reconnect path for a move caught
                        # before a deletion grace period has elapsed.
                        entry = self._entry(notes, documents, path=path, kind='missing', now=now)
                        self._save(entry); self._reserve_documents(entry)
                        self._reconnect(entry, matches[0])
                        relocated.append({'source': value, 'target': str(matches[0])})
                        continue
                    if matches: continue
                    first = self.missing_since.setdefault(value, now)
                    if not force and now - first < MISSING_GRACE_SECONDS: continue
                    removed.append({'path': value} if dry_run else self.remove(path, now=now))
                except (OSError, ValueError):
                    logger.debug('Deferred missing-note retention', exc_info=True)
            for entry in self._all():
                if now - entry['deleted_at'] < RETENTION_SECONDS: continue
                try:
                    if not dry_run: self._purge(entry)
                    purged.append(entry['id'])
                except (OSError, ValueError):
                    logger.debug('Deferred expired-note purge', exc_info=True)
        return {'removed': removed, 'relocated': relocated, 'restored': restored, 'purged': purged}
