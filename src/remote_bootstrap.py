"""Prepare device models without a GPU, then launch a GPU only for a job."""

import json
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
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
JOB_FILES = ("src", "tmp", "cache", "__pycache__", "worker.log", "worker.pid", "worker.json")
LEASE_SECONDS = 60
WORKER_STOP_GRACE_SECONDS = 10


class WorkerLease:
    """Read the app's private SSH stream independently of model startup/work."""

    def __init__(self, stream):
        self.last_seen = time.monotonic()
        self.connected = True

        def receive():
            try:
                for line in stream:
                    if line.strip() != "PING":
                        break
                    self.last_seen = time.monotonic()
            finally:
                self.connected = False

        threading.Thread(target=receive, name="cosmoss-worker-lease", daemon=True).start()

    def check(self):
        if not self.connected or time.monotonic() - self.last_seen >= LEASE_SECONDS:
            raise RuntimeError("앱과의 연결이 끊겨 GPU 작업을 종료했습니다.")


def job_cleanup_pending(root):
    return any((root / name).exists() or (root / name).is_symlink() for name in JOB_FILES)


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


def prepare_shared_runtime(shared, device, emit):
    def install(runtime_root, lock_descriptor):
        # Installer caches belong to this unpublished runtime, not a Mac's job
        # directory. Another client's cleanup cannot remove them mid-install.
        environment = temporary_environment(runtime_root, device)
        try:
            prepare_runtime(runtime_root, emit, environment, pass_fds=(lock_descriptor,))
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
    # One framed request, followed by PING lines for a GPU session. Other
    # actions still finish immediately and can use an EOF-terminated request.
    request = json.loads(sys.stdin.readline())
    device = request["device"]
    action = request.get("action", "install")
    device_id = device["id"]
    if len(device_id) != 32 or any(character not in "0123456789abcdef" for character in device_id):
        raise ValueError("Invalid worker ID")
    root = Path.home() / ".local" / "share" / "cosmoss" / "workers" / device_id
    shared = SharedRuntime(RUNTIME_VERSION, probe=inspect_environment)
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
        if action == 'install':
            emit("checking", hardware=hardware_info())
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
            details["active_gpu_uuid"] = None
            # A crashed worker can leave private job files without a live PID.
            details["worker_cleanup_pending"] = job_cleanup_pending(root)
            if isinstance(active, dict) and active.get("app") == "cosMOSS CUDA worker" and active.get("device_id") == device_id:
                details["active_gpu_uuid"] = active.get("gpu_uuid")
                details["worker_cleanup_pending"] = True
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

    if action == "stop":
        stop_owned_worker(root, device, emit)
        return
    if action != "prepare" or not device.get("gpu_uuid"):
        raise RuntimeError("전사를 실행할 GPU를 선택해 주세요.")
    if request.get("lease_protocol") != 1:
        raise RuntimeError("안전한 GPU 종료를 위해 cosMOSS 앱을 다시 실행해 주세요.")
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("The selected port is already occupied; choose another port")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required on the selected host")

    # Busy/unknown GPUs require explicit confirmation in this job's snapshot.
    # Other processes are never stopped; the worker only shares the selected GPU.
    import xml.etree.ElementTree as ET
    inventory = ET.fromstring(subprocess.check_output(["nvidia-smi", "-q", "-x"], timeout=10))
    selected = next((gpu for gpu in inventory.findall("gpu") if gpu.findtext("uuid") == device["gpu_uuid"]), None)
    if selected is None:
        raise RuntimeError("The selected GPU UUID is no longer present")
    processes = selected.find("processes")
    compute = [] if processes is None else [process for process in processes.findall("process_info") if process.findtext("type", "") != "G"]
    if (processes is None or (processes.text or "").strip() not in {"", "None"} or compute) and device.get("allow_busy_gpu") is not True:
        raise RuntimeError("The selected GPU has active or unknown processes; choose an idle GPU")

    claim_root(root, device)
    import fcntl
    session_lock = (root / "worker.lock").open("a")
    try:
        fcntl.flock(session_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        session_lock.close()
        raise RuntimeError("이 장치의 이전 GPU 작업을 정리 중입니다. 잠시 후 다시 시도해 주세요.")
    lease = WorkerLease(sys.stdin)
    worker = None
    model_details = None
    try:
        lease.check()
        environment = temporary_environment(root, device)
        python = prepare_shared_runtime(shared, device, emit)
        model_details = prepare_model(request, python, emit, install=False, environment=environment)
        lease.check()  # Never acquire a GPU after its app has disappeared.
        source = root / "src"
        source.mkdir(exist_ok=True)
        (source / "__init__.py").write_text("")
        (source / "cuda_server.py").write_text(request["source"])
        (source / "process_lifecycle.py").write_text(request["lifecycle_source"])
        environment.update({
            "CUDA_VISIBLE_DEVICES": device["gpu_uuid"], "COSMOSS_DEVICE_ID": device_id,
            "MOSS_MODEL_NAME": device["model"], "MOSS_MODEL_PATH": model_details["model_path"],
            "HF_HUB_CACHE": model_details["model_cache"], "HF_HUB_OFFLINE": "1",
            "COSMOSS_WORKER_TOKEN": device["api_key"],
            "COSMOSS_WORKER_PARENT_PID": str(os.getpid()),
            "PYTHONUNBUFFERED": "1", "COSMOSS_MODEL_OPTIONS": json.dumps(device.get("options", {})),
        })
        emit("loading", active_gpu_uuid=device["gpu_uuid"], worker_cleanup_pending=True)
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
            lease.check()
            if owns(status()):
                emit("ready")
                break
            time.sleep(1)
        else:
            raise RuntimeError("Worker model loading timed out")
        # Retain the SSH supervisor while the Mac uploads, transcribes and
        # collects results. EOF or a silent network partition revokes the lease.
        while worker.poll() is None:
            lease.check()
            time.sleep(0.5)
        raise RuntimeError("GPU worker exited before the app released its session")
    finally:
        try:
            # Only the direct child created by this supervisor is signalled;
            # Popen.poll/wait reap it before files are removed or a PID reused.
            if worker is not None and worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=WORKER_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
            cleanup_job_files(root, device, details=model_details)
        finally:
            session_lock.close()



if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("COSMOSS:" + json.dumps({"phase": "error", "error": str(error)}), flush=True)
        sys.exit(1)
