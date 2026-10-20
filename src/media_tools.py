"""Common media tools, independent of the local/remote transcription models."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


VERSION = "9.0.1-1"
ARCHIVE_ROOT = f"ffmpeg-{VERSION}-lgpl-macos-arm64"
ARCHIVE_URL = f"https://github.com/dfloresc/ffmpeg-macos-builds/releases/download/v{VERSION}/{ARCHIVE_ROOT}.tar.xz"
ARCHIVE_SHA256 = "d0ea5ea43a5adfa5d01d4137c9e5a61728b800a812f12917a26fd7af0c14c03a"
ARCHIVE_BYTES = 10351016
TOOLS = ("ffmpeg", "ffprobe")


def probe_tool(path: Path | None, name: str) -> dict:
    result = {"ready": False, "path": str(path) if path else "", "version": ""}
    if path is None or not path.is_file() or not os.access(path, os.X_OK):
        return result
    try:
        process = subprocess.run([str(path), "-version"], capture_output=True, text=True, timeout=8)
        first = process.stdout.splitlines()[0] if process.stdout else ""
        if process.returncode == 0 and first.startswith(name + " version "):
            result.update(ready=True, version=first.split()[2])
    except (OSError, subprocess.SubprocessError):
        pass
    return result


class MediaTools:
    def __init__(self, root: Path):
        self.root = root
        self.task: asyncio.Task | None = None
        self.state = {"phase": "idle"}
        self.cancelled = threading.Event()
        self.paths: dict[str, str] = {}

    def managed_folder(self) -> Path | None:
        try:
            metadata = json.loads((self.root / "current.json").read_text())
            relative = Path(metadata["folder"])
            if (metadata.get("archive_sha256") != ARCHIVE_SHA256 or relative.is_absolute()
                    or ".." in relative.parts or len(relative.parts) != 3 or relative.parts[0] != "installs"
                    or relative.parts[2] != ARCHIVE_ROOT):
                return None
            folder = self.root / relative
            if folder.resolve().is_relative_to(self.root.resolve()):
                return folder
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def inspect(self) -> dict:
        folder = self.managed_folder()
        result = {}
        paths = {}
        for name in TOOLS:
            existing = shutil.which(name)
            component = probe_tool(Path(existing) if existing else None, name)
            component["source"] = "system"
            if not component["ready"] and folder is not None:
                component = {**probe_tool(folder / "bin" / name, name), "source": "app"}
            result[name] = component
            if component["ready"]:
                paths[name] = component["path"]
        self.paths = paths
        return {"ready": len(paths) == len(TOOLS), "components": result}

    def status(self) -> dict:
        checked = self.inspect()
        state = self.state.copy()
        busy = self.task is not None and not self.task.done()
        if not busy:
            state["phase"] = "ready" if checked["ready"] else "error" if state.get("error") else "idle"
        if checked["ready"]:
            state.pop("error", None)
        return {**state, **checked, "busy": busy}

    def start(self) -> dict:
        if self.task is None or self.task.done():
            self.cancelled.clear()
            self.state = {"phase": "checking"}
            self.task = asyncio.create_task(self.run())
        return {**self.state, "busy": True}

    async def run(self) -> None:
        try:
            await asyncio.to_thread(self.install)
            self.state = {"phase": "ready"}
        except (urllib.error.URLError, TimeoutError):
            self.state = {"phase": "error", "error": "도구를 다운로드하지 못했습니다. 인터넷 연결을 확인하고 설정에서 다시 준비해 주세요."}
        except tarfile.TarError:
            self.state = {"phase": "error", "error": "도구 압축 파일을 확인하지 못했습니다. 설정에서 다시 준비해 주세요."}
        except Exception as error:
            self.state = {"phase": "error", "error": str(error)}

    async def ensure_ready(self) -> None:
        self.start()
        await asyncio.shield(self.task)
        if self.state["phase"] != "ready":
            raise RuntimeError(self.state.get("error", "파일 처리 도구를 준비하지 못했습니다."))

    async def require_ready(self) -> None:
        # Only the protected setup endpoint starts software downloads. Uploads
        # can wait for that setup, but cannot bypass its local/same-site checks.
        if self.task is not None and not self.task.done():
            await asyncio.shield(self.task)
        if not (await asyncio.to_thread(self.inspect))["ready"]:
            raise RuntimeError(self.state.get("error", "상단 설정에서 파일 처리 도구를 준비해 주세요."))

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError("파일 처리 도구 준비가 중단되었습니다. 설정에서 다시 준비해 주세요.")

    def install(self) -> None:
        if self.inspect()["ready"]:
            return
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("파일 처리 도구 자동 설치는 Apple Silicon Mac에서 지원합니다.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.root / "install.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 600
            while True:
                self.check_cancelled()
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.state = {"phase": "checking"}
                    if time.monotonic() >= deadline:
                        raise RuntimeError("다른 앱의 도구 준비가 끝난 뒤 다시 시도해 주세요.")
                    self.cancelled.wait(0.2)
            if self.inspect()["ready"]:
                return
            self.download_and_publish()
        finally:
            os.close(descriptor)

    def download(self, path: Path) -> None:
        digest = hashlib.sha256()
        received = 0
        self.state = {"phase": "downloading", "download": {"percent": 0, "downloaded_bytes": 0, "total_bytes": ARCHIVE_BYTES}}
        request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "cosMOSS/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response, path.open("xb") as output:
            while chunk := response.read(256 * 1024):
                self.check_cancelled()
                received += len(chunk)
                if received > ARCHIVE_BYTES:
                    raise RuntimeError("도구 다운로드 크기가 예상과 다릅니다. 설정에서 다시 시도해 주세요.")
                digest.update(chunk)
                output.write(chunk)
                self.state = {"phase": "downloading", "download": {"percent": round(received / ARCHIVE_BYTES * 100, 1),
                              "downloaded_bytes": received, "total_bytes": ARCHIVE_BYTES}}
        if received != ARCHIVE_BYTES or digest.hexdigest() != ARCHIVE_SHA256:
            raise RuntimeError("도구 다운로드 검증에 실패했습니다. 설정에서 다시 시도해 주세요.")

    def download_and_publish(self) -> None:
        installs = self.root / "installs"
        installs.mkdir(exist_ok=True, mode=0o700)
        generation = installs / uuid.uuid4().hex
        published = False
        pointer = self.root / (".current-" + uuid.uuid4().hex + ".json")
        try:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=self.root) as temporary:
                archive = Path(temporary) / "tools.tar.xz"
                self.download(archive)
                self.check_cancelled()
                self.state = {"phase": "installing"}
                generation.mkdir(mode=0o700)
                with tarfile.open(archive, "r:xz") as package:
                    members = package.getmembers()
                    if len(members) > 1024 or sum(m.size for m in members) > 256 * 1024**2:
                        raise RuntimeError("도구 압축 파일 크기가 예상과 다릅니다.")
                    for member in members:
                        parts = Path(member.name).parts
                        if not parts or parts[0] != ARCHIVE_ROOT or ".." in parts or member.isdev() or member.isfifo():
                            raise RuntimeError("도구 압축 파일에 올바르지 않은 경로가 있습니다.")
                    package.extractall(generation, members=members, filter="data")
                folder = generation / ARCHIVE_ROOT
                for name in TOOLS:
                    self.check_cancelled()
                    if not probe_tool(folder / "bin" / name, name)["ready"]:
                        raise RuntimeError(f"{name} 실행을 확인하지 못했습니다. 설정에서 다시 준비해 주세요.")
                # Preserve bin/, lib/, LICENSES/ and build provenance together.
                metadata = {"folder": str(folder.relative_to(self.root)), "archive_sha256": ARCHIVE_SHA256}
                pointer.write_text(json.dumps(metadata))
                pointer.replace(self.root / "current.json")
                published = True
                if not self.inspect()["ready"]:
                    raise RuntimeError("파일 처리 도구를 확인하지 못했습니다.")
        finally:
            pointer.unlink(missing_ok=True)
            if not published:
                shutil.rmtree(generation, ignore_errors=True)

    def executable(self, name: str) -> str:
        if name not in TOOLS:
            raise ValueError("Unknown media tool")
        cached = self.paths.get(name)
        if cached and Path(cached).is_file():
            return cached
        self.inspect()
        if name not in self.paths:
            raise RuntimeError("상단 설정에서 파일 처리 도구를 준비해 주세요.")
        return self.paths[name]

    async def close(self) -> None:
        self.cancelled.set()
        if self.task and not self.task.done():
            await asyncio.shield(self.task)
