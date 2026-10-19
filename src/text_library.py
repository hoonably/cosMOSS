"""Index TXT, SRT and VTT in place. Scanning and opening never rewrite them."""
from __future__ import annotations

import codecs
import hashlib
import os
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from src.text_files import TextConflict, digest, speaker_changes
from src.subtitles import subtitle_fields, rename_subtitle_speakers

SUPPORTED_FORMATS = {"txt", "srt", "vtt"}


HEADER = re.compile(r"^\[(?P<time>\d+:\d{2}:\d{2}(?:\.\d+)?)\][ \t]*(?P<name>[^:\r\n]{1,60}):[ \t]*", re.MULTILINE)


def decode_text(content: bytes) -> tuple[str, str, bytes]:
    for prefix, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if content.startswith(prefix):
            return content[len(prefix):].decode(encoding), encoding, prefix
    for encoding in ("utf-8", "cp949"):
        try:
            text = content.decode(encoding)
            if "\x00" in text:
                raise ValueError("텍스트 인코딩을 확인해 주세요. UTF-8 또는 BOM이 있는 UTF-16을 지원합니다.")
            return text, encoding, b""
        except UnicodeDecodeError:
            continue
    raise ValueError("파일 인코딩을 읽지 못했습니다. UTF-8로 저장한 뒤 다시 열어 주세요.")


def transcript_fields(text: str) -> tuple[list[dict], dict[str, str]]:
    headers = list(HEADER.finditer(text))
    if not headers or text[:headers[0].start()].strip():
        return [], {}
    labels, segments = {}, []
    for index, match in enumerate(headers):
        label = match["name"].strip()
        key = labels.setdefault(label, f"S{len(labels) + 1:02d}")
        hours, minutes, seconds = map(float, match["time"].split(":"))
        start = hours * 3600 + minutes * 60 + seconds
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        segments.append({"id": index, "start": start, "end": start, "speaker": key, "text": text[match.end():end].strip()})
    return segments, {key: label for label, key in labels.items()}


class TextLibrary:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    @staticmethod
    def is_document(note_id: str) -> bool:
        return note_id.startswith("txt_")

    def scan(self, directory: str, managed_paths=()) -> list[dict]:
        folder = Path(directory).expanduser() if directory else Path.home() / "Downloads"
        if not folder.is_absolute():
            raise ValueError("저장 폴더는 절대 경로로 지정해 주세요.")
        folder = folder.resolve()
        if not folder.is_dir():
            raise ValueError("전사 파일 폴더를 찾을 수 없습니다. 기본 경로를 다시 선택해 주세요.")
        managed = {str(Path(path).expanduser().resolve()) for path in managed_paths if path}
        notes, rows = [], []
        for path in folder.iterdir():
            try:
                if path.suffix.lower().lstrip(".") not in SUPPORTED_FORMATS or path.is_symlink() or not path.is_file() or str(path) in managed:
                    continue
                info = path.stat()
            except OSError:
                continue
            note_id = "txt_" + hashlib.sha256(os.fsencode(path)).hexdigest()[:32]
            rows.append((note_id, str(path)))
            notes.append(self._metadata(note_id, path, info))
        with self.lock, self._connect() as connection:
            connection.executemany("INSERT INTO documents VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET path=excluded.path", rows)
        return sorted(notes, key=lambda note: (note["updated_at"], note["title"]), reverse=True)

    @staticmethod
    def _metadata(note_id, path, info):
        changed = datetime.fromtimestamp(info.st_mtime, UTC).isoformat()
        return {"id": note_id, "title": path.stem, "original_filename": path.name, "status": "done",
                "source_type": "text_file", "text_path": str(path), "created_at": changed, "updated_at": changed,
                "file_mtime_ns": info.st_mtime_ns, "file_size": info.st_size, "device_name": path.suffix[1:].upper(), "output_format": path.suffix[1:].lower(), "chunk_count": 1,
                "duration": None, "processing_seconds": None}

    def path(self, note_id):
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT path FROM documents WHERE id = ?", (note_id,)).fetchone()
        if not row:
            raise KeyError(note_id)
        path = Path(row[0])
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError("전사 파일을 찾을 수 없습니다. 저장 폴더에서 파일을 다시 확인해 주세요.")
        return path

    def get(self, note_id):
        path = self.path(note_id)
        info = path.stat()
        if info.st_size > 32 * 1024 * 1024:
            raise ValueError("파일이 너무 큽니다. 32MB 이하의 파일을 열어 주세요.")
        content = path.read_bytes()
        text, _, _ = decode_text(content)
        format_name = path.suffix[1:].lower()
        segments, names = transcript_fields(text) if format_name == "txt" else subtitle_fields(text, format_name)
        return {**self._metadata(note_id, path, info), "text_body": text, "text_hash": digest(content),
                "segments": segments, "raw_segments": segments, "speaker_names": names,
                "progress_phase": "done"}

    def update(self, note_id, changes, expected_hash):
        if not expected_hash:
            raise TextConflict("파일을 다시 열어 최신 내용을 확인한 뒤 저장해 주세요.")
        if set(changes) - {"text_body", "speaker_names", "speaker_merges"}:
            raise ValueError("전사 파일 이름은 Finder에서 변경해 주세요.")
        with self.lock:
            note = self.get(note_id)
            path = self.path(note_id)
            current = path.read_bytes()
            if digest(current) != expected_hash:
                raise TextConflict("파일이 다른 앱에서 수정되었습니다. 원문을 덮어쓰지 않았습니다. 변경 내용을 확인해 주세요.")
            text, encoding, prefix = decode_text(current)
            if "text_body" in changes:
                text = changes["text_body"]
            if "speaker_names" in changes or "speaker_merges" in changes:
                names = speaker_changes(note, changes.get("speaker_names", {}), changes.get("speaker_merges"))["speaker_names"]
                replacements = {label: names[key] for key, label in note["speaker_names"].items()}
                if note["output_format"] == "txt":
                    text = HEADER.sub(lambda match: match.group()[:match.start("name") - match.start()] + replacements.get(match["name"].strip(), match["name"]) + match.group()[match.end("name") - match.start():], text)
                else:
                    text = rename_subtitle_speakers(text, note["output_format"], replacements)
            if "\r\n" in note["text_body"]:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            content = prefix + text.encode(encoding)
            if content != current:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cosmoss-", delete=False) as stream:
                        temporary = Path(stream.name)
                        os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if self.path(note_id) != path or digest(path.read_bytes()) != expected_hash:
                        raise TextConflict("파일이 다른 앱에서 수정되었습니다. 원문을 덮어쓰지 않았습니다.")
                    os.replace(temporary, path)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
            return self.get(note_id)
