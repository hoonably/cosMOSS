"""Recoverable file deletion and transcript conversion without inference."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from src.text_files import FORMATS, TextConflict, digest
from src.text_library import decode_text, transcript_fields
from src.subtitles import subtitle_fields
from src.transcript import format_vtt_time


def read_file(note, expected_hash=None, mtime_ns=None, size=None, file_version=None):
    if note['status'] != 'done' or not note.get('text_path'):
        raise ValueError('전사가 완료되고 저장된 파일에서 사용할 수 있습니다.')
    path = Path(note['text_path'])
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or not path.is_file() or path.suffix.lower()[1:] not in FORMATS):
        raise ValueError('파일을 찾을 수 없습니다. 목록을 다시 확인해 주세요.')
    info = path.stat()
    if info.st_size > 32*1024*1024:
        raise ValueError('32MB 이하의 전사 파일을 선택해 주세요.')
    if ((file_version is not None and file_version != f'{info.st_mtime_ns}:{info.st_size}')
            or (mtime_ns is not None and info.st_mtime_ns != mtime_ns)
            or (size is not None and info.st_size != size)):
        raise TextConflict('파일이 변경되었습니다. 다시 불러온 뒤 시도해 주세요.')
    content = path.read_bytes()
    actual = digest(content)
    expected = expected_hash or (note.get('text_hash') if note.get('source_type') != 'text_file' else None)
    if expected and actual != expected:
        raise TextConflict('파일이 다른 내용으로 변경되었습니다. 목록에서 최신 파일을 다시 선택해 주세요.')
    return path, content


def rename_file(note, title, **version):
    path, content = read_file(note, **version)
    stem = title.strip()
    if stem.lower().endswith(path.suffix.lower()):
        stem = stem[:-len(path.suffix)].strip()
    if not stem or stem in {'.', '..'} or any(c in '/\\:' or ord(c) < 32 or ord(c) == 127 for c in stem):
        raise ValueError('파일명에는 빈 이름이나 /, \\, :, 줄바꿈을 사용할 수 없습니다.')
    if len(os.fsencode(stem + path.suffix)) > 255:
        raise ValueError('파일명이 너무 깁니다. 이름을 줄여 주세요.')
    target = path.with_name(stem + path.suffix)
    if target == path:
        return target
    # A case-only rename on a case-insensitive filesystem names the same entry.
    if str(target).casefold() == str(path).casefold() and target.exists() and not target.is_symlink() and target.samefile(path):
        path.rename(target)
        return target
    identity = path.stat()
    try:
        # Exclusive link creation never replaces an existing file, even if it
        # appears after validation. Moving within a folder preserves all bytes,
        # encoding, permissions, timestamps and file identity.
        os.link(path, target, follow_symlinks=False)
    except FileExistsError:
        raise TextConflict('같은 이름의 파일이 있습니다. 다른 이름을 입력해 주세요.') from None
    try:
        checked, current = read_file(note, **version)
        if current != content or not checked.samefile(target):
            raise TextConflict('파일이 변경되었습니다. 다시 불러온 뒤 이름을 바꿔 주세요.')
        path.unlink()
    except Exception:
        if target.exists() and not target.is_symlink() and (target.stat().st_dev, target.stat().st_ino) == (identity.st_dev, identity.st_ino):
            target.unlink()
        raise
    return target


def convert_file(note, target_format, **version):
    path, content = read_file(note, **version)
    current_format = path.suffix.lower()[1:]
    if target_format not in FORMATS or target_format == current_format:
        raise ValueError('현재 파일과 다른 TXT·SRT·VTT 형식을 선택해 주세요.')
    text = decode_text(content)[0]
    if current_format == 'txt' and note.get('source_type') != 'text_file' and note.get('segments'):
        segments, names = note['segments'], note['speaker_names']
    else:
        segments, names = transcript_fields(text) if current_format == 'txt' else subtitle_fields(text, current_format)
    if not segments or any(s['end'] <= s['start'] for s in segments):
        raise ValueError('발화의 시작·종료 시간 정보가 필요합니다. 자막 파일이나 앱에서 전사한 원본을 선택해 주세요.')
    if target_format == 'txt':
        # Keep both timestamps so converting this TXT back to subtitles is exact.
        result = ''.join(f"[{format_vtt_time(s['start'])}-{format_vtt_time(s['end'])}] "
                         f"{names.get(s['speaker'],s['speaker'])}: {s['text']}\n" for s in segments)
    else:
        result = FORMATS[target_format](segments, names)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.cosmoss-',delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(result.encode('utf-8')); stream.flush(); os.fsync(stream.fileno())
        number = 0
        while True:
            target = path.with_name(f"{path.stem}{f'-{number}' if number else ''}.{target_format}")
            try:
                os.link(temporary,target)
                return target
            except FileExistsError:
                number += 1
    finally:
        if temporary: temporary.unlink(missing_ok=True)


def trash_file(path):
    if sys.platform != 'darwin':
        raise ValueError('휴지통으로 이동은 Mac 앱에서 지원합니다.')
    # Foundation moves to the volume's Trash. No Finder automation permission,
    # shell interpolation, or fallback to permanent deletion is involved.
    script = """ObjC.import('Foundation'); function run(argv) {
      const error = Ref();
      if (!$.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError(
          $.NSURL.fileURLWithPath(argv[0]), null, error))
        throw Error('파일을 휴지통으로 이동하지 못했습니다.');
      return true;
    }"""
    try:
        subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script,str(path)],
                       check=True,capture_output=True,text=True,timeout=15)
    except (OSError,subprocess.SubprocessError) as error:
        raise ValueError('파일을 휴지통으로 옮기지 못했습니다. 파일 접근 권한을 확인해 주세요.') from error
