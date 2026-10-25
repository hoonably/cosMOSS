"""Read CLI model catalogs without starting a prompt; persist per-user choices."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import time

from src.summaries import executable, cli_environment


def valid_identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/()\[\]-]{0,199}', value))


def normalize_models(provider, rows):
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('hidden'):
            continue
        if provider == 'codex':
            if 'text' not in row.get('inputModalities', ['text']):
                continue
            identity = row.get('model') or row.get('id')
            efforts = [item.get('reasoningEffort') for item in row.get('supportedReasoningEfforts', []) if isinstance(item, dict)]
            default = row.get('defaultReasoningEffort')
        else:
            identity = row.get('resolvedModel') or row.get('value')
            efforts = row.get('supportedEffortLevels', []) if row.get('supportsEffort') is not False else []
            default = row.get('defaultEffortLevel')
        if not valid_identifier(identity) or identity == 'default' or identity in seen:
            continue
        efforts = list(dict.fromkeys(value for value in (efforts or []) if valid_identifier(value)))
        name = row.get('displayName') or identity
        if provider == 'claude' and row.get('value') == 'default':
            name = next((item.get('displayName') for item in rows if item.get('value') != 'default' and (item.get('resolvedModel') or item.get('value')) == identity), identity)
        models.append(dict(id=identity, name=name,
                           efforts=efforts, default_effort=default if default in efforts else '',
                           recommended=bool(row.get('isDefault') or provider == 'claude' and row.get('value') == 'default')))
        seen.add(identity)
    if not models:
        raise ValueError('CLI가 모델 목록을 반환하지 않았습니다. CLI를 업데이트한 뒤 다시 새로고침해 주세요.')
    return models


async def discover_models(provider, binary):
    """Only initialize + list metadata: no user transcript, turn or tool execution."""
    if provider == 'codex':
        args = [binary, 'app-server', '--listen', 'stdio://']
    else:
        args = [binary, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
                '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--setting-sources', '', '--disable-slash-commands', '--no-session-persistence']
    with tempfile.TemporaryDirectory(prefix='cosmoss-model-list-') as directory:
        process = await asyncio.create_subprocess_exec(*args, cwd=directory, env=cli_environment(binary),
            start_new_session=True, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024**2)
        async def send(value):
            process.stdin.write((json.dumps(value) + '\n').encode())
            await process.stdin.drain()
        async def receive():
            while line := await process.stdout.readline():
                if line.lstrip().startswith(b'{'):
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
            raise ValueError('모델 목록을 읽지 못했습니다. CLI 로그인·버전을 확인한 뒤 다시 시도해 주세요.')
        async def rpc(identity, method, params):
            await send(dict(id=identity, method=method, params=params))
            while True:
                value = await receive()
                if value.get('id') == identity:
                    if 'error' in value:
                        raise ValueError('CLI가 모델 조회를 지원하지 않습니다. CLI를 업데이트해 주세요.')
                    return value['result']
        try:
            async with asyncio.timeout(20):
                if provider == 'codex':
                    await rpc(0, 'initialize', {'clientInfo': {'name':'cosmoss', 'version':'1.0.0'}})
                    await send({'method':'initialized', 'params':{}})
                    rows, cursor = [], None
                    for page in range(20):
                        response = await rpc(page + 1, 'model/list', {'limit':100, 'includeHidden':False, 'cursor':cursor})
                        rows.extend(response['data'])
                        cursor = response.get('nextCursor')
                        if not cursor:
                            break
                    else:
                        raise ValueError('모델 목록이 너무 큽니다. CLI 설정을 확인해 주세요.')
                else:
                    await send({'type':'control_request', 'request_id':'cosmoss-model-list',
                                'request':{'subtype':'initialize', 'hooks':None}})
                    while True:
                        value = await receive()
                        response = value.get('response', {})
                        if value.get('type') == 'control_response' and response.get('request_id') == 'cosmoss-model-list':
                            if response.get('subtype') == 'error':
                                raise ValueError('Claude 모델 목록을 읽지 못했습니다. Claude CLI를 업데이트해 주세요.')
                            rows = response.get('response', {}).get('models', [])
                            break
                return normalize_models(provider, rows)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()


class SummaryModels:
    def __init__(self, data):
        self.preferences_path = Path(data) / 'summary-model-preferences.json'
        self.cache_path = Path(data) / 'summary-model-catalog.json'
        self.lock = threading.RLock()
        self.locks = {provider: asyncio.Lock() for provider in ('codex', 'claude')}

    def _read(self, path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.summary-models-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(json.dumps(value, ensure_ascii=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def default_effort(model):
        values = model['efforts']
        return 'medium' if 'medium' in values else model.get('default_effort') if model.get('default_effort') in values else next(iter(values), '')

    def _view(self, provider, catalog, *, reconcile=True):
        with self.lock:
            preferences = self._read(self.preferences_path)
            saved = preferences.get(provider, {})
            if not isinstance(saved, dict):
                saved = {}
            models = catalog.get('models', [])
            chosen = next((model for model in models if model['id'] == saved.get('model')), None)
            notice = ''
            if not chosen and models:
                chosen = next((model for model in models if model['recommended']), models[0])
                if saved and reconcile:
                    preferences.pop(provider, None)
                    self._write(self.preferences_path, preferences)
                    saved = {}
                    notice = '선택했던 모델이 목록에서 사라져 기본 모델로 변경했습니다.'
            effort = saved.get('effort', '')
            if chosen and effort not in chosen['efforts']:
                effort = self.default_effort(chosen)
                if saved.get('model') == chosen['id'] and reconcile and saved.get('effort') != effort:
                    preferences[provider] = {'model':chosen['id'], 'effort':effort}
                    self._write(self.preferences_path, preferences)
                    notice = '이 모델이 지원하는 추론 강도로 변경했습니다.'
            elif chosen and not saved:
                effort = self.default_effort(chosen)
            return {**{key:catalog[key] for key in ('models', 'checked_at', 'stale', 'error') if key in catalog},
                    'provider':provider, 'selection':{'model':chosen['id'] if chosen else '', 'effort':effort}, 'notice':notice}

    async def get(self, provider, *, refresh=False):
        if provider not in self.locks:
            raise ValueError('지원하지 않는 AI입니다.')
        async with self.locks[provider]:
            with self.lock:
                cache = self._read(self.cache_path)
                catalog = cache.get(provider, {})
                if not isinstance(catalog, dict):
                    catalog = {}
            binary = executable(provider)
            if not binary:
                return self._view(provider, {**catalog, 'error':'CLI를 설치한 뒤 새로고침해 주세요.', 'stale':True}, reconcile=False)
            try:
                info = Path(binary).stat()
            except OSError:
                return self._view(provider, {**catalog, 'error':'CLI 파일을 읽지 못했습니다. 설치 상태를 확인해 주세요.', 'stale':True}, reconcile=False)
            signature = [str(Path(binary).resolve()), info.st_mtime_ns, info.st_size]
            if not refresh and catalog.get('signature') == signature and time.time() - catalog.get('timestamp', 0) < 3600:
                return self._view(provider, catalog)
            try:
                models = await discover_models(provider, binary)
            except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                return self._view(provider, {**catalog, 'error':'모델 목록을 갱신하지 못했습니다. CLI 로그인·버전을 확인한 뒤 다시 시도해 주세요.', 'stale':True}, reconcile=False)
            catalog = {'models':models, 'signature':signature, 'timestamp':time.time(),
                       'checked_at':datetime.now(UTC).isoformat(), 'stale':False, 'error':None}
            with self.lock:
                cache = self._read(self.cache_path)
                cache[provider] = catalog
                self._write(self.cache_path, cache)
            return self._view(provider, catalog)

    async def select(self, provider, model, effort):
        catalog = await self.get(provider)
        chosen = next((item for item in catalog.get('models', []) if item['id'] == model), None)
        if not chosen or effort not in (chosen['efforts'] or ['']):
            raise ValueError('모델 또는 추론 강도가 목록과 다릅니다. 새로고침 후 다시 선택해 주세요.')
        with self.lock:
            preferences = self._read(self.preferences_path)
            preferences[provider] = {'model':model, 'effort':effort}
            self._write(self.preferences_path, preferences)
        return self._view(provider, catalog)

    async def resolve(self, provider, model=None, effort=None):
        catalog = await self.get(provider)
        model = catalog['selection']['model'] if model is None else model
        chosen = next((item for item in catalog.get('models', []) if item['id'] == model), None)
        if not chosen:
            raise ValueError(catalog.get('error') or '사용할 모델을 선택해 주세요.')
        effort = catalog['selection']['effort'] if effort is None and model == catalog['selection']['model'] else self.default_effort(chosen) if effort is None else effort
        if effort not in (chosen['efforts'] or ['']):
            raise ValueError('이 모델이 지원하지 않는 추론 강도입니다. 다시 선택해 주세요.')
        return {'model':model, 'effort':effort, 'model_name':chosen['name']}
