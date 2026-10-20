"""Portable voice enrollment and local, non-authentication speaker matching."""
from __future__ import annotations

import array
import base64
import binascii
import io
import json
import math
import os
import sqlite3
import tempfile
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

from src.text_files import digest
from src.voice_engine import MODEL_ID, VoiceEngine

PROFILE_FORMAT = 'cosmoss.voice-profile'
MAX_PROFILE_BYTES = 2 * 1024 * 1024


def name_value(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60 or any(c in value for c in '\r\n\x00'):
        raise ValueError('화자 이름을 줄바꿈 없이 60자 이내로 입력해 주세요.')
    return value.strip()


def unit_vector(values):
    if not isinstance(values, list) or len(values) != 192 or any(type(v) not in (float, int) or abs(v) > 1000 or not math.isfinite(v) for v in values):
        raise ValueError('목소리 특징 데이터가 올바르지 않습니다.')
    norm = math.sqrt(sum(value * value for value in values))
    if norm < 1e-8:
        raise ValueError('목소리 특징 데이터가 비어 있습니다.')
    return [value / norm for value in values]


def wave_duration(content):
    try:
        with wave.open(io.BytesIO(content), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError('목소리 샘플 형식이 올바르지 않습니다.')
            frames = audio.getnframes()
            if not 16000 <= frames <= 320000 or len(audio.readframes(frames + 1)) != frames * 2:
                raise ValueError('목소리 샘플은 1~20초 PCM WAV여야 합니다.')
            return frames / 16000
    except (wave.Error, EOFError) as error:
        raise ValueError('목소리 샘플을 읽지 못했습니다.') from error


class VoiceProfiles:
    def __init__(self, data: Path, engine: VoiceEngine):
        self.folder = data / 'voice-profiles'
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.folder / 'profiles.sqlite3'
        self.engine = engine
        self.operations = set()
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, sample_hash TEXT NOT NULL, document TEXT NOT NULL, UNIQUE(name,sample_hash))')
            connection.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.database.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def settings(self):
        with self.connect() as connection:
            result = dict(connection.execute('SELECT key,value FROM settings'))
        return {'enabled': json.loads(result.get('enabled', 'true')), 'threshold': json.loads(result.get('threshold', '0.75')), 'margin': 0.10}

    def configure(self, enabled, threshold):
        if type(enabled) is not bool or type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError('유사도 기준은 0~1 사이로 지정해 주세요.')
        with self.connect() as connection:
            connection.executemany('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   [('enabled', json.dumps(enabled)), ('threshold', json.dumps(threshold))])
        return self.settings()

    def documents(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT document FROM profiles ORDER BY rowid DESC')]

    def get(self, profile_id):
        with self.connect() as connection:
            row = connection.execute('SELECT document FROM profiles WHERE id=?', (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        return json.loads(row[0])

    @staticmethod
    def metadata(document):
        return {key: document[key] for key in ('id', 'name', 'created_at', 'duration', 'model_id')}

    def listing(self):
        return {'profiles': [self.metadata(item) for item in self.documents()], 'settings': self.settings(), 'engine': self.engine.status()}

    def store(self, name, embedding, sample):
        name, vector = name_value(name), unit_vector(embedding)
        duration = wave_duration(sample)
        sample_hash = digest(sample)
        with self.connect() as connection:
            existing = connection.execute('SELECT document FROM profiles WHERE name=? AND sample_hash=?', (name, sample_hash)).fetchone()
            if existing:
                return self.metadata(json.loads(existing[0]))
            document = {'format': PROFILE_FORMAT, 'version': 1, 'id': uuid.uuid4().hex, 'name': name,
                        'created_at': datetime.now(UTC).isoformat(), 'model_id': MODEL_ID, 'duration': duration,
                        'embedding': vector, 'sample_sha256': sample_hash, 'sample_wav': base64.b64encode(sample).decode('ascii')}
            connection.execute('INSERT INTO profiles VALUES (?,?,?,?)', (document['id'], name, sample_hash, json.dumps(document, ensure_ascii=False)))
        return self.metadata(document)

    def import_file(self, content):
        if len(content) > MAX_PROFILE_BYTES:
            raise ValueError('목소리 프로필은 2MB 이하만 가져올 수 있습니다.')
        try:
            document = json.loads(content)
            if not isinstance(document, dict) or document.get('format') != PROFILE_FORMAT or type(document.get('version')) is not int or document.get('version') != 1 or document.get('model_id') != MODEL_ID:
                raise ValueError('지원하지 않는 .mossvoice 프로필 버전 또는 모델입니다.')
            sample = base64.b64decode(document['sample_wav'], validate=True)
            if digest(sample) != document['sample_sha256']:
                raise ValueError('목소리 샘플의 무결성을 확인하지 못했습니다.')
            return self.store(document['name'], document['embedding'], sample)
        except (KeyError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as error:
            raise ValueError('올바른 .mossvoice 파일을 선택해 주세요.') from error

    def export(self, profile_id):
        document = self.get(profile_id)
        return json.dumps(document, ensure_ascii=False, allow_nan=False).encode('utf-8')

    def rename(self, profile_id, name):
        document = self.get(profile_id)
        document['name'] = name_value(name)
        try:
            with self.connect() as connection:
                connection.execute('UPDATE profiles SET name=?,document=? WHERE id=?', (document['name'], json.dumps(document, ensure_ascii=False), profile_id))
        except sqlite3.IntegrityError as error:
            raise ValueError('같은 이름과 목소리가 이미 등록되어 있습니다.') from error
        return self.metadata(document)

    def delete(self, profile_id):
        self.get(profile_id)
        with self.connect() as connection:
            connection.execute('DELETE FROM profiles WHERE id=?', (profile_id,))

    async def clip(self, source, destination, start, seconds, ffmpeg):
        await self.engine.command([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-ss', str(start),
            '-i', source, '-t', str(seconds), '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', '-y', destination], timeout=90)
        content = destination.read_bytes()
        wave_duration(content)
        with wave.open(io.BytesIO(content), 'rb') as audio:
            samples = array.array('h', audio.readframes(audio.getnframes()))
        if samples and math.sqrt(sum(float(s) * s for s in samples) / len(samples)) < 32:
            raise ValueError('목소리가 너무 작거나 무음입니다. 다시 녹음해 주세요.')
        return content

    async def enroll(self, name, source, start, seconds, ffmpeg):
        name_value(name)
        await self.engine.require_ready()
        with tempfile.TemporaryDirectory(prefix='cosmoss-voice-enroll-') as temporary:
            clip = Path(temporary) / 'sample.wav'
            content = await self.clip(source, clip, start, seconds, ffmpeg)
            embeddings = await self.engine.embeddings([clip])
            return self.store(name, embeddings[0], content)

    @staticmethod
    def ranges(segments):
        selected = {}
        for speaker in dict.fromkeys(s['speaker'] for s in segments):
            candidates = sorted([s for s in segments if s['speaker'] == speaker and s['end'] - s['start'] >= 1.2],
                                key=lambda s: s['end'] - s['start'], reverse=True)
            ranges = []
            for segment in candidates:
                seconds = min(5., segment['end'] - segment['start'])
                start = max(0., segment['start'] + (segment['end'] - segment['start'] - seconds) / 2)
                if any(other['speaker'] != speaker and other['start'] < start + seconds - .15 and other['end'] > start + .15 for other in segments):
                    continue
                ranges.append((start, seconds))
                if len(ranges) == 3: break
            selected[speaker] = ranges
        return selected

    def compare(self, speaker_vectors, profiles=None):
        profiles = self.documents() if profiles is None else profiles
        settings = self.settings()
        matches, names = [], {}
        for speaker, vector in speaker_vectors.items():
            scores = {}
            for profile in profiles:
                score = max(-1., min(1., sum(a * b for a, b in zip(vector, profile['embedding']))))
                if profile['name'] not in scores or score > scores[profile['name']]['similarity']:
                    scores[profile['name']] = {'name': profile['name'], 'profile_id': profile['id'], 'similarity': score}
            ranked = sorted(scores.values(), key=lambda item: item['similarity'], reverse=True)
            if not ranked: continue
            best = ranked[0]
            runner_up = ranked[1]['similarity'] if len(ranked) > 1 else -1.
            accepted = best['similarity'] >= settings['threshold'] and best['similarity'] - runner_up >= settings['margin']
            matches.append({'speaker': speaker, **best, 'similarity': round(best['similarity'], 4),
                            'runner_up_similarity': round(runner_up, 4), 'accepted': accepted})
            if accepted: names[speaker] = best['name']
        return {'speaker_names': names, 'matches': matches, 'model_id': MODEL_ID}

    async def identify(self, source, segments, ffmpeg):
        profiles = self.documents()
        if not profiles:
            return {'speaker_names': {}, 'matches': [], 'model_id': MODEL_ID}
        await self.engine.require_ready()
        selected = self.ranges(segments)
        with tempfile.TemporaryDirectory(prefix='cosmoss-voice-match-') as temporary:
            files, speakers = [], []
            for speaker, ranges in selected.items():
                for start, seconds in ranges:
                    target = Path(temporary) / f'{len(files)}.wav'
                    try: await self.clip(source, target, start, seconds, ffmpeg)
                    except ValueError: continue
                    files.append(target)
                    speakers.append(speaker)
            if not files:
                return {'speaker_names': {}, 'matches': [], 'model_id': MODEL_ID}
            vectors = await self.engine.embeddings(files)
            collected = {}
            for speaker, vector in zip(speakers, vectors):
                collected.setdefault(speaker, []).append(vector)
            averaged = {speaker: unit_vector([sum(axis) / len(group) for axis in zip(*group)]) for speaker, group in collected.items()}
            return self.compare(averaged, profiles)
