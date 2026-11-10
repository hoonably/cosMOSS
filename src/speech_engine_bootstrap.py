"""SSH-owned sequential engine stages, one UUID lock, one private audio upload."""
from __future__ import annotations
from contextlib import nullcontext
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
import wave

CUDA_COMPILER_VERSION = '13.0.88'
CUDA_COMPILER_PROBE = 'cuda-13.0.88-headers-compile-v1'
CUDA_COMPILER_PACKAGES = ('nvidia-cuda-nvcc', 'nvidia-nvvm', 'nvidia-cuda-crt')


def compiler_ready(folder):
    try:
        return ((folder/'cuda-toolchain-ready').read_text() == CUDA_COMPILER_PROBE
                and (folder/'cuda-toolkit/bin/nvcc').is_file()
                and shutil.which('ninja', path=engine_path(folder, os.environ.get('PATH', ''))) is not None)
    except OSError:
        return False


def engine_path(folder, host_path):
    return os.pathsep.join((str(folder/'cuda-toolkit/bin'), str(folder/'bin'), host_path))


def python_has_headers(python):
    if not python.is_file():
        return False
    code = ("import sys, sysconfig; from pathlib import Path; "
            "sys.exit(not (Path(sysconfig.get_path('include')) / 'Python.h').is_file())")
    try:
        return subprocess.run([str(python), '-I', '-B', '-c', code], timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_compiler(python, toolkit, job, environment, run):
    """Exercise NVVM -> PTX -> ptxas without loading a model or using a GPU."""
    code = ("from importlib.metadata import version; "
            f"assert all(version(p)=={CUDA_COMPILER_VERSION!r} for p in {CUDA_COMPILER_PACKAGES!r}), "
            "'CUDA compiler packages differ; prepare the model again'")
    run([str(python), '-I', '-B', '-c', code])
    source = job/'compiler-check.cu'
    source.write_text(
        '#include <cuda_runtime_api.h>\n'
        'static_assert(CUDART_VERSION / 10 == __CUDACC_VER_MAJOR__ * 100 + __CUDACC_VER_MINOR__, '
        '"CUDA compiler and runtime header versions differ");\n'
        '__global__ void cosmoss_check(float *x) { x[threadIdx.x] += 1.0f; }\n'
    )
    private = dict(environment, CUDA_HOME=str(toolkit), CUDA_PATH=str(toolkit),
                   CUDACXX=str(toolkit/'bin/nvcc'), PATH=str(toolkit/'bin')+':'+environment['PATH'])
    try:
        run([str(toolkit/'bin/nvcc'), '-arch=sm_90a', '-std=c++20', '-c', str(source),
             '-o', str(job/'compiler-check.o')], env=private)
    except RuntimeError as error:
        raise RuntimeError('CUDA 컴파일 도구 호환성 검사에 실패했습니다. 선택한 모델을 다시 준비해 주세요.') from error


def emit(**event):
    print('COSMOSS_SPEECH:'+json.dumps(event,ensure_ascii=False),flush=True)


def main():
    os.umask(0o077)
    incoming = os.fdopen(os.dup(sys.stdin.fileno()),'rb',buffering=0)
    request = json.loads(incoming.readline(1024*1024))
    # Bootstrap/catalog sources come from the local app, never the selected model.
    root = Path.home()/'.local/share/cosmoss/speech/engines-sg019-qwen006-v1'
    if any(p.is_symlink() for p in (root,root.parent,root/'envs',root/'python')):
        raise ValueError('Unsafe runtime path')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='job-',dir=root) as directory:
        job = Path(directory)
        for name,field in [('speech_engines.py','engine_source'),('speech_pipeline.py','catalog_source'),
                           ('worker.py','engine_worker_source'),('runtime_environment.py','runtime_source')]:
            (job/name).write_text(request[field])
        sys.path.insert(0,str(job))
        from speech_engines import CUDA_OOM_MESSAGE,MOSS_CAPACITY_MESSAGE,ENGINE_PIPELINES,ENGINE_VERSION,REQUIREMENTS,MODELS,stages,model_keys,missing_moss_partition_tail,run_moss_with_memory_retries
        from speech_pipeline import attribute_speakers,utterances
        from runtime_environment import ensure_uv
        key,action = request['model_key'],request['action']
        if key not in ENGINE_PIPELINES or action not in {'inspect','install','run'}:
            raise ValueError('Invalid engine request')
        manifest = root/(key+'.json')
        def cached_model_paths():
            try:
                saved = json.loads(manifest.read_text())
                paths = saved['paths']
                if saved['version'] == ENGINE_VERSION and all(
                    (Path(paths[model])/'config.json').is_file()
                    and any(Path(paths[model]).glob('*.safetensors'))
                    for model in model_keys(key)
                ):
                    return paths
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return None

        def installed():
            try:
                return (cached_model_paths() is not None and all(
                    (root/'envs'/env/'bin/python').is_file() and (root/'envs'/env/'ready').read_text()==ENGINE_VERSION
                    for _,env in stages(key)) and (all(env!='sglang' for _,env in stages(key))
                    or compiler_ready(root/'envs/sglang')))
            except OSError:
                return False
        if action=='inspect':
            emit(phase='installed' if installed() else 'idle',model_key=key)
            return
        size = request.get('audio_bytes',0)
        if type(size)!=int or not 0<=size<=600*1024**2:
            raise ValueError('정규화된 음성이 600 MiB를 초과합니다.')
        if action=='run':
            if type(request.get('batch'))!=int or not 1<=request['batch']<=24:
                raise ValueError('동시 청크 수를 다시 선택해 주세요.')
            with (job/'audio.wav').open('wb') as stream:
                remaining = size
                while remaining:
                    block = incoming.read(min(256*1024,remaining))
                    if not block:
                        raise RuntimeError('Audio transfer interrupted')
                    stream.write(block)
                    remaining -= len(block)
        disconnected,heartbeat = threading.Event(),[time.monotonic()]
        def listen():
            try:
                while incoming.read(4096):
                    heartbeat[0] = time.monotonic()
            finally:
                disconnected.set()
        threading.Thread(target=listen,daemon=True).start()
        def check():
            if disconnected.is_set() or time.monotonic()-heartbeat[0]>60:
                raise RuntimeError('앱 연결이 끊겨 GPU 작업을 종료했습니다.')
        environment = {**os.environ,'CUDA_VISIBLE_DEVICES':'','OMP_NUM_THREADS':'4','TOKENIZERS_PARALLELISM':'false',
            'HF_HUB_DISABLE_PROGRESS_BARS':'1','HF_HUB_DISABLE_TELEMETRY':'1','TQDM_DISABLE':'1',
            'PYTHONDONTWRITEBYTECODE':'1','COSMOSS_WORKER_PARENT_PID':str(os.getpid()),'PYTHONUNBUFFERED':'1'}
        peak_mib,stage_peaks = 0,{}
        def run(args,*,env=None,stage=None):
            nonlocal peak_mib
            child = subprocess.Popen(args,cwd=job,env=env or environment,start_new_session=True)
            peak,last = 0,0
            try:
                # WNOWAIT keeps the group leader's PID reserved until all of its
                # children have been signalled, avoiding a PID-reuse cleanup race.
                while os.waitid(os.P_PID,child.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT) is None:
                    check()
                    if stage and time.monotonic()-last>=.5:
                        last=time.monotonic()
                        try:
                            output=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid,used_memory,gpu_uuid',
                                '--format=csv,noheader,nounits'],text=True,timeout=5)
                        except (OSError, subprocess.SubprocessError):
                            # Telemetry must not terminate a working model.
                            output=''
                        used=0
                        for line in output.splitlines():
                            pid,memory,gpu=[x.strip() for x in line.split(',')]
                            try:
                                owned=os.getpgid(int(pid))==child.pid
                            except ProcessLookupError:
                                continue
                            if owned and gpu==request['gpu_uuid']:
                                used+=int(memory)
                        peak=max(peak,used)
                        peak_mib=max(peak_mib,peak)
                    time.sleep(.25)
            finally:
                try:
                    os.killpg(child.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(.2)
                try:
                    os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code=child.wait()
                # Releasing a process group is synchronous before the next
                # stage acquires CUDA, including SGLang's stage grandchildren.
                deadline=time.monotonic()+15
                while stage:
                    output=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits'],text=True,timeout=5)
                    alive=[]
                    for line in output.splitlines():
                        try:
                            if os.getpgid(int(line.strip()))==child.pid: alive.append(line)
                        except ProcessLookupError:
                            pass
                    if not alive:
                        break
                    if time.monotonic()>deadline:
                        raise RuntimeError('이전 모델의 GPU 메모리 해제를 확인하지 못해 다음 단계를 시작하지 않았습니다.')
                    time.sleep(.25)
            if stage:
                # Continuation/recovery can launch the same stage again. Keep
                # its highest peak, including failed attempts, for diagnostics.
                stage_peaks[stage]=max(stage_peaks.get(stage,0),peak)
            if code:
                error_path=job/(stage+'.error.json') if stage else None
                if error_path and error_path.exists():
                    error_info=json.loads(error_path.read_text())
                    if error_info.get('cuda_oom'):raise RuntimeError(CUDA_OOM_MESSAGE)
                    if error_info.get('context_capacity'):raise RuntimeError(MOSS_CAPACITY_MESSAGE)
                raise RuntimeError('전사 엔진 단계가 실패했습니다. 위의 모델 오류를 확인해 주세요.')
            return peak
        if action=='install':
            lock=(root/'install.lock').open('a')
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    check()
                    time.sleep(.25)
            uv=None
            environment['UV_PYTHON_INSTALL_DIR'] = str(root/'python')
            for env in dict.fromkeys(env for _,env in stages(key)):
                folder=root/'envs'/env
                if folder.is_symlink():
                    raise ValueError('Unsafe engine environment')
                python=folder/'bin/python'
                ready_stamp = folder/'ready'
                if (not ready_stamp.is_file() or ready_stamp.read_text()!=ENGINE_VERSION or not python.is_file()
                        or env=='sglang' and not compiler_ready(folder)):
                    ready_stamp.unlink(missing_ok=True)
                    if env=='sglang': (folder/'cuda-toolchain-ready').unlink(missing_ok=True)
                    emit(phase='installing',engine=env)
                    if uv is None:
                        uv=ensure_uv(root/'tools',environment)
                    if not python_has_headers(python):
                        # Some dependencies build C extensions even on a clean install.
                        # Ubuntu's system interpreter can be missing the development headers.
                        run([uv,'venv','--clear','--managed-python','--python','3.12',str(folder)])
                    args=[uv,'pip','install','--python',str(python),'--prerelease=allow',*REQUIREMENTS[env]]
                    if env=='nemotron':
                        args+=['--extra-index-url','https://download.pytorch.org/whl/cu128','--index-strategy','unsafe-best-match']
                    run(args)
                    if env=='sglang':
                        # Use the environment's CUDA13 compiler; never alter the
                        # server's system CUDA or another application's toolkit.
                        code="import site; from pathlib import Path; print(next(str(Path(p)/'nvidia/cu13') for p in site.getsitepackages() if (Path(p)/'nvidia/cu13/bin/nvcc').is_file()))"
                        cu=Path(subprocess.check_output([str(python),'-c',code],text=True).strip())
                        toolkit=folder/'cuda-toolkit'
                        (toolkit/'lib64').mkdir(parents=True,exist_ok=True)
                        for name in ('bin','include','nvvm'):
                            target=toolkit/name
                            if not target.exists(): target.symlink_to(cu/name,target_is_directory=True)
                        for source in (cu/'lib').iterdir():
                            target=toolkit/'lib64'/source.name
                            if not target.exists(): target.symlink_to(source)
                        for name,source in [('libcudart.so','libcudart.so.13'),('libnvrtc.so','libnvrtc.so.13'),('libnvJitLink.so','libnvJitLink.so.13')]:
                            target=toolkit/'lib64'/name
                            if not target.exists(): target.symlink_to(cu/'lib'/source)
                        check_compiler(python, toolkit, job, environment, run)
                        (folder/'cuda-toolchain-ready').write_text(CUDA_COMPILER_PROBE)
                    (folder/'ready').write_text(ENGINE_VERSION)
            if cached_model_paths() is None:
                python=root/'envs/nemotron/bin/python'
                config=dict(stage='prepare',models=model_keys(key),output=str(job/'paths.json'))
                (job/'request.json').write_text(json.dumps(config))
                run([str(python),'-u',str(job/'worker.py'),str(job/'request.json')])
                paths=json.loads((job/'paths.json').read_text())
                manifest.write_text(json.dumps(dict(version=ENGINE_VERSION,paths=paths)))
            if not installed():
                raise RuntimeError('모델 설치 파일을 확인하지 못했습니다.')
            emit(phase='installed',model_key=key)
            return
        if not installed():
            raise RuntimeError('왼쪽 처리 장치에서 선택한 모델을 먼저 다운로드해 주세요.')
        if any(env=='sglang' for _,env in stages(key)):
            check_compiler(root/'envs/sglang/bin/python', root/'envs/sglang/cuda-toolkit', job, environment, run)
        gpu=str(request.get('gpu_uuid',''))
        if not gpu.startswith('GPU-') or any(c not in 'GPU-0123456789abcdefABCDEF' for c in gpu):
            raise ValueError('GPU 번호를 다시 선택해 주세요.')
        locks=root.parent.parent/'gpu-locks'
        locks.mkdir(exist_ok=True)
        gpu_lock=(locks/(gpu+'.lock')).open('a')
        try:
            fcntl.flock(gpu_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('다른 cosMOSS 작업이 이 GPU를 사용 중입니다.') from None
        capacity_mib=int(subprocess.check_output(['nvidia-smi','-i',gpu,'--query-gpu=memory.total','--format=csv,noheader,nounits'],text=True,timeout=5).strip())
        gpu_name=subprocess.check_output(['nvidia-smi','-i',gpu,'--query-gpu=name','--format=csv,noheader'],text=True,timeout=5).strip()
        with wave.open(str(job/'audio.wav'),'rb') as wav:
            duration=wav.getnframes()/wav.getframerate()
        paths=json.loads(manifest.read_text())['paths']
        environment.update(CUDA_VISIBLE_DEVICES=gpu,HF_HUB_OFFLINE='1')
        results={}
        started=time.monotonic()
        def execute_stage(stage, env, **overrides):
            check()
            emit(phase='loading',engine=stage,resume_offset=overrides.get('start_offset',0),
                 memory_retry_count=overrides.get('memory_retry_count',0))
            config={k:request.get(k) for k in ('batch','model_key','language','hotwords','prompt','allow_moss_split')}
            config.update(stage=stage,paths=paths,audio=str(job/'audio.wav'),duration=duration,output=str(job/(stage+'.json')),
                qwen_result=str(job/'qwen.json'))
            config.update(overrides)
            for suffix in ('.json','.error.json','.checkpoint.json'):
                (job/(stage+suffix)).unlink(missing_ok=True)
            stage_env=environment.copy()
            if env=='sglang':
                folder = root/'envs/sglang'
                toolkit = folder/'cuda-toolkit'
                stage_env.update(CUDA_HOME=str(toolkit), CUDA_PATH=str(toolkit), PATH=engine_path(folder, environment['PATH']))
                stage_env.setdefault('MAX_JOBS', '8')
            # Keep UNIX socket paths short and give each worker a private directory.
            ipc = tempfile.TemporaryDirectory(prefix='cm-ipc-', dir='/tmp') if env=='sglang' else nullcontext()
            with ipc as ipc_path:
                if ipc_path is not None:
                    config['ipc_path'] = ipc_path
                    # SGLang also creates its GPU startup lock under TMPDIR.
                    stage_env['TMPDIR'] = ipc_path
                (job/'request.json').write_text(json.dumps(config))
                run([str(root/'envs'/env/'bin/python'),'-u',str(job/'worker.py'),str(job/'request.json')],env=stage_env,stage=stage)
            return json.loads((job/(stage+'.json')).read_text())
        moss_chunk_seconds=request.get('moss_chunk_seconds') or 5400
        def execute_moss(start_offset=0,end_offset=None):
            nonlocal moss_chunk_seconds
            def checkpoint():
                path=job/'moss.checkpoint.json'
                return json.loads(path.read_text()) if path.exists() else None
            value=run_moss_with_memory_retries(duration,
                lambda **options:execute_stage('moss','sglang',**options),checkpoint,
                lambda **event:emit(phase='retrying_memory',engine='moss-sglang',**event),
                max_seconds=moss_chunk_seconds,start_offset=start_offset,end_offset=end_offset)
            moss_chunk_seconds=value['moss_chunk_seconds']
            return value
        for stage,env in stages(key):
            results[stage]=execute_moss() if stage=='moss' else execute_stage(stage,env)
        turns=results['nemotron']['turns']
        if 'moss' in results:
            # Natural EOS can also stop early with syntactically valid markup.
            # Whole-recording diarization provides independent evidence of a
            # spoken tail, so complete that tail before publishing the note.
            for _ in range(2*results['moss'].get('partition_count',1)):
                missing=missing_moss_partition_tail(results['moss']['items'],turns,duration,results['moss'].get('ranges'))
                if missing is None:
                    break
                offset,end=missing
                recovered=execute_moss(start_offset=offset,end_offset=end)
                if max((row['end'] for row in recovered['items']),default=offset)<=offset+1:
                    raise RuntimeError('MOSS가 녹음 뒤쪽 발화를 전사하지 못했습니다. 불완전한 결과를 완료로 저장하지 않았습니다.')
                results['moss']['items'].extend(recovered['items'])
                results['moss']['chunks'].extend(recovered['chunks'])
                for metric in ('load_seconds','inference_seconds'):
                    results['moss'][metric]+=recovered[metric]
                results['moss']['adaptive_memory_retries'].extend(recovered.get('adaptive_memory_retries',[]))
            results['moss']['items'].sort(key=lambda row:row['start'])
            if missing_moss_partition_tail(results['moss']['items'],turns,duration,results['moss'].get('ranges')) is not None:
                raise RuntimeError('MOSS 전사에 녹음 뒤쪽 발화가 누락되어 있습니다. 불완전한 결과를 완료로 저장하지 않았습니다.')
        items=results['aligner']['items'] if key=='qwen-transformers' else results['moss']['items']
        attributed=attribute_speakers(items,turns)
        for row in attributed:
            if row.get('unaligned'): row.update(speaker='미확인',speaker_uncertain=True)
        segments=utterances(attributed) if key=='qwen-transformers' else [dict(row,id=i) for i,row in enumerate(attributed)]
        chunks=results['qwen' if key=='qwen-transformers' else 'moss']['chunks']
        elapsed=time.monotonic()-started
        inference_seconds=sum(r['inference_seconds'] for r in results.values())
        result=dict(segments=segments,words=attributed if key=='qwen-transformers' else [],turns=turns,asr_chunks=chunks,
            pipeline=key,runtime=ENGINE_VERSION,batch=request['batch'],chunk_count=len(chunks),audio_seconds=duration,
            load_seconds=max(0,elapsed-inference_seconds),
            inference_seconds=inference_seconds,total_seconds=elapsed,
            stage_metrics={name:{k:v for k,v in result.items() if k in {'load_seconds','inference_seconds','batch_history','adaptive_retries'}} for name,result in results.items()},
            peak_process_mib=peak_mib,memory_phases=stage_peaks,gpu_name=gpu_name,capacity_gib=capacity_mib/1024,
            uncertain_segments=sum(bool(r.get('speaker_uncertain')) for r in segments),precision='bf16',
            model_revisions={k:MODELS[k][1] for k in model_keys(key)},schedule='sequential-unload',
            auxiliary_transcript=results['qwen'] if key=='moss-qwen-sequential' else None,
            has_auxiliary_transcript=key=='moss-qwen-sequential',
            moss_partition_count=results.get('moss',{}).get('partition_count',1),moss_chunk_seconds=moss_chunk_seconds,
            adaptive_memory_retries=results.get('moss',{}).get('adaptive_memory_retries',[]))
        emit(phase='done',result=result)


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        emit(phase='error',error=str(error)[-1800:])
        sys.exit(1)
