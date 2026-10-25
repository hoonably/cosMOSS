"""Clean up private speaker-analysis records left by older app versions."""
from pathlib import Path


class SpeakerRecords:
    def __init__(self, data: Path):
        self.directory = data / 'diarization'

    def forget(self, note_id):
        if not note_id or Path(note_id).name != note_id or note_id in {'.', '..'}:
            raise ValueError('올바르지 않은 노트입니다.')
        for suffix in ('.json', '.tmp'):
            (self.directory / (note_id + suffix)).unlink(missing_ok=True)
