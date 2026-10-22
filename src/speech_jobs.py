"""SSH leases for finite speech jobs, with no long-lived model server."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import shlex
import sqlite3
import time

from src.speech_pipeline import FAST_PIPELINES, RUNTIME_VERSION
from src.speech_engines import ENGINE_PIPELINES, runtime_for
from src.ssh_devices import discover_hosts, ssh_arguments


class SpeechJobs:
    def __init__(self, database):
        self.states, self.tasks = {}, {}
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS speech_samples (id INTEGER PRIMARY KEY, sample TEXT NOT NULL)')

    def observations(self):
        with sqlite3.connect(self.database) as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT sample FROM speech_samples ORDER BY id')]

    def record(self, result):
        # Only numeric/runtime identity metadata; never persist recordings,
        # transcripts, filenames, SSH hosts, note IDs or GPU UUIDs here.
        if result.get('runtime') != runtime_for(result.get('pipeline')) or not result.get('segments') or not result.get('peak_process_mib'):
            return
        positive = ('audio_seconds', 'inference_seconds', 'capacity_gib', 'peak_process_mib')
        if (result.get('pipeline') not in FAST_PIPELINES or not isinstance(result.get('batch'), int)
                or not 1 <= result['batch'] <= 24 or not isinstance(result.get('gpu_name'), str)
                or not all(type(result.get(key)) in (int, float) and math.isfinite(result[key]) and result[key] > 0 for key in positive)
                or type(result.get('load_seconds')) not in (int, float) or not math.isfinite(result['load_seconds']) or result['load_seconds'] < 0):
            return
        sample = {key: result[key] for key in ('pipeline', 'runtime', 'batch', 'audio_seconds', 'inference_seconds',
                                               'load_seconds', 'capacity_gib')}
        sample['gpu_name'] = ' '.join(result['gpu_name'].lower().removeprefix('nvidia ').split())
        sample['peak_process_gib'] = result['peak_process_mib'] / 1024
        with sqlite3.connect(self.database) as connection:
            connection.execute('INSERT INTO speech_samples(sample) VALUES(?)', (json.dumps(sample),))
            connection.execute('DELETE FROM speech_samples WHERE id NOT IN (SELECT id FROM speech_samples ORDER BY id DESC LIMIT 200)')

    def status(self, host, pipeline):
        return self.states.get((host, pipeline), {'phase': 'idle'})

    def install(self, host, pipeline):
        if host not in discover_hosts() or pipeline not in FAST_PIPELINES:
            raise ValueError('등록된 SSH 서버와 전사 방식을 선택해 주세요.')
        key = host, pipeline
        if key not in self.tasks or self.tasks[key].done():
            self.states[key] = {'phase': 'connecting'}
            self.tasks[key] = asyncio.create_task(self._install(host, pipeline))
        return self.status(host, pipeline)

    async def _install(self, host, pipeline):
        try:
            await self.execute(host, pipeline, 'install', lambda event: self.states[(host, pipeline)].update(event))
        except asyncio.CancelledError:
            self.states[(host, pipeline)] = {'phase': 'cancelled'}
            raise
        except Exception as error:
            self.states[(host, pipeline)] = {'phase': 'error', 'error': str(error)}

    async def execute(self, host, pipeline, action, on_event, *, audio=None, **options):
        if host not in discover_hosts() or pipeline not in FAST_PIPELINES or action not in {'inspect', 'install', 'run'}:
            raise ValueError('등록된 SSH 서버와 전사 방식을 선택해 주세요.')
        size = audio.stat().st_size if audio else 0
        if size > 600 * 1024**2:
            raise ValueError('정규화된 음성이 600 MiB를 초과합니다. 녹음을 나누어 올려 주세요.')
        source = Path(__file__).parent
        request = {'model_key': pipeline, 'action': action, **options, 'audio_bytes': size,
                   **{key: (source / name).read_text() for key, name in
                      [('worker_source', 'speech_worker.py'), ('catalog_source', 'speech_pipeline.py'),
                       ('engine_source', 'speech_engines.py'), ('engine_worker_source', 'speech_engine_worker.py'),
                       ('lifecycle_source', 'process_lifecycle.py'), ('runtime_source', 'runtime_environment.py')]}}
        bootstrap = (source / ('speech_engine_bootstrap.py' if pipeline in ENGINE_PIPELINES else 'speech_bootstrap.py')).read_text()
        process = await asyncio.create_subprocess_exec(*ssh_arguments(host), '-o', 'ClearAllForwardings=yes',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3', '--', host,
            'python3 -u -c ' + shlex.quote(bootstrap), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=32 * 1024**2)
        last, remote_error, detail = {}, '', ''
        async def send():
            process.stdin.write(json.dumps(request).encode() + b'\n')
            await process.stdin.drain()
            if audio:
                on_event({'phase': 'transferring'})
                with audio.open('rb') as stream:
                    while block := stream.read(256 * 1024):
                        process.stdin.write(block)
                        await process.stdin.drain()
            while True:
                process.stdin.write(b'PING\n')
                await process.stdin.drain()
                await asyncio.sleep(5)
        sender = asyncio.create_task(send())
        try:
            async with asyncio.timeout(7200):
                async for raw in process.stdout:
                    line = raw.decode(errors='replace').strip()
                    if line.startswith('COSMOSS_SPEECH:'):
                        event = json.loads(line.split(':', 1)[1])
                        if event.get('phase') == 'error' and not remote_error:
                            remote_error = event.get('error', '')
                        last = event
                        on_event(event)
                    elif line:
                        detail = line[-1200:]
                await process.wait()
            if process.returncode or last.get('phase') not in {'installed', 'done', 'idle'}:
                raise RuntimeError(remote_error or last.get('error') or detail or '원격 전사 결과를 받지 못했습니다.')
            if action == 'run' and (last.get('phase') != 'done' or not isinstance(last.get('result'), dict)):
                raise RuntimeError('원격 전사 결과가 불완전합니다.')
            return last
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if process.returncode is None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.communicate(), timeout=12)
                except TimeoutError:
                    process.kill()
                    await process.communicate()

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
