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
from pathlib import Path
from typing import Any

import httpx

from src.model_cache import default_cache
from src.model_catalog import DEFAULT_OPTIONS, MODELS
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
    def __init__(self, root: Path, data: Path, port: int, model_name: str):
        self.root, self.data, self.port, self.model_name = root, data, port, model_name
        self.settings_file = data / "local-model.json"
        self.runtime = Path(os.getenv("MLX_VENV") or root / ".venv-mlx").expanduser().resolve()
        self.model_id = os.getenv("MLX_MODEL_ID", DEFAULT_MODEL)
        self.cache = default_cache().resolve()
        self.task: asyncio.Task | None = None
        self.process: asyncio.subprocess.Process | None = None
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
        if self.state["phase"] == "ready" and self.process and self.process.returncode is not None:
            self.state = {"phase": "error", "error": "로컬 모델 실행이 종료되었습니다. 모델을 다시 준비해 주세요."}
        suggested = self.saved.get("model_path") or os.getenv("MLX_MODEL_PATH") or str(self.root / "models/moss-mlx-8bit")
        return {"model_id": self.model_id, "model_key": self.model_key, "options": self.options, "cache_dir": str(self.cache), "source": self.saved.get("source", "cache"),
                "model_path": self.saved.get("model_path", ""),
                "suggested_path": suggested if Path(suggested).expanduser().is_dir() else "",
                **self.state}

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
        self.state = {'phase': 'idle'}
        return self.status()

    def start(self, source: str, path: str = "", *, model_id=None, model_key=None, options=None) -> dict[str, Any]:
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
        if model_id:
            self.model_id = model_id
        if model_key:
            self.model_key = model_key
        if options:
            self.options = options.copy()
        self.state = {"phase": "checking", "source": source}
        self.task = asyncio.create_task(self.run(source, selected))
        return self.status()

    async def restore(self) -> None:
        # Reopen an explicitly prepared model without triggering new downloads.
        if self.saved.get("model_path") and (self.runtime / "bin/python").is_file():
            try:
                path = validate_model_folder(self.saved["model_path"])
            except ValueError:
                return
            self.state = {"phase": "loading"}
            self.task = asyncio.create_task(self.run(self.saved.get("source", "folder"), path, restore=True))

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
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()

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
        previous = "ready" if self.process and self.process.returncode is None else "idle"
        try:
            await self.prepare_runtime()
            self.state.update(phase="ready" if self.process and self.process.returncode is None else "idle")
        except asyncio.CancelledError:
            self.state.update(phase=previous)
            raise
        except Exception as error:
            self.state.update(phase="error", error=str(error))

    async def download(self, python: Path) -> Path:
        cached = await asyncio.to_thread(find_cached_model, self.cache, self.model_id)
        if cached:
            self.state.update(reused=True)
            return cached
        self.state.update(phase="downloading", reused=False)
        script = (Path(__file__).parent / "model_download.py").read_text()
        result = await self.command([str(python), "-c", script, self.model_id, str(self.cache)])
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

    async def launch(self, python: Path, path: Path) -> None:
        configuration_id = hashlib.sha256(json.dumps({key: self.options[key] for key in ("kv_bits", "prefill_step_size", "max_tokens")}, sort_keys=True).encode()).hexdigest()
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
            existing = await self.probe()
            if existing and existing.get("app") == "cosMOSS MLX" and existing.get("model_path") == str(path) and existing.get("model") == self.model_name and existing.get("configuration_id") == configuration_id:
                return  # Reuse, but never take ownership of a manually started model.
            raise RuntimeError(f"연결 포트 {self.port}를 이미 사용 중입니다. 기존 로컬 모델을 종료한 뒤 다시 준비해 주세요.")
        environment = dict(os.environ, MLX_MODEL_PATH=str(path), MOSS_MODEL_NAME=self.model_name,
                           HF_HUB_CACHE=str(self.cache), HF_HUB_OFFLINE="1", COSMOSS_LOCAL_MODEL_INSTANCE=uuid.uuid4().hex,
                           MLX_KV_BITS=str(self.options["kv_bits"]), MLX_PREFILL_STEP_SIZE=str(self.options["prefill_step_size"]),
                           MLX_MAX_TOKENS=str(self.options["max_tokens"]), COSMOSS_CONFIG_ID=configuration_id)
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

    async def run(self, source: str, path: Path | None, restore: bool = False) -> None:
        try:
            python = self.runtime / "bin/python" if restore else await self.prepare_runtime()
            if path is None:
                path = await self.download(python)
            self.state.update(phase="loading", model_path=str(path))
            await self.launch(python, path)
            saved = {"source": source, "model_path": str(path), "model_id": self.model_id, "model_key": self.model_key, "options": self.options}
            self.data.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved, ensure_ascii=False))
            temporary.replace(self.settings_file)
            self.saved = saved
            self.state.update(phase="ready")
        except asyncio.CancelledError:
            self.state.update(phase="idle")
            await self.stop_process()
            raise
        except Exception as error:
            await self.stop_process()
            self.state.update(phase="error", error=str(error))

    async def stop_process(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.stop_process()
