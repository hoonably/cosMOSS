"""Inspect isolated runtimes without importing models or initializing a GPU."""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
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


def ensure_uv(tools, environment=None):
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
        subprocess.run(["/bin/sh", str(script)], env=environment, check=True, timeout=180)
    installed = find_uv(tools)
    if not installed["ready"]:
        raise RuntimeError("uv 설치를 확인하지 못했습니다. 실행 환경 설치를 다시 시도해 주세요.")
    return installed["path"]
