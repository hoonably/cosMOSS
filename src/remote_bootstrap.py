"""Sent to a selected SSH host's python3; no shell interpolation or root access."""

import json
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


UPSTREAM = "https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/archive/e3bdd93f8ad58418e04659818efd5fd87a0bc486.tar.gz"
PROCESSES = Path("/proc")


def main():
    request = json.load(sys.stdin)
    device = request["device"]
    device_id = device["id"]
    if len(device_id) != 32 or any(character not in "0123456789abcdef" for character in device_id):
        raise ValueError("Invalid worker ID")
    root = Path.home() / ".local" / "share" / "cosmoss" / "workers" / device_id
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
                and payload.get("device_id") == device_id and payload.get("gpu_uuid") == device["gpu_uuid"])

    def emit(phase):
        print("COSMOSS:" + json.dumps({"phase": phase}), flush=True)

    previous = status()
    if request.get("action") == "stop":
        pid_file = root / "worker.pid"
        if not pid_file.exists():
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", port)) != 0:
                    emit("stopped")
                    return
            raise RuntimeError("No matching cosMOSS worker is running; no process was stopped")
        pid = int(pid_file.read_text())
        process_root = PROCESSES / str(pid)
        if not process_root.exists():
            emit("stopped")
            return
        if (process_root / "cwd").resolve() != root.resolve():
            raise RuntimeError("Worker process ownership could not be verified")
        variables = dict(item.split(b"=", 1) for item in (process_root / "environ").read_bytes().split(b"\0") if b"=" in item)
        expected = {b"CUDA_VISIBLE_DEVICES": device["gpu_uuid"].encode(), b"COSMOSS_DEVICE_ID": device_id.encode(), b"COSMOSS_WORKER_TOKEN": device["api_key"].encode()}
        if any(variables.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Worker process identity could not be verified")
        os.kill(pid, signal.SIGTERM)
        emit("stopping")
        for _ in range(20):
            if not process_root.exists():
                emit("stopped")
                return
            time.sleep(0.5)
        if (process_root / "cwd").resolve() == root.resolve():
            os.kill(pid, signal.SIGKILL)
        emit("stopped")
        return
    if owns(previous) and previous.get("ok") and previous.get("model") == device["model"]:
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

    root.mkdir(parents=True, exist_ok=True)
    source = root / "src"
    source.mkdir(exist_ok=True)
    (source / "__init__.py").write_text("")
    (source / "cuda_server.py").write_text(request["source"])
    runtime = root / ".venv"
    python = runtime / "bin" / "python"
    uv = shutil.which("uv")
    if not uv and (Path.home() / ".local" / "bin" / "uv").is_file():
        uv = str(Path.home() / ".local" / "bin" / "uv")
    emit("installing")
    if not python.exists():
        if uv:
            subprocess.run([uv, "venv", "--python", "3.12", str(runtime)], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
    installer = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
    stamp = root / "runtime-version"
    version = "cuda128-torch280-moss-e3bdd93f-v1"
    if not stamp.exists() or stamp.read_text() != version:
        subprocess.run(installer + ["torch==2.8.0", "torchaudio==2.8.0", "--index-url", "https://download.pytorch.org/whl/cu128"], check=True)
        subprocess.run(installer + [UPSTREAM], check=True)
        stamp.write_text(version)

    environment = dict(os.environ)
    environment.update({
        "CUDA_VISIBLE_DEVICES": device["gpu_uuid"], "COSMOSS_DEVICE_ID": device_id,
        "MOSS_MODEL_NAME": device["model"], "HF_HOME": str(root.parent.parent / "models-cache"),
        "COSMOSS_WORKER_TOKEN": device["api_key"],
        "PYTHONUNBUFFERED": "1",
    })
    emit("loading")
    with (root / "worker.log").open("ab") as log:
        worker = subprocess.Popen(
            [str(python), "-m", "uvicorn", "src.cuda_server:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
            cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    (root / "worker.pid").write_text(str(worker.pid))
    for _ in range(900):
        if worker.poll() is not None:
            raise RuntimeError("Worker exited: " + (root / "worker.log").read_text(errors="replace")[-1800:])
        if owns(status()):
            emit("ready")
            return
        time.sleep(1)
    # Only terminate the child we just created, never an existing GPU process.
    worker.terminate()
    raise RuntimeError("Worker model loading timed out; inspect " + str(root / "worker.log"))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("COSMOSS:" + json.dumps({"phase": "error", "error": str(error)}), flush=True)
        sys.exit(1)
