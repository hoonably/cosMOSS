"""User-selected Mac models and the MLX process owned by this application."""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import platform
import shutil
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from src.model_cache import default_cache
from src.model_catalog import DEFAULT_OPTIONS, MODELS
from src.local_diarization import finish_cleanup
from src.runtime_environment import ensure_uv, inspect_environment


DEFAULT_MODEL = "vanch007/mlx-MOSS-Transcribe-Diarize-8bit"
RUNTIME_VERSION = "mlx-audio-0.5.7-mlx-lm-0.31.3-v1"
BUSY_PHASES = {"checking", "installing_uv", "installing", "downloading", "loading"}


def validate_model_folder(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    try:
        config = json.loads((path / "config.json").read_text())
        if config.get("model_type") != "moss_transcribe_diarize":
            raise ValueError("Mac용 MOSS MLX 모델 폴더를 선택해 주세요.")
        for name in ("processor_config.json", "preprocessor_config.json", "tokenizer_config.json"):
            json.loads((path / name).read_text())
        if not (path / "tokenizer.json").is_file() and not all((path / name).is_file() for name in ("vocab.json", "merges.txt")):
            raise ValueError("모델의 토크나이저 파일이 없습니다.")
        index = path / "model.safetensors.index.json"
        names = set(json.loads(index.read_text())["weight_map"].values()) if index.is_file() else {"model.safetensors"}
        if not names:
            raise ValueError("모델 가중치가 없습니다.")
        for name in names:
            weight = (path / name).resolve()
            # HF snapshots may symlink to their cache's blobs directory.
            with weight.open("rb") as file:
                header_size = int.from_bytes(file.read(8), "little")
                if not 0 < header_size <= 32 * 1024 * 1024:
                    raise ValueError("모델 가중치 파일이 올바르지 않습니다.")
                header = json.loads(file.read(header_size))
                offsets = [tensor["data_offsets"] for key, tensor in header.items() if key != "__metadata__"]
                if not offsets or any(len(pair) != 2 or not all(isinstance(n, int) for n in pair) or not 0 <= pair[0] <= pair[1] for pair in offsets):
                    raise ValueError("모델 가중치 정보가 올바르지 않습니다.")
                if weight.stat().st_size < 8 + header_size + max(pair[1] for pair in offsets):
                    raise ValueError("모델 다운로드가 완전하지 않습니다.")
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError("모델 폴더에 필요한 설정·가중치 파일이 없습니다. config.json과 safetensors가 있는 Mac용 MOSS 모델 폴더를 선택해 주세요.") from error
    return path


def find_cached_model(cache: Path, model_id: str) -> Path | None:
    repository = cache / ("models--" + model_id.replace("/", "--"))
    snapshots = repository / "snapshots"
    candidates = []
    reference = repository / "refs/main"
    if reference.is_file():
        revision = reference.read_text().strip()
        if revision and "/" not in revision and revision not in {".", ".."}:
            candidates.append(snapshots / revision)
    if snapshots.is_dir():
        candidates.extend(sorted(snapshots.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True))
    for candidate in candidates:
        try:
            return validate_model_folder(candidate)
        except ValueError:
            continue
    return None


class LocalModels:
    def __init__(self, root: Path, data: Path, port: int, model_name: str, *, diarization=None):
        self.root, self.data, self.port, self.model_name = root, data, port, model_name
        self.settings_file = data / "local-model.json"
        self.runtime = Path(os.getenv("MLX_VENV") or root / ".venv-mlx").expanduser().resolve()
        self.model_id = os.getenv("MLX_MODEL_ID", DEFAULT_MODEL)
        self.cache = default_cache().resolve()
        self.task: asyncio.Task | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.diarization = diarization
        self.lock = diarization.lock if diarization is not None else asyncio.Lock()
        self.state: dict[str, Any] = {"phase": "idle"}
        try:
            self.saved = json.loads(self.settings_file.read_text())
            if not isinstance(self.saved, dict):
                self.saved = {}
        except (OSError, ValueError):
            self.saved = {}
        self.model_id = self.saved.get("model_id") or self.model_id
        self.model_key = self.saved.get("model_key") or next((model["key"] for model in MODELS if model["backend"] == "mlx" and model["repo"] == self.model_id), "custom")
        self.options = {**DEFAULT_OPTIONS, "kv_bits": int(os.getenv("MLX_KV_BITS", "8")),
                        "prefill_step_size": int(os.getenv("MLX_PREFILL_STEP_SIZE", "512")), **self.saved.get("options", {})}
        self.log_path = root / "logs/local-model.log"

    def status(self) -> dict[str, Any]:
        readiness = self.readiness_for(self.current_selection())
        if self.state['phase'] in {'installed', 'ready'} and not readiness['ready']:
            self.state['phase'] = 'idle'
        suggested = self.saved.get("model_path") or os.getenv("MLX_MODEL_PATH") or str(self.root / "models/moss-mlx-8bit")
        return {"model_id": self.model_id, "model_key": self.model_key, "options": self.options, "cache_dir": str(self.cache), "source": self.saved.get("source", "cache"),
                "model_path": self.saved.get("model_path", ""),
                "suggested_path": suggested if Path(suggested).expanduser().is_dir() else "",
                **self.state, **readiness}

    def current_selection(self):
        return {'model_id': self.model_id, 'model_key': self.model_key, 'options': self.options,
                'model_source': self.saved.get('source', 'cache'), 'model_path': self.saved.get('model_path', '')}

    def model_path_for(self, device):
        """Resolve an immutable selection from local files; never download here."""
        known = next((model for model in MODELS if model['key'] == device.get('model_key') and model['backend'] == 'mlx'), None)
        model_id = device.get('model_id') or (known['repo'] if known else self.model_id)
        source = device.get('model_source', device.get('source', 'cache'))
        path = device.get('model_path')
        if path or source == 'folder':
            if not path: return None
            try:
                resolved = validate_model_folder(path)
            except ValueError:
                return None
        else:
            resolved = find_cached_model(self.cache, model_id)
        if resolved and known:
            try:
                quantization = json.loads((resolved / 'config.json').read_text()).get('quantization') or {}
                expected = {'mlx-4bit': 4, 'mlx-8bit': 8, 'mlx-16bit': 16}[known['key']]
                if quantization.get('bits', 16) != expected:
                    return None
            except (OSError, ValueError, AttributeError):
                return None
        return resolved

    def readiness_for(self, device):
        stamp = self.runtime / 'cosmoss-runtime-version'
        try:
            runtime_ready = (self.runtime / 'bin/python').is_file() and stamp.read_text().strip() == RUNTIME_VERSION
        except OSError:
            runtime_ready = False
        asr_ready = runtime_ready and self.model_path_for(device) is not None
        diarization_ready = self.diarization is None or self.diarization.ready()
        return {'asr_ready': asr_ready, 'diarization_ready': diarization_ready,
                'ready': asr_ready and diarization_ready}

    async def select(self, selected, options):
        """Remember an explicit choice without installing or loading weights."""
        if self.task and not self.task.done():
            raise ValueError('현재 모델 준비가 끝난 뒤 선택해 주세요.')
        if selected['key'] == self.model_key and selected['repo'] == self.model_id:
            return self.status()
        await self.stop_process()
        self.model_key, self.model_id, self.options = selected['key'], selected['repo'], options
        self.saved = {'source': 'cache', 'model_id': self.model_id, 'model_key': self.model_key, 'options': options}
        self.data.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.saved, ensure_ascii=False))
        temporary.replace(self.settings_file)
        self.state = {'phase': 'installed' if self.readiness_for(self.current_selection())['ready'] else 'idle'}
        return self.status()

    def start(self, source: str, path: str = "", *, model_id=None, model_key=None, options=None, remember=True) -> dict[str, Any]:
        if self.task and not self.task.done():
            return self.status()
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise ValueError("이 Mac에서 모델을 실행하려면 Apple Silicon이 필요합니다.")
        selected = validate_model_folder(path) if source == "folder" and path.strip() else None
        if source == "folder" and selected is None:
            raise ValueError("이미 다운로드한 모델 폴더를 선택해 주세요.")
        if selected and model_key in {"mlx-4bit", "mlx-8bit", "mlx-16bit"}:
            quantization = json.loads((selected / "config.json").read_text()).get("quantization") or {}
            expected = {"mlx-4bit": 4, "mlx-8bit": 8, "mlx-16bit": 16}[model_key]
            if quantization.get("bits", 16) != expected:
                raise ValueError("선택한 모델의 비트 수와 폴더의 모델이 다릅니다. 모델 선택을 폴더에 맞춰 주세요.")
        chosen_id, chosen_key, chosen_options = model_id or self.model_id, model_key or self.model_key, dict(options or self.options)
        if model_id and remember:
            self.model_id = model_id
        if model_key and remember:
            self.model_key = model_key
        if options and remember:
            self.options = options.copy()
        self.state = {"phase": "checking", "source": source}
        self.task = asyncio.create_task(self.run(source, selected, model_id=chosen_id, model_key=chosen_key,
                                                 options=chosen_options, remember=remember))
        return self.status()

    async def restore(self) -> None:
        # Reopening the app inspects disk only. Weights are loaded per job.
        ready = self.readiness_for(self.current_selection())
        self.state = {'phase': 'installed' if ready['ready'] else 'idle'}

    async def command(self, arguments: list[str]) -> str:
        process = await asyncio.create_subprocess_exec(*arguments, cwd=self.root, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            lines = []
            async for line in process.stdout:
                text = line.decode(errors="replace")
                if text.startswith("COSMOSS_DOWNLOAD:"):
                    self.state.update(phase="downloading", download=json.loads(text.split(":", 1)[1]))
                else:
                    lines.append(text)
            await process.wait()
            text = "".join(lines)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as log:
                log.write(text)
            if process.returncode:
                raise RuntimeError(text[-1200:] or "실행 환경을 준비하지 못했습니다.")
            return text
        finally:
            if process.returncode is None:
                async def stop():
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.communicate(), 5)
                    except TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.communicate()
                await finish_cleanup(stop())

    async def prepare_runtime(self) -> Path:
        python = self.runtime / "bin/python"
        stamp = self.runtime / "cosmoss-runtime-version"
        self.state.update(phase="installing_uv")
        uv = await asyncio.to_thread(ensure_uv, self.data / "runtime-tools")
        checked = await self.environment()
        if checked["ready"]:
            return python
        await self.stop_process()
        self.state.update(phase="installing")
        if not checked["venv"]["ready"]:
            if self.runtime.is_symlink():
                raise ValueError("실행 환경 경로를 확인해 주세요.")
            if self.runtime.exists() and not (self.runtime / "pyvenv.cfg").is_file():
                raise ValueError("기존 폴더가 Python venv가 아닙니다. MLX_VENV 경로를 확인해 주세요.")
            for value in (self.saved.get("model_path"), os.getenv("MLX_MODEL_PATH")):
                if value and Path(value).expanduser().resolve().is_relative_to(self.runtime):
                    raise ValueError("모델이 실행 환경 안에 있습니다. 별도 모델 폴더를 지정해 주세요.")
            self.runtime.parent.mkdir(parents=True, exist_ok=True)
            await self.command([uv, "venv", "--clear", "--python", sys.executable, str(self.runtime)])
        installer = [uv, "pip", "install", "--python", str(python)]
        await self.command(installer + ["mlx-audio==0.5.7", "mlx-lm==0.31.3", "jinja2", "fastapi>=0.116,<1", "uvicorn[standard]>=0.35,<1", "python-multipart"])
        stamp.write_text(RUNTIME_VERSION)
        checked = await self.environment()
        if not checked["ready"]:
            raise RuntimeError("실행 환경 검증에 실패했습니다. 누락된 패키지를 확인해 주세요.")
        return python

    async def environment(self) -> dict[str, Any]:
        result = await asyncio.to_thread(inspect_environment, self.runtime, self.runtime / "cosmoss-runtime-version",
                                         RUNTIME_VERSION, self.data / "runtime-tools", "mlx")
        self.state["runtime"] = result
        return result

    def start_environment(self) -> dict[str, Any]:
        if self.task and not self.task.done():
            return self.status()
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise ValueError("Apple Silicon Mac에서 실행 환경을 설치할 수 있습니다.")
        self.state.update(phase="installing_uv")
        self.state.pop("error", None)
        self.task = asyncio.create_task(self.run_environment())
        return self.status()

    async def run_environment(self) -> None:
        previous = 'installed' if self.readiness_for(self.current_selection())['ready'] else 'idle'
        try:
            async with self.lock:
                await self.prepare_runtime()
            self.state.update(phase='installed' if self.readiness_for(self.current_selection())['ready'] else 'idle')
        except asyncio.CancelledError:
            self.state.update(phase=previous)
            raise
        except Exception as error:
            self.state.update(phase="error", error=str(error))

    async def download(self, python: Path, model_id: str | None = None) -> Path:
        model_id = model_id or self.model_id
        cached = await asyncio.to_thread(find_cached_model, self.cache, model_id)
        if cached:
            self.state.update(reused=True)
            return cached
        self.state.update(phase="downloading", reused=False)
        script = (Path(__file__).parent / "model_download.py").read_text()
        result = await self.command([str(python), "-c", script, model_id, str(self.cache)])
        lines = [line.split("COSMOSS_MODEL_PATH:", 1)[1] for line in result.splitlines() if line.startswith("COSMOSS_MODEL_PATH:")]
        if not lines:
            raise RuntimeError("다운로드한 모델 경로를 확인하지 못했습니다.")
        path = validate_model_folder(lines[-1])
        if not path.is_relative_to(self.cache):
            raise RuntimeError("모델이 지정한 Hugging Face 캐시에 저장되지 않았습니다.")
        return path

    async def probe(self) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(f"http://127.0.0.1:{self.port}/healthz")
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def launch(self, python: Path, path: Path, *, options=None) -> None:
        options = {**DEFAULT_OPTIONS, **(self.options if options is None else options)}
        configuration_id = hashlib.sha256(json.dumps({key: options[key] for key in ("kv_bits", "prefill_step_size", "max_tokens")}, sort_keys=True).encode()).hexdigest()
        if self.process:
            current = await self.probe()
            if self.process.returncode is None and current and current.get("model_path") == str(path) and current.get("configuration_id") == configuration_id:
                return
            await self.stop_process()
        try:
            connection = await asyncio.open_connection("127.0.0.1", self.port)
        except OSError:
            connection = None
        if connection:
            connection[1].close()
            await connection[1].wait_closed()
            # A finite job must release its own weights before Nemotron starts.
            # A manually started server belongs to its owner and stays untouched.
            raise RuntimeError(f"연결 포트 {self.port}를 이미 사용 중입니다. 기존 로컬 모델을 종료한 뒤 다시 준비해 주세요.")
        environment = dict(os.environ, MLX_MODEL_PATH=str(path), MOSS_MODEL_NAME=self.model_name,
                           HF_HUB_CACHE=str(self.cache), HF_HUB_OFFLINE="1", COSMOSS_LOCAL_MODEL_INSTANCE=uuid.uuid4().hex,
                           COSMOSS_LOCAL_MODEL_PARENT_PID=str(os.getpid()),
                           MLX_KV_BITS=str(options["kv_bits"]), MLX_PREFILL_STEP_SIZE=str(options["prefill_step_size"]),
                           MLX_MAX_TOKENS=str(options["max_tokens"]), COSMOSS_CONFIG_ID=configuration_id)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(str(python), "-m", "uvicorn", "src.mlx_server:app", "--host", "127.0.0.1", "--port", str(self.port), "--workers", "1",
                                                                cwd=self.root, env=environment, stdout=log, stderr=log, start_new_session=True)
        for _ in range(900):
            if self.process.returncode is not None:
                raise RuntimeError("로컬 모델을 실행하지 못했습니다. " + self.log_path.read_text(errors="replace")[-1200:])
            ready = await self.probe()
            if ready and ready.get("instance_id") == environment["COSMOSS_LOCAL_MODEL_INSTANCE"] and ready.get("model_path") == str(path):
                return
            await asyncio.sleep(1)
        raise RuntimeError("모델을 불러오는 데 시간이 너무 오래 걸립니다. 모델 준비를 다시 시도해 주세요.")

    async def run(self, source: str, path: Path | None, restore: bool = False, *,
                  model_id=None, model_key=None, options=None, remember=True) -> None:
        model_id, model_key = model_id or self.model_id, model_key or self.model_key
        options = dict(options or self.options)
        try:
            if restore:
                await self.restore()
                return
            async with self.lock:
                await self.stop_process()
                python = await self.prepare_runtime()
                if path is None:
                    path = await self.download(python, model_id)
            if self.diarization is not None:
                def on_event(event):
                    self.state.update({key: value for key, value in event.items() if key not in {'model_key', 'model_id', 'model_path'}}, component='diarization')
                result = await self.diarization.execute('nemotron-3', 'install', on_event)
                if result.get('phase') != 'installed' or not self.diarization.ready():
                    self.state.update(result)
                    if result.get('phase') not in {'auth_required', 'error'}:
                        self.state.update(phase='error', error='Nemotron 설치 완료를 확인하지 못했습니다.')
                    return
            if remember:
                saved = {"source": source, "model_path": str(path), "model_id": model_id, "model_key": model_key, "options": options}
                self.data.mkdir(parents=True, exist_ok=True)
                temporary = self.settings_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(saved, ensure_ascii=False))
                temporary.replace(self.settings_file)
                self.saved = saved
                self.model_id, self.model_key, self.options = model_id, model_key, options
            self.state = {'phase': 'installed' if remember or self.readiness_for(self.current_selection())['ready'] else 'idle',
                          'reused': self.state.get('reused', source == 'folder')}
        except asyncio.CancelledError:
            self.state.update(phase="idle")
            raise
        except Exception as error:
            self.state.update(phase="error", error=str(error))

    @asynccontextmanager
    async def inference_session(self, device, on_event):
        """Load one job's saved selection, then free its weights before diarization."""
        selection = {**device, 'options': dict(device.get('options', {}))}
        async with self.lock:
            readiness = self.readiness_for(selection)
            needs_diarization = selection['options'].get('local_diarization') == 'nemotron-3'
            if not readiness['asr_ready'] or (needs_diarization and not readiness['diarization_ready']):
                raise ValueError('선택한 Mac 모델과 Nemotron을 먼저 준비해 주세요.')
            path = self.model_path_for(selection)
            on_event({'phase': 'loading', 'component': 'asr'})
            self.state.update(phase='loading')
            try:
                await self.launch(self.runtime / 'bin/python', path, options=selection['options'])
                self.state.update(phase='ready')
                on_event({'phase': 'running', 'component': 'asr'})
                yield
            finally:
                try:
                    await finish_cleanup(self.stop_process())
                finally:
                    self.state = {'phase': 'installed' if self.readiness_for(self.current_selection())['ready'] else 'idle'}

    async def stop_process(self) -> None:
        process = self.process
        async def stop():
            if process and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 8)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if self.process is process:
                self.process = None
        await finish_cleanup(stop())

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await finish_cleanup(self.stop_process())
