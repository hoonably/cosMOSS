"""One SSH lease owns one finite speech pipeline child and its private audio copy."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

VERSION = 'speech-torch280-transformers5202-moss-e3bdd93f-v1'


def emit(**event):
    print('COSMOSS_SPEECH:' + json.dumps(event, ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    incoming = os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0)
    request = json.loads(incoming.readline(1024 * 1024))
    if request['action'] not in {'inspect', 'install', 'run'} or request['model_key'] not in {'moss-batch', 'qwen-batch'}:
        raise ValueError('Invalid speech pipeline request')
    root = Path.home() / '.local/share/cosmoss/speech' / VERSION
    if any(path.is_symlink() for path in (root, root.parent, root / 'runtime')):
        raise ValueError('Unsafe runtime path')
    python = root / 'runtime/bin/python'
    cache = Path(os.environ.get('HF_HUB_CACHE') or Path(os.environ.get('HF_HOME') or Path.home() / '.cache/huggingface') / 'hub')
    models = {
        'moss': ('OpenMOSS-Team--MOSS-Transcribe-Diarize', '704aa4a9c304e8520be88901e0d1960158ef5b15', ['config.json', 'processor_config.json', 'model-00000-of-00001.safetensors']),
        'qwen': ('Qwen--Qwen3-ASR-1.7B-hf', 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3', ['config.json', 'model.safetensors']),
        'aligner': ('Qwen--Qwen3-ForcedAligner-0.6B-hf', 'c07281df297b9905d24a508279258cccf987a064', ['config.json', 'model.safetensors']),
        'nemotron': ('nvidia--Nemotron-3-Diarization', 'f667ed73aee57d40cc39428eb768b4fd87a0a29e', ['config.json', 'processor_config.json', 'model.safetensors']),
    }
    keys = ('moss', 'nemotron') if request['model_key'] == 'moss-batch' else ('qwen', 'aligner', 'nemotron')
    def cached():
        return all((cache / ('models--' + models[key][0]) / 'snapshots' / models[key][1] / name).is_file()
                   for key in keys for name in models[key][2])
    if request['action'] == 'inspect':
        stamp = root / (request['model_key'] + '.json')
        emit(phase='installed' if python.is_file() and stamp.is_file() and cached() else 'idle', model_key=request['model_key'])
        return
    if request['action'] == 'run' and (type(request.get('batch')) is not int or not 1 <= request['batch'] <= 24):
        raise ValueError('동시 청크 수를 다시 선택해 주세요.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='job-', dir=root) as directory:
        job = Path(directory)
        size = request.get('audio_bytes', 0)
        if not isinstance(size, int) or not 0 <= size <= 600 * 1024**2:
            raise ValueError('정규화된 음성이 600 MiB를 초과합니다. 녹음을 나누어 올려 주세요.')
        if request['action'] == 'run':
            with (job / 'audio.wav').open('wb') as output:
                remaining = size
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise RuntimeError('Audio transfer interrupted')
                    output.write(block)
                    remaining -= len(block)
        disconnected, heartbeat = threading.Event(), [time.monotonic()]
        def listen():
            try:
                while incoming.read(4096):
                    heartbeat[0] = time.monotonic()
            finally:
                disconnected.set()
        threading.Thread(target=listen, daemon=True).start()
        def check():
            if disconnected.is_set() or time.monotonic() - heartbeat[0] > 60:
                raise RuntimeError('Speech pipeline lease disconnected')
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                       'HF_HUB_DISABLE_PROGRESS_BARS': '1', 'TQDM_DISABLE': '1',
                       'PYANNOTE_METRICS_ENABLED': '0', 'OMP_NUM_THREADS': '4'}
        def run(args, measure_gpu=False, **kwargs):
            child = subprocess.Popen(args, start_new_session=True, env=environment, **kwargs)
            peak_memory, sampled = 0, 0.0
            try:
                while child.poll() is None:
                    check()
                    if measure_gpu and time.monotonic() - sampled >= .5:
                        sampled = time.monotonic()
                        try:
                            output = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid,used_memory', '--format=csv,noheader,nounits'], timeout=3, text=True)
                            for line in output.splitlines():
                                pid, memory = line.split(',')
                                if int(pid) == child.pid: peak_memory = max(peak_memory, int(memory))
                        except (OSError, ValueError, subprocess.SubprocessError):
                            pass
                    time.sleep(.25)
                if child.returncode:
                    raise RuntimeError('Speech pipeline subprocess failed')
                return peak_memory
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
        if request['action'] == 'install':
            with (root / 'install.lock').open('a') as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        check()
                        time.sleep(.25)
                if not (root / 'runtime-ready').is_file() or not python.is_file():
                    emit(phase='installing')
                    (job / 'runtime_environment.py').write_text(request['runtime_source'])
                    sys.path.insert(0, str(job))
                    from runtime_environment import ensure_uv
                    uv = ensure_uv(root / 'tools', environment)
                    if not python.is_file():
                        run([uv, 'venv', '--python', '3.12', str(root / 'runtime')])
                    run([uv, 'pip', 'install', '--python', str(python), 'torch==2.8.0+cu128', 'torchaudio==2.8.0+cu128',
                         '--index-url', 'https://download.pytorch.org/whl/cu128'])
                    run([uv, 'pip', 'install', '--python', str(python), 'torch==2.8.0+cu128', 'torchaudio==2.8.0+cu128',
                         'transformers==5.20.2', 'numpy==2.2.6', 'soundfile==0.13.1', 'librosa==0.11.0',
                         'accelerate==1.11.0', 'soynlp==0.0.493', 'jinja2>=3.1,<4'])
                    run([uv, 'pip', 'install', '--python', str(python), '--no-deps',
                         'https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/archive/e3bdd93f8ad58418e04659818efd5fd87a0bc486.tar.gz'])
                    (root / 'runtime-ready').write_text(VERSION)
        if not python.is_file():
            raise RuntimeError('먼저 선택한 모델의 준비하기를 눌러 주세요.')
        for name, source in [('worker.py', request['worker_source']), ('speech_pipeline.py', request['catalog_source']), ('speech_engines.py', request['engine_source']), ('process_lifecycle.py', request['lifecycle_source'])]:
            (job / name).write_text(source)
        config = {key: request[key] for key in ('action', 'model_key', 'batch', 'language', 'hotwords', 'prompt') if key in request}
        config.update(audio=str(job / 'audio.wav'), result=str(job / 'result.json'))
        (job / 'request.json').write_text(json.dumps(config))
        if request['action'] == 'run':
            # UUID binding prevents GPU index changes from selecting new hardware.
            gpu = str(request.get('gpu_uuid', ''))
            if not gpu.startswith('GPU-') or any(c not in 'GPU-0123456789abcdefABCDEF' for c in gpu):
                raise ValueError('GPU 번호를 다시 선택해 주세요.')
            locks = Path.home() / '.local/share/cosmoss/gpu-locks'
            locks.mkdir(parents=True, exist_ok=True)
            gpu_lock = (locks / (gpu + '.lock')).open('a')
            try:
                fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('다른 cosMOSS 작업이 이 GPU를 사용 중입니다. 다른 GPU를 선택해 주세요.')
            environment['CUDA_VISIBLE_DEVICES'] = gpu
            environment['HF_HUB_OFFLINE'] = '1'
        else:
            environment['CUDA_VISIBLE_DEVICES'] = ''
        environment['COSMOSS_WORKER_PARENT_PID'] = str(os.getpid())
        peak_process_mib = run([str(python), '-u', '-B', str(job / 'worker.py'), str(job / 'request.json')],
                               cwd=job, measure_gpu=request['action'] == 'run')
        if (job / 'result.json').is_file():
            result = json.loads((job / 'result.json').read_text())
            result['peak_process_mib'] = peak_process_mib or None
            emit(phase='done', result=result)
        elif request['action'] == 'install':
            # Publish readiness only after the pinned files are present.
            if cached():
                (root / (request['model_key'] + '.json')).write_text(json.dumps({'version': VERSION}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit(phase='error', error=str(error)[-1200:])
        sys.exit(1)
