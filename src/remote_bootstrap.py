"""Prepare device models without a GPU, then launch a GPU only for a job."""

import json
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from src.runtime_environment import ensure_uv, inspect_environment, SharedRuntime
except ModuleNotFoundError:
    # RemoteWorkers prepends the same standalone helper source over SSH.
    pass


UPSTREAM = "https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/archive/e3bdd93f8ad58418e04659818efd5fd87a0bc486.tar.gz"
PROCESSES = Path("/proc")
RUNTIME_VERSION = "cuda128-torch280-moss-e3bdd93f-v1"
QUANTIZATION_VERSION = "bitsandbytes-0.48.1-accelerate-v1"
JOB_FILES = ("src", "tmp", "cache", "__pycache__", "worker.log", "worker.pid", "worker.json")


def model_cache(device):
    if device.get("model_cache") or os.environ.get("HF_HUB_CACHE"):
        return Path(device.get("model_cache") or os.environ["HF_HUB_CACHE"]).expanduser()
    return Path(os.environ.get("HF_HOME") or (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface")).expanduser() / "hub"


def owned_root(root, device):
    if root.is_symlink():
        return False
    marker = root / "owner.json"
    if marker.is_file():
        try:
            saved = json.loads(marker.read_text())
        except (OSError, ValueError):
            return False
        return saved == {"app": "cosMOSS", "device_id": device["id"], "token_hash": hashlib.sha256(device["api_key"].encode()).hexdigest()}
    # Recognize the managed runtime created by earlier versions of this app.
    stamp = root / "runtime-version"
    return stamp.is_file() and stamp.read_text() == RUNTIME_VERSION


def guard_models(root, device, details=None):
    cache = model_cache(device).absolute()
    protected = [cache / ("models--" + device["model"].replace("/", "--"))]
    if cache != root.resolve() and cache.is_relative_to(root.resolve()):
        protected.append(cache)
    if device.get("model_path"):
        protected.append(Path(device["model_path"]).expanduser())
    if details:
        if details.get("model_path"):
            protected.append(Path(details["model_path"]).expanduser())
    transient = [root / name for name in (*JOB_FILES, ".venv")]
    if any(candidate.is_relative_to(item.absolute()) or item.absolute().is_relative_to(candidate)
           for path in protected for candidate in (path.absolute(), path.resolve()) for item in transient):
        raise RuntimeError("모델 경로가 cosMOSS 임시 작업 폴더와 겹쳐 삭제하지 않았습니다. 모델은 Hugging Face 기본 캐시 또는 별도 폴더에 보관해 주세요.")


def claim_root(root, device):
    guard_models(root, device)
    if root.is_symlink() or (root.exists() and any(root.iterdir()) and not owned_root(root, device)):
        raise RuntimeError("Worker directory ownership could not be verified")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    (root / "owner.json").write_text(json.dumps({"app": "cosMOSS", "device_id": device["id"], "token_hash": hashlib.sha256(device["api_key"].encode()).hexdigest()}))


def cleanup_job_files(root, device, verified=False, details=None):
    if not root.exists() and not root.is_symlink():
        return
    guard_models(root, device, details)
    if root.is_symlink() or (not verified and not owned_root(root, device)):
        raise RuntimeError("Worker directory ownership could not be verified; no files were removed")
    for name in JOB_FILES:
        path = root / name
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    try:
        (root / "python").rmdir()  # Keep managed interpreters only when installed.
    except OSError:
        pass


def temporary_environment(root, device):
    environment = dict(os.environ)
    cache = root / "cache"
    paths = {
        "TMPDIR": root / "tmp", "XDG_CACHE_HOME": cache,
        "UV_CACHE_DIR": cache / "uv", "UV_PYTHON_INSTALL_DIR": root / "python",
        "PIP_CACHE_DIR": cache / "pip", "HF_MODULES_CACHE": cache / "hf-modules",
        "HF_ASSETS_CACHE": cache / "hf-assets", "HF_XET_CACHE": cache / "hf-xet",
        "TORCH_HOME": cache / "torch", "TORCH_EXTENSIONS_DIR": cache / "torch-extensions",
        "TORCHINDUCTOR_CACHE_DIR": cache / "inductor", "TRITON_CACHE_DIR": cache / "triton",
        "CUDA_CACHE_PATH": cache / "cuda", "NUMBA_CACHE_DIR": cache / "numba",
        "MPLCONFIGDIR": cache / "matplotlib",
    }
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    hf_home = Path(os.environ.get("HF_HOME") or (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface")).expanduser()
    environment.update(HF_HOME=str(hf_home), HF_HUB_CACHE=str(model_cache(device)), PYTHONDONTWRITEBYTECODE="1",
                       PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", UV_LINK_MODE="copy")
    # A preconfigured bytecode prefix must not redirect caches outside the job.
    environment.pop("PYTHONPYCACHEPREFIX", None)
    return environment


def prepare_model(request, python, emit, install=True, environment=None):
    namespace = {}
    exec(compile(request["model_source"], "model_cache.py", "exec"), namespace)
    namespace["DOWNLOAD_SOURCE"] = request.get("download_source")
    namespace["DOWNLOAD_ENVIRONMENT"] = environment
    return namespace["ensure_model"](request["device"], python, emit, install)


def prepare_runtime(root, emit, environment=None, pass_fds=()):
    runtime = root / ".venv"
    python = runtime / "bin" / "python"
    emit("installing_uv")
    uv = ensure_uv(root / "tools", environment, pass_fds=pass_fds)
    check = inspect_environment(runtime, root / "runtime-version", RUNTIME_VERSION, root / "tools")
    emit("installing", runtime=check)
    if check["ready"]:
        return python
    if runtime.is_symlink():
        raise RuntimeError("실행 환경 경로를 확인해 주세요.")
    recreated = not check["venv"]["ready"]
    if recreated:
        # Rebuild only this owned venv if its interpreter was deleted or broken.
        subprocess.run([uv, "venv", "--clear", "--python", "3.12", str(runtime)], check=True, env=environment, pass_fds=pass_fds)
        emit("installing", runtime=inspect_environment(runtime, root / "runtime-version", RUNTIME_VERSION, root / "tools"))
    installer = [uv, "pip", "install", "--python", str(python)]
    stamp = root / "runtime-version"
    version = RUNTIME_VERSION
    if recreated or not check["packages"]["ready"]:
        subprocess.run(installer + ["torch==2.8.0+cu128", "torchaudio==2.8.0+cu128", "--index-url", "https://download.pytorch.org/whl/cu128"], check=True, env=environment, pass_fds=pass_fds)
        subprocess.run(installer + [UPSTREAM, "fastapi>=0.116,<1", "uvicorn[standard]>=0.35,<1", "python-multipart", "jinja2"], check=True, env=environment, pass_fds=pass_fds)
        stamp.write_text(version)
    return python


def hardware_info():
    import xml.etree.ElementTree as ET
    try:
        inventory = ET.fromstring(subprocess.check_output(["nvidia-smi", "-q", "-x"], timeout=8))
        gpus = []
        for index, gpu in enumerate(inventory.findall("gpu")):
            total = gpu.findtext("fb_memory_usage/total", "").split()
            memory = round(float(total[0]) / 1024, 1) if total and total[0].replace('.', '', 1).isdigit() else None
            gpus.append(dict(index=index, name=gpu.findtext("product_name", "NVIDIA GPU"), memory_gib=memory))
        return dict(gpus=gpus)
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError):
        return dict(gpus=[])


def prepare_quantization_runtime(root, python, options, emit, environment=None, pass_fds=()):
    if options.get("precision") not in {"int8", "int4"}:
        return
    stamp = root / "quantization-version"
    version = QUANTIZATION_VERSION
    checked = inspect_environment(root / ".venv", root / "runtime-version", RUNTIME_VERSION, root / "tools", precision=options["precision"])
    if stamp.is_file() and stamp.read_text() == version and checked["packages"]["ready"]:
        return
    emit("installing")
    uv = ensure_uv(root / "tools", environment, pass_fds=pass_fds)
    installer = [uv, "pip", "install", "--python", str(python)]
    subprocess.run(installer + ["bitsandbytes==0.48.1", "accelerate>=1,<2"], check=True, env=environment, pass_fds=pass_fds)
    stamp.write_text(version)


def prepare_shared_runtime(shared, device, emit):
    def install(runtime_root, lock_descriptor):
        # Installer caches belong to this unpublished runtime, not a Mac's job
        # directory. Another client's cleanup cannot remove them mid-install.
        environment = temporary_environment(runtime_root, device)
        try:
            python = prepare_runtime(runtime_root, emit, environment, pass_fds=(lock_descriptor,))
            prepare_quantization_runtime(runtime_root, python, device.get("options", {}), emit, environment, pass_fds=(lock_descriptor,))
        finally:
            cleanup_job_files(runtime_root, device, verified=True)
    return shared.ensure(emit, install)


def process_live(process_root):
    if not process_root.exists():
        return False
    try:
        return (process_root / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return process_root.exists()


def stop_owned_worker(root, device, emit):
    verified = False
    pid_file = root / "worker.pid"
    if pid_file.is_file():
        pid = int(pid_file.read_text())
        if pid <= 1:
            raise RuntimeError("Invalid worker PID")
        process_root = PROCESSES / str(pid)
        if process_live(process_root):
            if (process_root / "cwd").resolve() != root.resolve():
                raise RuntimeError("Worker process ownership could not be verified")
            recorded = root / "worker.json"
            gpu_uuid = device.get("gpu_uuid") or (json.loads(recorded.read_text())["gpu_uuid"] if recorded.is_file() else None)
            if not gpu_uuid:
                raise RuntimeError("Worker GPU identity could not be verified")
            expected = {b"CUDA_VISIBLE_DEVICES": gpu_uuid.encode(), b"COSMOSS_DEVICE_ID": device["id"].encode(), b"COSMOSS_WORKER_TOKEN": device["api_key"].encode()}

            def check_identity():
                variables = dict(item.split(b"=", 1) for item in (process_root / "environ").read_bytes().split(b"\0") if b"=" in item)
                if (process_root / "cwd").resolve() != root.resolve() or any(variables.get(key) != value for key, value in expected.items()):
                    raise RuntimeError("Worker process identity could not be verified; no further signal was sent")

            check_identity()
            verified = True
            os.kill(pid, signal.SIGTERM)
            emit("stopping")
            for _ in range(20):
                if not process_live(process_root):
                    break
                time.sleep(0.5)
            if process_live(process_root):
                check_identity()
                os.kill(pid, signal.SIGKILL)
                for _ in range(20):
                    if not process_live(process_root):
                        break
                    time.sleep(0.1)
            if process_live(process_root):
                raise RuntimeError("Worker is still running; its files were not removed")
    cleanup_job_files(root, device, verified=verified)
    emit("stopped", cleaned=True)


def main():
    request = json.load(sys.stdin)
    device = request["device"]
    action = request.get("action", "install")
    device_id = device["id"]
    if len(device_id) != 32 or any(character not in "0123456789abcdef" for character in device_id):
        raise ValueError("Invalid worker ID")
    root = Path.home() / ".local" / "share" / "cosmoss" / "workers" / device_id
    shared = SharedRuntime(RUNTIME_VERSION, device.get("options", {}).get("precision", "auto"),
                           QUANTIZATION_VERSION, probe=inspect_environment)
    port = int(device["remote_port"])
    if not 1024 <= port <= 65535:
        raise ValueError("Use an unprivileged worker port")
    address = "http://127.0.0.1:%d/healthz" % port

    def status():
        try:
            query = urllib.request.Request(address, headers={"Authorization": "Bearer " + device["api_key"]})
            with urllib.request.urlopen(query, timeout=2) as response:
                return json.load(response)
        except (OSError, ValueError):
            return None

    def owns(payload):
        return (isinstance(payload, dict) and payload.get("app") == "cosMOSS CUDA worker"
                and payload.get("device_id") == device_id and payload.get("gpu_uuid") == device.get("gpu_uuid"))

    def emit(phase, **details):
        print("COSMOSS:" + json.dumps({"phase": phase, **details}), flush=True)

    def runtime_status():
        return shared.inspect()

    if action == "environment":
        emit("environment", runtime=runtime_status())
        return

    if action == "options":
        namespace = {}
        exec(compile(request["model_source"], "model_cache.py", "exec"), namespace)
        emit("options", source=device.get("model_source", "cache"),
             cache_dir=str(namespace["cache_candidates"](device)[0]),
             suggested_path=device.get("model_path", ""), model_id=device["model"], hardware=hardware_info())
        return

    if action in {"install", "inspect", "runtime"}:
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10 or newer is required on the selected host")
        if action == "inspect":
            runtime = runtime_status()
            python = Path(runtime["venv"].get("path", root / ".venv")) / "bin/python"
            try:
                details = dict(prepare_model(request, python, emit, install=False), model_ready=True)
            except RuntimeError as error:
                details = {"model_ready": False, "model_error": str(error)}
            active = status()
            if isinstance(active, dict) and active.get("app") == "cosMOSS CUDA worker" and active.get("device_id") == device_id:
                details["active_gpu_uuid"] = active.get("gpu_uuid")
        else:
            pid_file = root / "worker.pid"
            if pid_file.is_file() and process_live(PROCESSES / pid_file.read_text().strip()):
                raise RuntimeError("A worker is already running; finish its job before preparing models")
            if action == "install" and device.get("model_source") == "folder":
                prepare_model(request, root / ".venv/bin/python", emit, install=False)
            claim_root(root, device)
            details = None
            try:
                environment = temporary_environment(root, device)
                python = prepare_shared_runtime(shared, device, emit)
                if action == "runtime":
                    try:
                        details = dict(prepare_model(request, python, emit, install=False), model_ready=True)
                    except RuntimeError as error:
                        details = {"model_ready": False, "model_error": str(error)}
                else:
                    details = dict(prepare_model(request, python, emit, environment=environment), model_ready=True)
                runtime = runtime_status()
                if not runtime["ready"]:
                    emit("installing", runtime=runtime)
                    raise RuntimeError("실행 환경 검증에 실패했습니다. 누락된 패키지를 확인해 주세요.")
            finally:
                cleanup_job_files(root, device, details=details)
        emit("installed" if runtime["ready"] and details.get("model_ready") else "idle", runtime=runtime, **details)
        return

    previous = status() if action == "prepare" else None
    if action == "stop":
        stop_owned_worker(root, device, emit)
        return
    if action != "prepare" or not device.get("gpu_uuid"):
        raise RuntimeError("전사를 실행할 GPU를 선택해 주세요.")
    if (owns(previous) and previous.get("ok") and previous.get("model") == device["model"]
            and previous.get("options", {}) == device.get("options", {})
            and (device.get("model_source") != "folder" or previous.get("model_path") == str(Path(device["model_path"]).expanduser().resolve()))):
        emit("ready")
        return
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("The selected port is already occupied; choose another port")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required on the selected host")

    # Refuse to take over a GPU that is already running someone else's job.
    import xml.etree.ElementTree as ET
    inventory = ET.fromstring(subprocess.check_output(["nvidia-smi", "-q", "-x"], timeout=10))
    selected = next((gpu for gpu in inventory.findall("gpu") if gpu.findtext("uuid") == device["gpu_uuid"]), None)
    if selected is None:
        raise RuntimeError("The selected GPU UUID is no longer present")
    processes = selected.find("processes")
    compute = [] if processes is None else [process for process in processes.findall("process_info") if process.findtext("type", "") != "G"]
    if processes is None or (processes.text or "").strip() not in {"", "None"} or compute:
        raise RuntimeError("The selected GPU has active or unknown processes; choose an idle GPU")

    claim_root(root, device)
    worker = None
    model_details = None
    ready = False
    try:
        environment = temporary_environment(root, device)
        python = prepare_shared_runtime(shared, device, emit)
        model_details = prepare_model(request, python, emit, environment=environment)
        source = root / "src"
        source.mkdir(exist_ok=True)
        (source / "__init__.py").write_text("")
        (source / "cuda_server.py").write_text(request["source"])
        environment.update({
            "CUDA_VISIBLE_DEVICES": device["gpu_uuid"], "COSMOSS_DEVICE_ID": device_id,
            "MOSS_MODEL_NAME": device["model"], "MOSS_MODEL_PATH": model_details["model_path"],
            "HF_HUB_CACHE": model_details["model_cache"], "HF_HUB_OFFLINE": "1",
            "COSMOSS_WORKER_TOKEN": device["api_key"],
            "PYTHONUNBUFFERED": "1", "COSMOSS_MODEL_OPTIONS": json.dumps(device.get("options", {})),
        })
        emit("loading")
        with (root / "worker.log").open("ab") as log:
            worker = subprocess.Popen(
                [str(python), "-m", "uvicorn", "src.cuda_server:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        (root / "worker.pid").write_text(str(worker.pid))
        (root / "worker.json").write_text(json.dumps({"gpu_uuid": device["gpu_uuid"], "model_path": model_details["model_path"]}))
        for _ in range(900):
            if worker.poll() is not None:
                raise RuntimeError("Worker exited: " + (root / "worker.log").read_text(errors="replace")[-1800:])
            if owns(status()):
                emit("ready")
                ready = True
                return
            time.sleep(1)
        raise RuntimeError("Worker model loading timed out")
    finally:
        if not ready:
            # A failed launch may only stop the child created by this invocation.
            if worker is not None and worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
            cleanup_job_files(root, device, details=model_details)



if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("COSMOSS:" + json.dumps({"phase": "error", "error": str(error)}), flush=True)
        sys.exit(1)
