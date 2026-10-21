"""Browser-compatible playback reuses prepared audio and supports seeking."""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

from src.store import NoteStore


class SpeakerPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = NoteStore(Path(temporary.name) / 'notes.sqlite3')
        self.identity = 'b' * 32
        folder = self.store.upload_directory(self.identity)
        folder.mkdir(parents=True)
        self.source = folder / 'original.m4a'
        self.source.write_bytes(b'original recording')
        self.normalized = folder / 'normalized.wav'
        with wave.open(str(self.normalized), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x00\x00' * 32000)
        self.note = self.store.create(dict(id=self.identity, status='done', title='fixture',
            original_filename='recording.m4a', media_type='audio/mp4',
            source_path=str(self.source), normalized_path=str(self.normalized)))
        self.enterContext(patch.object(main, 'STORE', self.store))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)

    async def test_playback_uses_existing_wav_and_range_without_changing_original(self):
        url = f'/api/notes/{self.identity}/media?playback=true'
        response = await self.client.get(url, headers={'Range': 'bytes=0-43'})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.headers['content-type'], 'audio/wav')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.content, self.normalized.read_bytes()[:44])
        self.assertEqual(self.source.read_bytes(), b'original recording')
        response = await self.client.get(f'/api/notes/{self.identity}/media')
        self.assertEqual(response.content, b'original recording')
        self.assertEqual(response.headers['content-type'], 'audio/mp4')

    async def test_missing_prepared_audio_falls_back_to_original(self):
        self.normalized.unlink()
        response = await self.client.get(f'/api/notes/{self.identity}/media?playback=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'original recording')

    async def test_missing_recording_and_bad_path_fail_without_serving_other_files(self):
        self.normalized.unlink()
        self.source.unlink()
        response = await self.client.get(f'/api/notes/{self.identity}/media?playback=true')
        self.assertEqual(response.status_code, 404)
        with patch.object(self.store, 'get', return_value={**self.note, 'normalized_path': '../../other.wav'}):
            response = await self.client.get(f'/api/notes/{self.identity}/media?playback=true')
            self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
