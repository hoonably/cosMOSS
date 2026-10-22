"""Save transcript files and keep edits in sync without overwriting outside edits."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from src.transcript import plain_text, srt_text, vtt_text


FORMATS = {"txt": plain_text, "srt": srt_text, "vtt": vtt_text}


def file_format(note: dict) -> str:
    suffix = Path(note.get("text_path") or "").suffix.lower().lstrip(".")
    format_name = suffix or note.get("output_format", "txt")
    if format_name not in FORMATS:
        raise ValueError("TXT·SRT·VTT 중 저장 형식을 선택해 주세요.")
    return format_name


def transcript_content(note: dict) -> str:
    return FORMATS[file_format(note)](note["segments"], note["speaker_names"])


class TextConflict(ValueError):
    pass


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def speaker_names(note: dict, names: dict[str, str]) -> dict[str, str]:
    known = {s["speaker"] for s in note["segments"] + note["raw_segments"]}
    if set(names) - known:
        raise ValueError("이 노트에 없는 화자입니다.")
    result = dict(note["speaker_names"])
    for key, value in names.items():
        value = value.strip() or key
        if len(value) > 60 or any(c in value for c in "\r\n\x00"):
            raise ValueError("화자 이름은 줄바꿈 없이 60자까지 입력할 수 있습니다.")
        result[key] = value
    return result


def speaker_changes(note: dict, names: dict[str, str], merges: dict[str, str] | None = None) -> dict:
    """Merge speaker identities while preserving the original ASR segments."""
    names = speaker_names(note, names)
    existing = note.get("speaker_merges", {})
    known = {s["speaker"] for s in note["segments"] + note["raw_segments"]}
    active = known - existing.keys()
    merges = merges or {}
    if any(source not in active or target not in active or source == target for source, target in merges.items()):
        raise ValueError("서로 다른 두 화자를 선택해 병합해 주세요.")
    mapping = {**existing, **merges}
    resolved = {}
    for source in mapping:
        target, seen = source, set()
        while target in mapping:
            if target in seen:
                raise ValueError("화자 병합 관계가 순환합니다. 다시 선택해 주세요.")
            seen.add(target)
            target = mapping[target]
        resolved[source] = target
    for source, target in resolved.items():
        names[source] = names.get(target, target)
    return {"speaker_names": names, "speaker_merges": resolved,
            "segments": [{**segment, "speaker": resolved.get(segment["speaker"], segment["speaker"])} for segment in note["segments"]]}


def write_text(note: dict, directory: str = "", *, reuse_existing: bool = False) -> dict[str, str]:
    if note["status"] != "done":
        raise ValueError("전사가 완료된 노트만 파일로 저장할 수 있습니다.")
    format_name = file_format(note)
    content = transcript_content(note).encode("utf-8")
    content_hash = digest(content)
    if note.get("text_path"):
        target = Path(note["text_path"])
        if not target.is_absolute() or target.suffix.lower().lstrip(".") not in FORMATS or target.is_symlink():
            raise ValueError("전사 파일 경로를 확인해 주세요.")
        if not target.exists():
            raise TextConflict("전사 파일이 이동되었거나 삭제되었습니다. 목록에서 파일을 다시 선택해 주세요.")
        if target.exists():
            actual_hash = digest(target.read_bytes())
            if actual_hash not in {note.get("text_hash"), content_hash}:
                raise TextConflict("파일이 다른 앱에서 수정되었습니다. 외부 변경 내용을 확인한 뒤 다시 저장해 주세요.")
    else:
        folder = Path(directory).expanduser() if directory else Path.home() / "Downloads"
        if not folder.is_absolute():
            raise ValueError("저장 폴더는 절대 경로로 지정해 주세요.")
        folder = folder.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        title = "".join(c for c in note["title"] if c.isalnum() or c in "-_ ").strip()[:80] or "노트"
        # Reconnect an old manual export without rewriting it or listing it twice.
        candidate = folder / f"{title}.{format_name}"
        if reuse_existing and candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size == len(content) and digest(candidate.read_bytes()) == content_hash:
            return {"text_path": str(candidate), "text_hash": content_hash}
        stem = title
        target = folder / f"{stem}.{format_name}"
        suffix = 1
        while os.path.lexists(target):
            target = folder / f"{stem}-{suffix}.{format_name}"
            suffix += 1
    # Do not recreate an old, removed save folder at a surprising location.
    if not target.parent.is_dir():
        raise ValueError("저장 폴더를 찾을 수 없습니다. 폴더를 다시 연결해 주세요.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".cosmoss-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {"text_path": str(target), "text_hash": content_hash}


def save_text(store, note_id: str, directory: str = "", changes: dict | None = None, *, reuse_existing: bool = False) -> dict:
    with store.lock:
        note = store.get(note_id)
        changes = dict(changes or {})
        if "speaker_names" in changes or "speaker_merges" in changes:
            changes.update(speaker_changes(note, changes.get("speaker_names", {}), changes.get("speaker_merges")))
        metadata = write_text({**note, **changes}, directory, reuse_existing=reuse_existing)
        return store.update(note_id, **changes, **metadata, text_save_error=None)
