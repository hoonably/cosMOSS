"""Read the Mac Voice Memos library without changing recordings or its database."""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


class VoiceMemos:
    def __init__(self, folders: list[Path] | None = None):
        home = Path.home()
        self.folders = folders if folders is not None else ([home / folder for folder in (
            'Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings',
            'Library/Containers/com.apple.VoiceMemos/Data/Library/Application Support/Recordings',
            'Library/Application Support/com.apple.voicememos/Recordings',
        )] if sys.platform == 'darwin' else [])
        self.secret = secrets.token_bytes(32)

    def _id(self, path: Path) -> str:
        return hmac.new(self.secret, os.fsencode(path), hashlib.sha256).hexdigest()

    @staticmethod
    def _info(path: Path, folder: Path):
        # Reject traversal and symlinks, including intermediate directories.
        if not path.is_relative_to(folder) or '..' in path.parts:
            return None
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(folder)):
            return None
        try:
            info = path.stat()
            return info if stat.S_ISREG(info.st_mode) and info.st_size > 0 else None
        except FileNotFoundError:
            return None

    @staticmethod
    def _version(info) -> str:
        return hashlib.sha256(f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'.encode()).hexdigest()

    @staticmethod
    def _metadata(folder: Path) -> dict[Path, dict]:
        database = folder / 'CloudRecordings.db'
        if not database.exists() or database.is_symlink():
            return {}
        records = {}
        # No immutable=1: Voice Memos may have recent changes in its WAL.
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
            connection.execute('PRAGMA query_only=ON')
            for table in ('ZCLOUDRECORDING', 'ZRECORDING'):
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                if 'ZPATH' not in columns:
                    continue
                # Voice Memos' CloudRecording.deletionDate is backed by
                # evictionDate (ZEVICTIONDATE). The audio remains on disk in
                # Recently Deleted, so retain a tombstone instead of removing
                # the metadata and accidentally rediscovering it by filename.
                deletion_fields = [name for name in ('ZEVICTIONDATE', 'ZDELETIONDATE') if name in columns]
                deleted = ' OR '.join(f'{name} IS NOT NULL' for name in deletion_fields) or '0'
                # Recent Voice Memos stores the displayed name in encryptedTitle
                # (plain text in the local database). customLabel can instead be
                # an internal ISO date, even for a user-renamed recording.
                fields = ','.join(name if name in columns else 'NULL' for name in (
                    'ZPATH', 'ZENCRYPTEDTITLE', 'ZCUSTOMLABEL', 'ZCUSTOMLABELFORSORTING', 'ZDATE', 'ZDURATION'))
                for raw_path, display_title, custom_label, sorting_label, date, duration, is_deleted in connection.execute(f'SELECT {fields}, ({deleted}) FROM {table}'):
                    if not isinstance(raw_path, str) or not raw_path:
                        continue
                    if raw_path.startswith('file:'):
                        parsed = urlsplit(raw_path)
                        if parsed.netloc not in ('', 'localhost'):
                            continue
                        raw_path = unquote(parsed.path)
                    elif urlsplit(raw_path).scheme:
                        continue
                    path = Path(raw_path)
                    path = path if path.is_absolute() else folder / path
                    if not path.is_relative_to(folder) or '..' in path.parts or path.suffix.lower() != '.m4a':
                        continue
                    try:
                        created = datetime.fromtimestamp(float(date) + 978307200, UTC).isoformat()
                    except (TypeError, ValueError, OverflowError, OSError):
                        created = None
                    seconds = float(duration) if isinstance(duration, (int, float)) else None
                    title = next((value.strip()[:200] for value in (display_title, custom_label, sorting_label)
                                  if isinstance(value, str) and value.strip()), path.stem)
                    item = records.setdefault(path, {'title': title,
                                                     'created_at': created, 'duration': seconds if seconds and math.isfinite(seconds) and seconds > 0 else None,
                                                     '_deleted': False})
                    item['_deleted'] = item['_deleted'] or bool(is_deleted)
        return records

    def _scan(self) -> tuple[list[dict], bool, bool]:
        records, denied, metadata_unavailable = [], False, False
        for folder in self.folders:
            try:
                if not stat.S_ISDIR(folder.stat().st_mode) or folder.is_symlink():
                    continue
                paths = {path for path in folder.iterdir() if path.suffix.lower() == '.m4a'}
                try:
                    metadata = self._metadata(folder)
                except sqlite3.Error:
                    metadata, metadata_unavailable = {}, True
                paths.update(metadata)
                for path in paths:
                    if metadata.get(path, {}).get('_deleted'):
                        continue
                    info = self._info(path, folder)
                    if info is None and (path.is_symlink() or path.exists()):
                        continue
                    item = metadata.get(path, {'title': path.stem, 'created_at': None, 'duration': None})
                    records.append({**item, 'id': self._id(path), '_path': path, '_folder': folder,
                                    'created_at': item['created_at'] or (datetime.fromtimestamp(info.st_mtime, UTC).isoformat() if info else None),
                                    'size': info.st_size if info else None, 'available': info is not None,
                                    'version': self._version(info) if info else None})
            except FileNotFoundError:
                continue
            except PermissionError:
                denied = True
        return sorted(records, key=lambda item: item['created_at'] or '', reverse=True), denied, metadata_unavailable

    def list(self) -> dict:
        records, denied, metadata_unavailable = self._scan()
        return {'recordings': [{key: value for key, value in item.items() if not key.startswith('_')} for item in records],
                'permission_required': denied, 'metadata_unavailable': metadata_unavailable}

    def open(self, recording_id: str, version: str):
        records, denied, _ = self._scan()
        item = next((item for item in records if hmac.compare_digest(item['id'], recording_id)), None)
        if not item:
            if denied:
                raise PermissionError('음성 메모 폴더에 접근할 권한이 없습니다.')
            raise FileNotFoundError('녹음을 찾을 수 없습니다. 목록을 새로고침해 주세요.')
        if not item['available']:
            raise FileNotFoundError('이 Mac에 다운로드되지 않은 녹음입니다. 음성 메모에서 먼저 다운로드해 주세요.')
        # Walk with directory descriptors so a replaced parent cannot escape the library.
        folder_fd = os.open(item['_folder'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            relative = item['_path'].relative_to(item['_folder'])
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder_fd)
                os.close(folder_fd)
                folder_fd = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder_fd)
        finally:
            os.close(folder_fd)
        stream = os.fdopen(fd, 'rb')
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or self._version(info) != version:
            stream.close()
            raise ValueError('녹음이 변경되었습니다. 목록을 새로고침한 뒤 다시 선택해 주세요.')
        return stream, item
