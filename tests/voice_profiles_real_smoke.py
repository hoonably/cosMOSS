"""Exercise voice enrollment with real user audio; preserve every original.

python tests/voice_profiles_real_smoke.py RUNTIME_ROOT NOTE_ID TXT_FILE [SCREENSHOT_DIR]
The installed CPU model is reused. Only the existing ASR result is replayed;
this checks the pipeline, not recognition accuracy or a fresh MOSS inference.
"""
import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import TranscriptionConfig
from src.devices import DeviceStore, ResolvedDevice
from src.media_tools import MediaTools
from src.store import NoteStore
from src.text_files import write_text
from src.text_library import TextLibrary
from src.voice_engine import VoiceEngine
from src.voice_profiles import VoiceProfiles


def checksum(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024): result.update(block)
    return result.hexdigest()


async def main():
    runtime, note_id, original_txt = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    screenshot_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    with sqlite3.connect(f'file:{runtime}/data/moss-note.sqlite3?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute('SELECT * FROM notes WHERE id=?', (note_id,)).fetchone())
    source = runtime / 'data' / (original['normalized_path'] or original['source_path'])
    source_hash, text_hash = checksum(source), checksum(original_txt)
    engine = VoiceEngine(runtime)
    await engine.require_ready()
    media = MediaTools(runtime / '.cache/media-tools')
    original_segments = json.loads(original['segments_json'])
    ranges = VoiceProfiles.ranges(original_segments)
    speaker = next(speaker for speaker, selections in ranges.items() if selections and selections[0][1] == 5.)
    start, seconds = ranges[speaker][0]
    real_segment = next(segment for segment in original_segments if segment['speaker'] == speaker and segment['start'] <= start and segment['end'] >= start + seconds)
    # Reuse real ASR words and a real five-second excerpt without running MOSS again.
    segments = [{**real_segment, 'id': 0, 'start': 0., 'end': 5., 'speaker': 'S01'}]
    with tempfile.TemporaryDirectory(prefix='cosmoss-real-voice-') as temporary:
        root = Path(temporary)
        data = root / 'data'
        profiles = VoiceProfiles(data, engine)
        clip = root / 'real-sample.wav'
        await profiles.clip(source, clip, start, seconds, media.executable('ffmpeg'))
        clip_hash = checksum(clip)
        # Preserve a copy of the existing Downloads transcript for indexing/edit QA.
        output = root / 'output'
        output.mkdir()
        copied_txt = output / original_txt.name
        shutil.copy2(original_txt, copied_txt)
        from src import main as app
        store = NoteStore(data / 'moss-note.sqlite3')
        replacements = {'STORE': store, 'VOICE_ENGINE': engine, 'VOICE_PROFILES': profiles,
                        'MEDIA_TOOLS': media, 'DEVICES': DeviceStore(data / 'devices.sqlite3'),
                        'TEXT_LIBRARY': TextLibrary(data / 'text-library.sqlite3')}
        from contextlib import ExitStack
        with ExitStack() as patches:
            for key, value in replacements.items(): patches.enter_context(patch.object(app, key, value))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://127.0.0.1') as client:
                response = await client.post('/api/voice-profiles/enroll', data={'name': '파이프라인 확인'}, files={'file': ('sample.wav', clip.read_bytes(), 'audio/wav')})
                assert response.status_code == 201, response.text
                profile_id = response.json()['id']
                exported = await client.get(f'/api/voice-profiles/{profile_id}/export')
                assert exported.status_code == 200
                (root / 'real-profile.mossvoice').write_bytes(exported.content)
                assert (await client.delete(f'/api/voice-profiles/{profile_id}')).status_code == 204
                response = await client.post('/api/voice-profiles/import', files={'file': ('real.mossvoice', exported.content)})
                assert response.status_code == 200, response.text
                assert response.json()['id'] != profile_id
                for fmt in ('txt', 'srt', 'vtt'):
                    identity = f'real-{fmt}'
                    folder = store.upload_directory(identity)
                    folder.mkdir(parents=True)
                    owned = folder / 'source.wav'
                    shutil.copy2(clip, owned)
                    store.create({'id': identity, 'title': '실제 녹음 확인 ' + fmt, 'original_filename': 'source.wav',
                                  'source_path': str(owned), 'normalized_path': str(owned), 'duration': 5.,
                                  'status': 'queued', 'output_format': fmt, 'output_directory': str(output)})
                    target = ResolvedDevice(TranscriptionConfig.from_env({}), 'moss', '이 Mac', 'local')
                    with patch.object(app, 'resolve_device', new=AsyncMock(return_value=target)), \
                         patch.object(app, 'run_audio_work', new=AsyncMock(side_effect=[[(owned, 0.)], []])), \
                         patch.object(app, 'transcribe_chunk', new=AsyncMock(return_value=('', segments))):
                        await app.transcribe(identity)
                    result = store.get(identity)
                    assert result['status'] == 'done', result.get('error')
                    assert not result['voice_match_error'], result['voice_match_error']
                    assert result['speaker_names']['S01'] == '파이프라인 확인'
                    assert '파이프라인 확인' in Path(result['text_path']).read_text()
                    assert not result['text_save_error']
                    assert store.resolve_path(identity, result['source_path']).is_file()
                # Manual identification remains a draft until the explicit Save endpoint.
                note = store.update('real-txt', speaker_names={'S01': 'S01'})
                store.update('real-txt', **write_text(note))
                before = Path(note['text_path']).read_bytes()
                preview = await client.post('/api/notes/real-txt/identify-speakers', json={})
                assert preview.status_code == 200, preview.text
                assert preview.json()['speaker_names'] == {'S01': '파이프라인 확인'}
                assert store.get('real-txt')['speaker_names'] == {'S01': 'S01'}
                assert Path(note['text_path']).read_bytes() == before
                response = await client.post('/api/notes/real-txt/speakers', json={'speaker_names': preview.json()['speaker_names']})
                assert response.status_code == 200, response.text
                assert '파이프라인 확인' in Path(note['text_path']).read_text()
        assert checksum(clip) == clip_hash
        assert checksum(copied_txt) == text_hash
        if screenshot_dir:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            await browser_qa(root, runtime, clip, output, screenshot_dir)
    assert checksum(source) == source_hash, 'Original recording changed'
    assert checksum(original_txt) == text_hash, 'Original Downloads transcript changed'
    print('PASS: real audio enrollment, portable roundtrip, actual CPU embeddings, automatic TXT/SRT/VTT naming, manual draft/save, original preservation; accuracy not evaluated')


async def browser_qa(root, runtime, clip, output, screenshots):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    environment = {**os.environ, 'MOSS_DATA_DIR': str(root / 'data'), 'MOSS_RUNTIME_DIR': str(runtime),
                   'PYTHONDONTWRITEBYTECODE': '1'}
    log_path = root / 'browser-backend.log'
    with log_path.open('wb') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'src.main:app', '--host', '127.0.0.1', '--port', str(port)], env=environment, stdout=log, stderr=log)
        try:
            base = f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    if process.poll() is not None: raise AssertionError(log_path.read_text())
                    try:
                        if (await client.get(base + '/healthz')).status_code == 200: break
                    except httpx.HTTPError: pass
                    await asyncio.sleep(.1)
                else: raise AssertionError('QA backend failed to start')
            from voice_profiles_ui_smoke import run
            await run(base, clip, output, screenshots)
        finally:
            process.terminate()
            try: await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)


if __name__ == '__main__':
    asyncio.run(main())
