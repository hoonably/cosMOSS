"""SSH model setup, finite GPU jobs, and reviewable speaker-only proposals."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import shlex
import tempfile
import time
import uuid

from src.ssh_devices import discover_hosts, ssh_arguments
from src.local_diarization import LocalDiarization

DEFAULT_MODEL_KEY = 'nemotron-3'

MODELS = [
    {'key': 'nemotron-3', 'name': 'Nemotron 3 Diarization · 0.1B', 'supports_speaker_count': False,
     'description': '원본 FP32 · 오프라인 분석 · 최대 8명'},
    {'key': 'pyannote-community-1', 'name': 'pyannote Community-1 — 무료', 'supports_speaker_count': True,
     'description': '원본 정밀도 · 화자 인원수 지정 가능'},
]


def fingerprint(note):
    return hashlib.sha256(json.dumps({'segments': note['segments'], 'speaker_names': note['speaker_names'],
                                     'speaker_merges': note.get('speaker_merges', {})}, sort_keys=True).encode()).hexdigest()


def proposals(note, turns):
    """Assign whole ASR utterances by overlapping speech time; never invent words."""
    cleaned = []
    for turn in turns:
        start, end = float(turn['start']), float(turn['end'])
        if not all(map(math.isfinite, (start, end))) or start < 0 or end <= start:
            raise ValueError('화자 구간의 시간이 올바르지 않습니다.')
        cleaned.append({'start': start, 'end': end, 'speaker': str(turn['speaker'])})
    weights, overlaps = {}, []
    for segment in note['segments']:
        scores = {}
        for turn in cleaned:
            length = max(0, min(segment['end'], turn['end']) - max(segment['start'], turn['start']))
            if length:
                speaker = turn['speaker']
                scores[speaker] = scores.get(speaker, 0) + length
                pair = speaker, segment['speaker']
                weights[pair] = weights.get(pair, 0) + length
        overlaps.append(scores)
    mapping, used = {}, set()
    # Preserve established labels where they correspond; each new voice receives
    # a distinct identity even when MOSS merged it with an existing voice.
    for (speaker, previous), _ in sorted(weights.items(), key=lambda item: -item[1]):
        if speaker not in mapping and previous not in used and previous != '미확인':
            mapping[speaker] = previous
            used.add(previous)
    known = {segment['speaker'] for segment in note['segments'] + note.get('raw_segments', [])} | set(note['speaker_names'])
    for speaker in sorted({turn['speaker'] for turn in cleaned}):
        if speaker not in mapping:
            index = 1
            while f'S{index:02d}' in known | used: index += 1
            mapping[speaker] = f'S{index:02d}'
            used.add(mapping[speaker])
    rows = []
    for segment, scores in zip(note['segments'], overlaps):
        if not scores:
            continue
        label = max(scores, key=scores.get)
        share = scores[label] / sum(scores.values())
        rows.append({'segment_id': segment['id'], 'before': segment['speaker'], 'after': mapping[label],
                     'start': segment['start'], 'end': segment['end'], 'text': segment['text'],
                     'speech_share': round(share, 4), 'mixed': share < .85,
                     'changed': segment['speaker'] != mapping[label]})
    voices = []
    for speaker, mapped in mapping.items():
        own = [turn for turn in cleaned if turn['speaker'] == speaker]
        samples = []
        candidates = []
        for turn in own:
            length = min(6, turn['end'] - turn['start'])
            start = turn['start'] + max(0, (turn['end'] - turn['start'] - length) / 2)
            overlap = sum(max(0, min(start + length, other['end']) - max(start, other['start']))
                          for other in cleaned if other['speaker'] != speaker)
            candidates.append((min(1, overlap / length), -length, start))
        for overlap, negative_length, start in sorted(candidates):
            if any(abs(start - sample['start']) < 15 for sample in samples): continue
            samples.append({'start': start, 'end': start - negative_length, 'overlap': overlap > .05})
            if len(samples) == 3: break
        voices.append({'model_speaker': speaker, 'speaker': mapped,
                       'seconds': sum(turn['end'] - turn['start'] for turn in own), 'samples': samples})
    return {'rows': rows, 'voices': voices, 'speaker_mapping': mapping, 'speaker_count': len(mapping),
            'unmatched_segments': len(note['segments']) - len(rows)}


class Diarization:
    def __init__(self, data: Path, runtime: Path | None = None):
        self.directory = data / 'diarization'
        self.tasks, self.states = {}, {}
        self.local = LocalDiarization(runtime or data)

    def status(self, identity):
        if identity in self.states:
            return self.states[identity]
        if identity.startswith('note:'):
            path = self.directory / (identity.split(':', 1)[1] + '.json')
            try: return json.loads(path.read_text())
            except (OSError, ValueError): pass
        return {'phase': 'idle'}

    def busy(self, identity):
        task = self.tasks.get(identity)
        return bool(task and not task.done())

    async def execute(self, host, model_key, action, on_event, *, audio=None, **options):
        if host == '@local':
            return await self.local.execute(model_key, action, on_event, audio=audio, **options)
        if host not in discover_hosts() or model_key not in {model['key'] for model in MODELS}:
            raise ValueError('등록된 SSH 서버와 화자 분리 모델을 선택해 주세요.')
        source = Path(__file__).parent
        request = {'model_key': model_key, 'action': action, **options,
                   'worker_source': (source / 'diarization_worker.py').read_text(),
                   'lifecycle_source': (source / 'process_lifecycle.py').read_text(),
                   'audio_bytes': audio.stat().st_size if audio else 0}
        bootstrap = (source / 'diarization_bootstrap.py').read_text()
        process = await asyncio.create_subprocess_exec(*ssh_arguments(host), '-o', 'ClearAllForwardings=yes',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3', '--', host,
            'python3 -u -c ' + shlex.quote(bootstrap), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=32 * 1024**2)
        heartbeat = None
        last, detail, remote_error = {}, '', ''
        async def send():
            process.stdin.write(json.dumps(request).encode() + b'\n')
            await process.stdin.drain()
            if audio:
                with audio.open('rb') as stream:
                    while block := stream.read(256 * 1024):
                        process.stdin.write(block)
                        await process.stdin.drain()
            while True:
                process.stdin.write(b'PING\n')
                await process.stdin.drain()
                await asyncio.sleep(5)
        try:
            heartbeat = asyncio.create_task(send())
            async with asyncio.timeout(2400):
                async for raw in process.stdout:
                    line = raw.decode(errors='replace').strip()
                    if 'COSMOSS_DIARIZATION:' in line:
                        event = json.loads(line.split('COSMOSS_DIARIZATION:', 1)[1])
                        if event.get('phase') == 'error' and not remote_error:
                            remote_error = event.get('error', '')
                        last = event
                        on_event(event)
                    elif line:
                        detail = line[-1000:]
                await process.wait()
            if process.returncode:
                raise RuntimeError(remote_error or last.get('error') or detail or '원격 화자 분리에 실패했습니다.')
            if last.get('phase') not in {'installed', 'done', 'idle', 'auth_required'}:
                raise RuntimeError('화자 분리 결과를 받지 못했습니다.')
            return last
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if process.returncode is None:
                process.stdin.close()
                try: await asyncio.wait_for(process.communicate(), timeout=12)
                except TimeoutError:
                    process.kill()
                    await process.communicate()

    def install(self, host, model_key):
        identity = 'setup:' + host + ':' + model_key
        if not self.busy(identity):
            self.states[identity] = {'phase': 'connecting'}
            self.tasks[identity] = asyncio.create_task(self._install(identity, host, model_key))
        return self.status(identity)

    async def _install(self, identity, host, model_key):
        try:
            await self.execute(host, model_key, 'install', lambda event: self.states[identity].update(event))
        except asyncio.CancelledError:
            self.states[identity] = {'phase': 'cancelled'}
            raise
        except Exception as error:
            self.states[identity] = {'phase': 'error', 'error': str(error)}

    def start(self, note, source, ffmpeg, host, model_key, gpu_uuid, num_speakers=None):
        identity = 'note:' + note['id']
        if self.busy(identity): raise ValueError('이 노트의 화자 분리가 이미 실행 중입니다.')
        self.states[identity] = {'phase': 'preparing', 'model_key': model_key, 'host': host, 'num_speakers': num_speakers}
        self.tasks[identity] = asyncio.create_task(self._run(identity, note, source, ffmpeg, host, model_key, gpu_uuid, num_speakers))
        return self.status(identity)

    async def _run(self, identity, note, source, ffmpeg, host, model_key, gpu_uuid, num_speakers):
        process = None
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix='cosmoss-diarization-') as temporary:
                audio = Path(temporary) / 'audio.wav'
                process = await asyncio.create_subprocess_exec(ffmpeg, '-nostdin', '-v', 'error', '-i', str(source),
                    '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(audio),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, error = await asyncio.wait_for(process.communicate(), timeout=180)
                if process.returncode: raise RuntimeError(error.decode(errors='replace')[-1000:])
                last = await self.execute(host, model_key, 'run', lambda event: self.states[identity].update(event),
                                          audio=audio, gpu_uuid=gpu_uuid, num_speakers=num_speakers)
            result = last['result']
            result['wall_seconds'] = time.monotonic() - started
            result.update(proposals(note, result['turns']))
            result['proposal_id'] = uuid.uuid4().hex
            result['source_fingerprint'] = fingerprint(note)
            state = {**self.states[identity], 'phase': 'done', 'result': result}
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.directory / (note['id'] + '.json')
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False))
            temporary.replace(target)
            self.states[identity] = state
        except asyncio.CancelledError:
            self.states[identity] = {'phase': 'cancelled'}
            raise
        except Exception as error:
            self.states[identity] = {'phase': 'error', 'error': str(error)}
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self):
        for task in self.tasks.values():
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def approved(self, note, proposal_id, segment_ids):
        result = self.status('note:' + note['id']).get('result') or {}
        if result.get('proposal_id') != proposal_id or result.get('source_fingerprint') != fingerprint(note):
            raise ValueError('분석 후 노트가 변경되었습니다. 다시 분석해 주세요.')
        rows = {row['segment_id']: row for row in result['rows'] if row['changed']}
        if not segment_ids or len(set(segment_ids)) != len(segment_ids) or set(segment_ids) - rows.keys():
            raise ValueError('승인할 발화를 선택해 주세요.')
        assignments = {identity: rows[identity]['after'] for identity in segment_ids}
        segments = [{**segment, 'speaker': assignments.get(segment['id'], segment['speaker']),
                     **({'speaker_uncertain': rows[segment['id']]['mixed']} if segment['id'] in assignments else {})}
                    for segment in note['segments']]
        names = dict(note['speaker_names'])
        for segment in segments: names.setdefault(segment['speaker'], segment['speaker'])
        return {'segments': segments, 'speaker_names': names, 'voice_matches': [], 'voice_merge_candidates': [], 'voice_match_error': None}
