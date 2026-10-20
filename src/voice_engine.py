"""Explicit setup and bounded subprocesses for local speaker verification."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
from pathlib import Path

import httpx

from src.runtime_environment import find_uv

MODEL_ID = 'campplus-zh-en-advanced-192-v1'
MODEL_URL = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx'
MODEL_SHA256 = 'aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2'
MODEL_SIZE = 28281164
RUNTIME_VERSION = 'sherpa-onnx-1.13.8-numpy-2.2.6-v1'


class VoiceEngine:
    def __init__(self, root: Path):
        self.root = root / '.cache/voice-engine'
        self.runtime = self.root / 'runtime'
        self.python = self.runtime / 'bin/python'
        self.model = self.root / 'campplus.onnx'
        self.stamp = self.root / 'ready.json'
        self.task = None
        self.state = {'phase': 'idle'}
        self.lock = asyncio.Lock()
        self.processes = set()

    def ready(self):
        try:
            metadata = json.loads(self.stamp.read_text())
            return (metadata == {'runtime': RUNTIME_VERSION, 'model_sha256': MODEL_SHA256}
                    and self.python.is_file() and self.model.stat().st_size == MODEL_SIZE)
        except (OSError, ValueError):
            return False

    def status(self):
        busy = self.task is not None and not self.task.done()
        return {**self.state, 'ready': self.ready(), 'busy': busy, 'model_id': MODEL_ID,
                'phase': self.state['phase'] if busy or self.state.get('error') else 'ready' if self.ready() else 'idle'}

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.prepare())
        return self.status()

    async def command(self, arguments, *, payload=None, timeout=180):
        process = await asyncio.create_subprocess_exec(*map(str, arguments), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.processes.add(process)
        try:
            output, error = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
            if process.returncode:
                raise RuntimeError(error.decode(errors='replace')[-1200:] or '목소리 처리를 완료하지 못했습니다.')
            return output
        finally:
            if process.returncode is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: await asyncio.wait_for(process.communicate(), timeout=3)
                except asyncio.TimeoutError:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    await process.communicate()
            self.processes.discard(process)

    async def prepare(self):
        try:
            self.state = {'phase': 'checking', 'message': '목소리 비교 도구를 확인하고 있습니다…'}
            if self.ready():
                try:
                    await self.command([self.python, '-I', '-B', '-c',
                        "import importlib.metadata as m; assert m.version('sherpa-onnx')=='1.13.8'; assert m.version('numpy')=='2.2.6'; import sherpa_onnx,numpy"], timeout=30)
                    if hashlib.sha256(self.model.read_bytes()).hexdigest() == MODEL_SHA256:
                        self.state = {'phase': 'ready'}
                        return
                except (RuntimeError, OSError, asyncio.TimeoutError):
                    pass
            self.stamp.unlink(missing_ok=True)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            uv = find_uv(self.root / 'tools')
            if not uv['ready']:
                raise RuntimeError('앱 실행 환경의 uv를 찾을 수 없습니다. 앱 설정에서 준비해 주세요.')
            self.state = {'phase': 'installing', 'message': '목소리 비교 도구를 준비하고 있습니다…'}
            if not self.python.is_file():
                await self.command([uv['path'], 'venv', '--clear', '--python', sys.executable, self.runtime], timeout=120)
            await self.command([uv['path'], 'pip', 'install', '--reinstall', '--python', self.python,
                                'sherpa-onnx==1.13.8', 'numpy==2.2.6'], timeout=600)
            await self.command([self.python, '-I', '-B', '-c', 'import sherpa_onnx,numpy'], timeout=30)
            valid = self.model.is_file() and hashlib.sha256(self.model.read_bytes()).hexdigest() == MODEL_SHA256
            if not valid:
                self.state = {'phase': 'downloading', 'message': '목소리 비교 모델을 내려받고 있습니다…', 'percent': 0}
                temporary = self.root / 'campplus.partial'
                try:
                    digest, size = hashlib.sha256(), 0
                    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                        async with client.stream('GET', MODEL_URL) as response:
                            response.raise_for_status()
                            with temporary.open('wb') as target:
                                async for block in response.aiter_bytes(256 * 1024):
                                    size += len(block)
                                    if size > MODEL_SIZE: raise ValueError('모델 파일 크기를 확인하지 못했습니다.')
                                    target.write(block)
                                    digest.update(block)
                                    self.state['percent'] = round(size / MODEL_SIZE * 100)
                    if size != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
                        raise ValueError('목소리 모델 다운로드를 검증하지 못했습니다. 다시 준비해 주세요.')
                    temporary.replace(self.model)
                finally:
                    temporary.unlink(missing_ok=True)
            self.stamp.write_text(json.dumps({'runtime': RUNTIME_VERSION, 'model_sha256': MODEL_SHA256}))
            self.state = {'phase': 'ready'}
        except asyncio.CancelledError:
            self.state = {'phase': 'idle'}
            raise
        except Exception as error:
            self.state = {'phase': 'error', 'error': str(error)}

    async def require_ready(self):
        if self.task is not None and not self.task.done():
            await asyncio.shield(self.task)
        if not self.ready():
            raise RuntimeError(self.state.get('error', '목소리 프로필 창에서 비교 도구를 먼저 준비해 주세요.'))

    async def embeddings(self, files: list[Path]):
        await self.require_ready()
        async with self.lock:
            result = await self.command([self.python, '-I', '-B', Path(__file__).with_name('voice_embedding_worker.py')],
                payload=json.dumps({'model': str(self.model), 'files': list(map(str, files))}).encode(), timeout=180)
        vectors = json.loads(result)['embeddings']
        if not isinstance(vectors, list) or len(vectors) != len(files):
            raise ValueError('목소리 특징 결과 개수가 올바르지 않습니다.')
        # Validate the process boundary before these vectors reach matching.
        from src.voice_profiles import unit_vector
        return [unit_vector(vector) for vector in vectors]

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
