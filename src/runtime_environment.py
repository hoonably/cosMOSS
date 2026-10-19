"""Inspect isolated runtimes and share CUDA environments without loading models."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

UV_VERSION = "0.9.25"
UV_INSTALLER = "https://astral.sh/uv/%s/install.sh" % UV_VERSION
PACKAGE_PROBE = r'''
import importlib.metadata as metadata
import importlib.util
import json, sys
from pathlib import Path
backend = sys.argv[1]
required = {"fastapi":"fastapi", "uvicorn":"uvicorn", "python-multipart":"multipart", "jinja2":"jinja2"}
required.update({"torch":"torch", "torchaudio":"torchaudio", "transformers":"transformers", "huggingface-hub":"huggingface_hub"} if backend == "cuda" else {"mlx-audio":"mlx_audio", "mlx-lm":"mlx_lm"})
if len(sys.argv) > 2 and sys.argv[2] in ("int4", "int8"):
    required.update({"bitsandbytes":"bitsandbytes", "accelerate":"accelerate"})
versions, missing = {}, []
for name, module in required.items():
    try:
        versions[name] = metadata.version(name)
        if importlib.util.find_spec(module) is None:
            missing.append(name)
    except (metadata.PackageNotFoundError, ImportError, ValueError):
        missing.append(name)
expected = {"torch":"2.8.0+cu128", "torchaudio":"2.8.0+cu128"} if backend == "cuda" else {"mlx-audio":"0.5.7", "mlx-lm":"0.31.3"}
for name, version in expected.items():
    if versions.get(name) != version and name not in missing:
        missing.append(name)
configuration = Path(sys.prefix) / "pyvenv.cfg"
settings = dict(line.lower().split("=", 1) for line in configuration.read_text().splitlines() if "=" in line) if configuration.is_file() else {}
isolated = sys.prefix != sys.base_prefix and any(key.strip() == "include-system-site-packages" and value.strip() == "false" for key, value in settings.items())
print(json.dumps({"version":"%d.%d.%d" % sys.version_info[:3], "isolated":isolated,
                  "supported":sys.version_info >= (3, 10), "versions":versions, "missing":missing}))
'''


def find_uv(tools):
    for candidate in (shutil.which(os.getenv("UV_BIN", "uv")), str(Path(tools) / "uv"),
                      str(Path.home() / ".local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            try:
                result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=8)
                if result.returncode == 0:
                    return {"ready": True, "path": candidate, "version": result.stdout.strip()}
            except (OSError, subprocess.SubprocessError):
                pass
    return {"ready": False, "path": str(Path(tools) / "uv"), "version": ""}


def inspect_environment(runtime, stamp, expected, tools, backend="cuda", precision="auto"):
    runtime = Path(runtime)
    python = runtime / "bin/python"
    result = {"uv": find_uv(tools), "venv": {"ready": False, "path": str(runtime), "version": ""},
              "packages": {"ready": False, "versions": {}, "missing": []}, "ready": False}
    if not python.is_file():
        return result
    try:
        # -I ignores inherited PYTHONPATH/user packages; -B prevents bytecode leftovers.
        process = subprocess.run([str(python), "-I", "-B", "-c", PACKAGE_PROBE, backend, precision],
                                 capture_output=True, text=True, timeout=20)
        if process.returncode:
            raise RuntimeError(process.stderr.strip()[-800:] or "Python을 실행하지 못했습니다.")
        details = json.loads(process.stdout)
        result["venv"].update(ready=details["isolated"] and details["supported"], version=details["version"])
        current = Path(stamp).is_file() and Path(stamp).read_text() == expected
        result["packages"].update(ready=current and not details["missing"], versions=details["versions"], missing=details["missing"])
        result["ready"] = all(result[key]["ready"] for key in ("uv", "venv", "packages"))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def ensure_uv(tools, environment=None, pass_fds=()):
    current = find_uv(tools)
    if current["ready"]:
        return current["path"]
    tools = Path(tools)
    tools.mkdir(parents=True, exist_ok=True)
    environment = dict(environment or os.environ)
    # The official unmanaged installer leaves shell profiles and global tools alone.
    environment.update(UV_UNMANAGED_INSTALL=str(tools), UV_NO_MODIFY_PATH="1")
    with tempfile.TemporaryDirectory(prefix="cosmoss-uv-", dir=environment.get("TMPDIR")) as temporary:
        script = Path(temporary) / "install.sh"
        request = urllib.request.Request(UV_INSTALLER, headers={"User-Agent": "cosMOSS/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            script.write_bytes(response.read(1024 * 1024))
        subprocess.run(["/bin/sh", str(script)], env=environment, check=True, timeout=180, pass_fds=pass_fds)
    installed = find_uv(tools)
    if not installed["ready"]:
        raise RuntimeError("uv 설치를 확인하지 못했습니다. 실행 환경 설치를 다시 시도해 주세요.")
    return installed["path"]


# Account-scoped CUDA environments travel with this standalone SSH helper.
# Keep ready installations immutable so workers from other Macs can use them.
class SharedRuntime:
    def __init__(self, version, precision="auto", quantization_version="", probe=None):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", version):
            raise ValueError("Invalid runtime version")
        self.version = version
        self.precision = precision
        self.quantization_version = quantization_version
        self.base = Path.home() / ".local/share/cosmoss"
        self.key = version + ("-quantized" if precision in {"int4", "int8"} else "")
        self.slot = self.base / "runtimes" / self.key
        self.probe = probe or inspect_environment

    def safe_path(self, path):
        """Do not follow directory/metadata symlinks outside the SSH account."""
        try:
            parts = path.relative_to(Path.home()).parts
            current = Path.home()
            for part in parts:
                if part in {".", ".."}:
                    return False
                current = current / part
                if current.is_symlink() or (current.exists() and current.stat().st_uid != os.getuid()):
                    return False
            return True
        except (ValueError, OSError):
            return False

    def read_json(self, path):
        try:
            if self.safe_path(path) and path.stat().st_size <= 4096:
                return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        return None

    def marker(self, complete):
        return {"app": "cosMOSS", "schema": 1, "runtime": self.key, "complete": complete}

    def valid_root(self, root, complete=True):
        if (not self.safe_path(root / ".venv") or not self.safe_path(root / "runtime-version")
                or not self.safe_path(root / "quantization-version") or not root.is_dir()):
            return False
        if root.parent == self.base / "workers" and re.fullmatch(r"[0-9a-f]{32}", root.name):
            if not complete:
                return False
            owner_file = root / "owner.json"
            owner = self.read_json(owner_file)
            if owner_file.exists() or owner_file.is_symlink():
                if not (isinstance(owner, dict) and owner.get("app") == "cosMOSS"
                        and owner.get("device_id") == root.name
                        and re.fullmatch(r"[0-9a-f]{64}", str(owner.get("token_hash", "")))):
                    return False
            # Pre-ownership-marker versions are recognized by their exact stamp.
            try:
                stamp = root / "runtime-version"
                return self.safe_path(stamp) and stamp.read_text() == self.version
            except OSError:
                return False
        return (root.parent == self.slot / "installs" and bool(re.fullmatch(r"[0-9a-f]{32}", root.name))
                and self.read_json(root / "runtime-owner.json") == self.marker(complete))

    def check(self, root):
        tools = root / "tools"
        if not self.safe_path(tools):
            raise RuntimeError("공유 실행 환경 도구 경로를 확인해 주세요.")
        result = self.probe(root / ".venv", root / "runtime-version", self.version, tools,
                            precision=self.precision)
        if result.get("uv", {}).get("ready") is False and tools != self.slot / "tools":
            tools = self.slot / "tools"
            if not self.safe_path(tools):
                raise RuntimeError("공유 실행 환경 도구 경로를 확인해 주세요.")
            result = self.probe(root / ".venv", root / "runtime-version", self.version, tools,
                                precision=self.precision)
        if self.precision in {"int4", "int8"}:
            stamp = root / "quantization-version"
            try:
                current = self.safe_path(stamp) and stamp.read_text() == self.quantization_version
            except OSError:
                current = False
            if not current:
                result["packages"]["ready"] = False
                result["ready"] = False
        return {**result, "shared": True}

    def candidates(self):
        if not self.safe_path(self.base):
            raise RuntimeError("공유 실행 환경 경로를 확인해 주세요. 심볼릭 링크는 사용할 수 없습니다.")
        pointer = self.read_json(self.slot / "current.json")
        if isinstance(pointer, dict) and isinstance(pointer.get("root"), str):
            yield self.base / pointer["root"]
        for parent in (self.slot / "installs", self.base / "workers"):
            if self.safe_path(parent) and parent.is_dir():
                yield from sorted(parent.iterdir())

    def find(self):
        seen = set()
        failed = None
        for root in self.candidates():
            if root in seen or not self.valid_root(root):
                continue
            seen.add(root)
            checked = self.check(root)
            if checked["venv"]["ready"] and checked["packages"]["ready"]:
                return root, checked
            if failed is None:
                failed = checked
        return None, failed

    def inspect(self):
        _, checked = self.find()
        # A read-only check must not create a worker, lock, tools or directories.
        if checked is not None:
            return checked
        if not self.safe_path(self.slot / ".venv"):
            raise RuntimeError("공유 실행 환경 경로를 확인해 주세요.")
        return self.check(self.slot)

    def write_json(self, path, value):
        if not self.safe_path(path):
            raise RuntimeError("공유 실행 환경 기록 경로를 확인해 주세요.")
        temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
        try:
            with temporary.open("x") as output:
                os.chmod(temporary, 0o600)
                json.dump(value, output)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def installation_lock(self, emit):
        if not self.safe_path(self.slot):
            raise RuntimeError("공유 실행 환경 경로를 확인해 주세요.")
        self.slot.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.slot / "install.lock"
        if not self.safe_path(path):
            raise RuntimeError("공유 실행 환경 잠금 경로를 확인해 주세요.")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 1500
            waiting = False
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        emit("waiting_environment")
                        waiting = True
                    if time.monotonic() >= deadline:
                        raise RuntimeError("다른 장치의 실행 환경 준비가 끝난 뒤 다시 시도해 주세요.")
                    time.sleep(0.2)
            yield descriptor
        finally:
            os.close(descriptor)

    def ensure(self, emit, install):
        emit("checking_environment")
        root, checked = self.find()
        if root is not None and checked["ready"]:
            emit("checking_environment", runtime=checked, runtime_reused=True)
            return root / ".venv/bin/python"
        with self.installation_lock(emit) as descriptor:
            # Another Mac may have completed setup while this request waited.
            root, checked = self.find()
            if root is not None:
                if not checked["uv"]["ready"]:
                    emit("installing_uv")
                    ensure_uv(self.slot / "tools", pass_fds=(descriptor,))
                    checked = self.check(root)
                if not checked["ready"]:
                    raise RuntimeError("공유 실행 환경 도구를 확인하지 못했습니다.")
                emit("checking_environment", runtime=checked, runtime_reused=True)
                return root / ".venv/bin/python"
            installs = self.slot / "installs"
            if not self.safe_path(installs):
                raise RuntimeError("공유 실행 환경 설치 경로를 확인해 주세요.")
            installs.mkdir(exist_ok=True, mode=0o700)
            # Resume only an unpublished install. Never repair an environment
            # that a live worker may still be using, or change a legacy owner.
            root = next((p for p in sorted(installs.iterdir()) if self.valid_root(p, complete=False)), None)
            if root is None:
                root = installs / uuid.uuid4().hex
                root.mkdir(mode=0o700)
                self.write_json(root / "runtime-owner.json", self.marker(False))
            # Installers inherit the lock: a lost SSH/bootstrap process must not
            # let another Mac mutate the venv while its uv child is still alive.
            install(root, descriptor)
            checked = self.check(root)
            if not checked["ready"]:
                raise RuntimeError("실행 환경 검증에 실패했습니다. 누락된 패키지를 확인해 주세요.")
            self.write_json(root / "runtime-owner.json", self.marker(True))
            self.write_json(self.slot / "current.json", {"root": str(root.relative_to(self.base))})
            emit("checking_environment", runtime=checked, runtime_reused=False)
            return root / ".venv/bin/python"
