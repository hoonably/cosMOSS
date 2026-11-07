"""Join prepared model inputs by copying PCM frames, without another encoding."""
from contextlib import ExitStack
from pathlib import Path
import time
import wave


def join_recordings(store, uploads, identity):
    folder = store.upload_directory(identity)
    folder.mkdir(parents=True)
    destination = folder / 'normalized.wav'
    started = time.monotonic()
    with ExitStack() as stack:
        recordings = [stack.enter_context(wave.open(str(store.resolve_path(
            upload['id'], upload['normalized_path'])), 'rb')) for upload in uploads]
        formats = [(r.getnchannels(), r.getsampwidth(), r.getframerate(), r.getcomptype()) for r in recordings]
        if any(value != (1, 2, 16000, 'NONE') for value in formats):
            raise ValueError('녹음 준비 형식을 확인할 수 없습니다. 파일을 다시 선택해 주세요.')
        frames = sum(r.getnframes() for r in recordings)
        if not frames or frames * 2 > 0xFFFFFFFF - 36:
            raise ValueError('이어붙일 녹음이 비어 있거나 너무 깁니다. 선택한 파일 수를 줄여 주세요.')
        with wave.open(str(destination), 'wb') as output:
            output.setparams((1, 2, 16000, frames, 'NONE', 'not compressed'))
            for recording in recordings:
                remaining = recording.getnframes()
                while remaining:
                    chunk = recording.readframes(min(remaining, 32768))
                    if not chunk or len(chunk) % 2:
                        raise ValueError('녹음 파일이 불완전합니다. 파일을 다시 선택해 주세요.')
                    output.writeframesraw(chunk)
                    remaining -= len(chunk) // 2
    return dict(id=identity, original_filename=f"{Path(uploads[0]['original_filename']).stem} 외 {len(uploads)-1}개.wav",
                media_type='audio/wav', source_path=str(destination), normalized_path=str(destination),
                duration=frames / 16000, preprocessing_seconds=round(time.monotonic() - started +
                    sum(upload.get('preprocessing_seconds') or 0 for upload in uploads), 3))
