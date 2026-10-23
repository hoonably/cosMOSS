"""A separate Mac Nemotron runtime; never installs a local transcription model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

from src.model_cache import default_cache
from src.runtime_environment import ensure_uv
from src.diarization_worker import MODELS

VERSION = 'nemotron-transformers5202-torch280-librosa0110-v1'


async def finish_cleanup(awaitable):
    """Keep the resource lease until a child has exited, even on repeated cancel."""
    cleanup = asyncio.create_task(awaitable)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class LocalDiarization:
    def __init__(self, root):
        self.root = Path(root) / '.cache/diarization' / VERSION
        self.python = self.root / 'runtime/bin/python'
        self.lock = asyncio.Lock()

    def ready(self):
        repo, revision = MODELS['nemotron-3']
        model = default_cache() / ('models--' + repo.replace('/', '--')) / 'snapshots' / revision
        return self.python.is_file() and (self.root / 'ready').is_file() and all((model / name).is_file() for name in ('config.json','processor_config.json','model.safetensors'))

    async def command(self, args, on_event, timeout=1800):
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'HF_HUB_DISABLE_PROGRESS_BARS':'1',
                       'HF_HUB_DISABLE_TELEMETRY':'1', 'COSMOSS_DIARIZATION_PARENT_PID':str(os.getpid()), 'OMP_NUM_THREADS':'4'}
        environment.pop('COSMOSS_WORKER_PARENT_PID', None)
        process = await asyncio.create_subprocess_exec(*map(str,args), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True, env=environment)
        last, detail = {}, ''
        try:
            async with asyncio.timeout(timeout):
                async for line in process.stdout:
                    text = line.decode(errors='replace').strip()
                    if 'COSMOSS_DIARIZATION:' in text:
                        last = json.loads(text.split('COSMOSS_DIARIZATION:',1)[1])
                        on_event(last)
                    elif text: detail = text[-1000:]
                await process.wait()
            if process.returncode: raise RuntimeError(last.get('error') or detail or 'Mac 화자 분석을 완료하지 못했습니다.')
            return last
        finally:
            if process.returncode is None:
                async def stop():
                    try: os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: await asyncio.wait_for(process.communicate(),5)
                    except TimeoutError:
                        try: os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError: pass
                        await process.communicate()
                await finish_cleanup(stop())

    async def execute(self, model_key, action, on_event, *, audio=None, **options):
        if model_key != 'nemotron-3': raise ValueError('이 Mac의 화자 분리는 Nemotron을 사용합니다.')
        if action == 'inspect': return {'phase':'installed' if self.ready() else 'idle'}
        if action not in {'install', 'run'}: raise ValueError('지원하지 않는 화자 분석 작업입니다.')
        async with self.lock:
            if action == 'install':
                self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
                uv = await asyncio.to_thread(ensure_uv,self.root/'tools')
                if not self.python.is_file():
                    on_event({'phase':'installing'})
                    await self.command([uv,'venv','--python',sys.executable,self.root/'runtime'],on_event)
                if not (self.root/'ready').is_file():
                    on_event({'phase':'installing'})
                    await self.command([uv,'pip','install','--python',self.python,'torch==2.8.0','transformers==5.20.2','librosa==0.11.0','numpy==2.2.6'],on_event)
                result = await self.worker({'action':'install','model_key':model_key},on_event)
                if result.get('phase') != 'installed':
                    return result
                (self.root/'ready').write_text(VERSION)
                return {'phase':'installed'}
            if not self.ready(): raise ValueError('이 Mac에서 모델 준비하기를 먼저 눌러 주세요.')
            return await self.worker({'action':'run','model_key':model_key,'audio':str(audio),'device':'auto',**options},on_event)

    async def worker(self, request, on_event):
        with tempfile.TemporaryDirectory(prefix='cosmoss-mac-diarization-') as temporary:
            root = Path(temporary)
            request['result'] = str(root/'result.json')
            config = root/'request.json';config.write_text(json.dumps(request))
            result = await self.command([self.python,'-u','-B',Path(__file__).with_name('diarization_worker.py'),config],on_event)
            if result.get('phase') in {'auth_required', 'error'}:
                return result
            if request['action'] == 'run':
                result = json.loads((root/'result.json').read_text())
                on_event({'phase':'done','result':result})
                return {'phase':'done','result':result}
            if result.get('phase') != 'installed':
                raise RuntimeError('화자 모델 설치 완료를 확인하지 못했습니다.')
            return result
