"""Split an existing recording by count using real ffmpeg, without inference."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    source = Path(sys.argv[1]).resolve(strict=True)
    original = digest(source), source.stat().st_mtime_ns
    with tempfile.TemporaryDirectory(prefix='cosmoss-real-chunks-') as folder:
        root = Path(folder)
        os.environ['MOSS_DATA_DIR'] = str(root / 'data')
        os.environ['MOSS_RUNTIME_DIR'] = str(root)
        from src.main import count_chunk_settings, normalize_audio, probe_duration, split_audio
        normalized = root / 'normalized.wav'
        normalize_audio(source, normalized)
        duration = probe_duration(normalized)
        assert duration and duration > 0
        for count in (1, 2, 3):
            seconds = count_chunk_settings(duration, count)['chunk_seconds']
            chunks = split_audio(normalized, duration, seconds)
            assert len(chunks) == count, (count, chunks)
            for index, (path, offset) in enumerate(chunks):
                assert abs(offset - index * duration / count) < .000001
                assert abs(probe_duration(path) - duration / count) < .002
            print(f'PASS real recording: {count} chunk(s), {seconds:.6f} seconds each; timestamps and media durations agree')
    assert (digest(source), source.stat().st_mtime_ns) == original
    print('Original recording unchanged; temporary audio removed; no model or external AI calls')


if __name__ == '__main__':
    main()
