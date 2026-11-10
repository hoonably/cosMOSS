"""One disposable engine stage. Exit releases its model before the next stage."""
from __future__ import annotations

import dataclasses
import gc
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import wave

from speech_engines import CUDA_OOM_MESSAGE, MOSS_CAPACITY_MESSAGE, MODELS, balanced_moss_ranges, is_cuda_oom, is_moss_capacity_error, transcribe_moss_balanced, transcribe_qwen_adaptively


def emit(**event):
    print('COSMOSS_SPEECH:' + json.dumps(event, ensure_ascii=False), flush=True)


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False))


def guard():
    """A dead SSH supervisor kills this entire stage, including SGLang children."""
    import ctypes
    parent = int(os.environ['COSMOSS_WORKER_PARENT_PID'])
    def stop(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, stop)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot arm stage parent guard')
    if os.getppid() != parent:
        stop()


def force_fa2(config):
    config._attn_implementation = 'flash_attention_2'
    for value in vars(config).values():
        if hasattr(value, 'to_dict') and hasattr(value, '_attn_implementation'):
            force_fa2(value)


def get_audio(path):
    import numpy as np
    with wave.open(str(path), 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Expected mono16k PCM16 audio')
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768


def prepare(request):
    from huggingface_hub import snapshot_download
    paths = {}
    for key in request['models']:
        emit(phase='downloading', model=key)
        paths[key] = snapshot_download(MODELS[key][0], revision=MODELS[key][1],
            allow_patterns=['*.json', '*.safetensors', '*.py', '*.txt', '*.jinja', '*.model'])
    save(request['output'], paths)


def moss(request):
    import httpx
    duration = request['duration']
    if duration > 90 * 60 and not request.get('allow_moss_split'):
        raise ValueError('90분을 넘는 MOSS 녹음은 균등 분할 확인이 필요합니다.')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = [str(Path(sys.executable).parent / 'sgl-omni'), 'serve', '--model-path', request['paths']['moss'],
        '--host', '127.0.0.1', '--port', str(port), '--mem-fraction-static', '.4',
        '--endpoints.base_path', request['ipc_path'],
        '--asr.engine.max_total_tokens', '131072', '--asr.engine.max_running_requests', '16',
        '--asr.engine.cuda_graph_max_bs', '16']
    log_path = Path(request['output']).with_suffix('.log')
    checkpoint_path = Path(request['output']).with_suffix('.checkpoint.json')
    maximum = request.get('moss_chunk_seconds') or 5400
    snapshot=dict(phase='loading',start_offset=request.get('start_offset',0),
                  completed_until=request.get('start_offset',0),items=[],chunks=[],ranges=[],load_seconds=0,inference_seconds=0)
    save(checkpoint_path,snapshot)
    ready=None
    started = time.monotonic()
    with log_path.open('w') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(timeout=3) as client:
                while True:
                    if child.poll() is not None or time.monotonic() - started > 1800:
                        raise RuntimeError('SGLang 초기화 실패: ' + log_path.read_text(errors='replace')[-1800:])
                    try:
                        if client.get(f'http://127.0.0.1:{port}/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
            ready = time.monotonic()
            snapshot.update(phase='processing',load_seconds=ready-started)
            save(checkpoint_path,snapshot)
            def infer(offset, end, maximum):
                data = dict(model=request['paths']['moss'], response_format='verbose_json', temperature='0',
                            repetition_penalty='1', max_new_tokens=str(maximum))
                if request.get('prompt'):
                    data['prompt'] = request['prompt']
                audio_path = Path(request['audio'])
                cropped = offset > 0 or end < duration
                if cropped:
                    audio_path = Path(request['output']).with_suffix('.remaining.wav')
                    with wave.open(request['audio'], 'rb') as source, wave.open(str(audio_path), 'wb') as tail:
                        tail.setparams(source.getparams())
                        first = min(source.getnframes(), round(offset*source.getframerate()))
                        remaining = min(source.getnframes(), round(end*source.getframerate())) - first
                        source.setpos(first)
                        while remaining:
                            block = source.readframes(min(remaining, 16000*30))
                            if not block:
                                raise ValueError('MOSS 입력 음성 구간이 불완전합니다.')
                            tail.writeframes(block)
                            remaining -= len(block) // (source.getnchannels()*source.getsampwidth())
                try:
                    with httpx.Client(timeout=3600) as client, audio_path.open('rb') as audio:
                        response = client.post(f'http://127.0.0.1:{port}/v1/audio/transcriptions', data=data,
                            files={'file': ('audio.wav', audio, 'audio/wav')})
                        response.raise_for_status()
                        return response.json()
                except httpx.HTTPError as error:
                    # SGLang writes CUDA failures to its private server log;
                    # propagate the cause instead of only an HTTP 500 message.
                    with log_path.open('rb') as stream:
                        stream.seek(max(0, log_path.stat().st_size-16384))
                        server_error = stream.read().decode(errors='replace')
                    body = error.response.text if isinstance(error, httpx.HTTPStatusError) else ''
                    if is_cuda_oom(str(error)+'\n'+body+'\n'+server_error):
                        raise RuntimeError(CUDA_OOM_MESSAGE) from None
                    if is_moss_capacity_error(str(error)+'\n'+body+'\n'+server_error):
                        raise RuntimeError(MOSS_CAPACITY_MESSAGE) from None
                    raise
                finally:
                    if cropped:
                        audio_path.unlink(missing_ok=True)
            def completed(items,chunks,ranges):
                snapshot.update(items=items,chunks=chunks,ranges=ranges,completed_until=ranges[-1][1],
                                inference_seconds=time.monotonic()-ready)
                save(checkpoint_path,snapshot)
            items, chunks = transcribe_moss_balanced(duration, infer,
                lambda **event: emit(phase='transcribing', engine='moss-sglang',
                                    memory_retry_count=request.get('memory_retry_count',0),**event),
                allow_split=request.get('allow_moss_split', False),
                start_offset=request.get('start_offset', 0), end_offset=request.get('end_offset'),
                max_seconds=maximum,on_partition_complete=completed)
            result = dict(items=items, chunks=chunks, text='\n'.join(c['text'] for c in chunks),
                partition_count=len(snapshot['ranges']),ranges=snapshot['ranges'],moss_chunk_seconds=maximum,
                load_seconds=ready-started, inference_seconds=time.monotonic()-ready)
            save(request['output'], result)
        except BaseException:
            snapshot.update(load_seconds=(ready or time.monotonic())-started,
                            inference_seconds=time.monotonic()-ready if ready else 0)
            save(checkpoint_path,snapshot)
            raise
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            # The outer supervisor also reaps this stage's process group before
            # starting the next engine; no CUDA allocator survives between stages.


def qwen(request):
    import torch
    from transformers import AutoConfig
    from qwen_asr import Qwen3ASRModel
    from qwen_asr.inference.utils import split_audio_into_chunks, parse_asr_output
    language = {'ko':'Korean','en':'English','ja':'Japanese','zh':'Chinese'}.get(request.get('language'), request.get('language')) or None
    config = AutoConfig.from_pretrained(request['paths']['qwen'], local_files_only=True)
    force_fa2(config)
    audio = get_audio(request['audio'])
    seconds = 180 if request['model_key'] == 'qwen-transformers' else 1200
    parts = split_audio_into_chunks(audio, 16000, seconds)
    maximum = 4096 if seconds == 180 else 8192
    started = time.monotonic()
    wrapper = Qwen3ASRModel.from_pretrained(request['paths']['qwen'], config=config,
        dtype=torch.bfloat16, device_map='cuda:0', attn_implementation='flash_attention_2',
        max_inference_batch_size=request['batch'], max_new_tokens=maximum)
    wrapper.model.eval()
    wrapper.model.generation_config.do_sample = False
    wrapper.model.generation_config.repetition_penalty = 1.
    if any(p.dtype != torch.bfloat16 for p in wrapper.model.parameters()):
        raise RuntimeError('Qwen BF16 설정을 확인하지 못했습니다.')
    original = wrapper.model.generate
    raw_chunks = []
    def capture(*args, **kwargs):
        kwargs.update(do_sample=False, repetition_penalty=1.)
        value = original(*args, **kwargs)
        eos = kwargs.get('eos_token_id', wrapper.model.generation_config.eos_token_id)
        if eos is None:
            eos = wrapper.processor.tokenizer.eos_token_id
        eos = [eos] if isinstance(eos, int) else eos
        if not eos:
            raise RuntimeError('Qwen 출력 종료 토큰을 확인하지 못했습니다.')
        for row in value.sequences[:, kwargs['input_ids'].shape[1]:]:
            ids = row.cpu().tolist()
            stop = next((i+1 for i, token in enumerate(ids) if token in eos), len(ids))
            ids = ids[:stop]
            raw_chunks.append(dict(token_ids=ids, raw=wrapper.processor.tokenizer.decode(ids, skip_special_tokens=False),
                                   finish_reason='stop' if ids and ids[-1] in eos else 'length'))
        return value
    wrapper.model.generate = capture
    torch.cuda.synchronize()
    ready = time.monotonic()
    def infer(selected):
        raw_chunks.clear()
        hint = request.get('hotwords') or ''
        with torch.inference_mode():
            raw = wrapper._infer_asr([hint]*len(selected), [a for a, _ in selected],
                                     [language]*len(selected))
        if len(raw) != len(selected) or len(raw_chunks) != len(selected):
            raise RuntimeError('Qwen 구간별 출력 검증 정보가 일치하지 않습니다.')
        return list(zip(raw, raw_chunks))
    chunks, retries = transcribe_qwen_adaptively(parts, infer,
        lambda text: parse_asr_output(text, user_language=language),
        lambda **event: emit(phase='transcribing', engine='qwen', **event), batch=request['batch'])
    torch.cuda.synchronize()
    save(request['output'], dict(chunks=chunks, text='\n'.join(c['text'] for c in chunks),
        adaptive_retries=retries, load_seconds=ready-started, inference_seconds=time.monotonic()-ready))


def align(request):
    import torch
    from transformers import AutoConfig
    from qwen_asr import Qwen3ForcedAligner
    audio = get_audio(request['audio'])
    chunks = json.loads(Path(request['qwen_result']).read_text())['chunks']
    total = torch.cuda.get_device_properties(0).total_memory
    # Only guide batch growth; CUDA allocations have no app-imposed ceiling.
    budget = total
    config = AutoConfig.from_pretrained(request['paths']['aligner'], local_files_only=True)
    force_fa2(config)
    started = time.monotonic()
    model = Qwen3ForcedAligner.from_pretrained(request['paths']['aligner'], config=config,
        dtype=torch.bfloat16, device_map='cuda:0', attn_implementation='flash_attention_2')
    torch.cuda.synchronize()
    ready = time.monotonic()
    base = torch.cuda.memory_allocated()
    i, batch, items, history = 0, 1, [], []
    while i < len(chunks):
        selected = chunks[i:i+batch]
        emit(phase='aligning', chunk_count=len(chunks), completed=i)
        torch.cuda.reset_peak_memory_stats()
        try:
            active = [c for c in selected if c['text'].strip()]
            values = model.align(audio=[(audio[round(c['offset']*16000):round(c['end']*16000)],16000) for c in active],
                text=[c['text'] for c in active], language=[c['language'].split(',')[0] or 'Korean' for c in active]) if active else []
            torch.cuda.synchronize()
            growth = max(64*1024**2, (torch.cuda.max_memory_allocated()-base)/max(1,len(active)))
            history.append(dict(start=i, batch=len(selected), peak_reserved_bytes=torch.cuda.max_memory_reserved()))
            for chunk, value in zip(active, values):
                words = dataclasses.asdict(value)['items']
                for word in words:
                    start = min(chunk['end'], max(chunk['offset'], word['start_time']+chunk['offset']))
                    end = min(chunk['end'], max(start, word['end_time']+chunk['offset']))
                    items.append(dict(start=start,end=end,text=word['text']))
                if not words:
                    items.append(dict(start=chunk['offset'],end=chunk['end'],text=chunk['text'],unaligned=True))
            i += len(selected)
            batch = max(1,min(32,int((budget-base)*.65/growth),len(chunks)-i or 1))
            del values
        except torch.cuda.OutOfMemoryError:
            if batch <= 1:
                raise
            batch = max(1,batch//2)
        gc.collect()
        torch.cuda.empty_cache()
    save(request['output'],dict(items=items, batch_history=history, load_seconds=ready-started,
        inference_seconds=time.monotonic()-ready))


def nemotron(request):
    import torch
    from transformers import AutoProcessor, AutoModelForAudioFrameClassification
    started = time.monotonic()
    audio = get_audio(request['audio'])
    processor = AutoProcessor.from_pretrained(request['paths']['nemotron'], local_files_only=True)
    processor.streaming_modes['offline'] = (340,40)
    processor.set_streaming_mode('offline')
    model = AutoModelForAudioFrameClassification.from_pretrained(request['paths']['nemotron'],
        local_files_only=True,dtype=torch.float32,attn_implementation='sdpa').to('cuda').eval()
    torch.cuda.synchronize()
    ready = time.monotonic()
    emit(phase='diarizing')
    cache,logits,frame,first = None,[],0,True
    with torch.inference_mode():
        while True:
            start = 0 if first else processor.audio_chunk_start(frame)
            size = processor.num_samples_first_audio_chunk if first else processor.num_samples_per_audio_chunk
            last = start+size >= len(audio)
            inputs = processor(audio[start:start+size],sampling_rate=16000,is_streaming=True,
                is_first_audio_chunk=first,is_last_audio_chunk=last).to('cuda',torch.float32)
            value = model(**inputs,speaker_cache=cache)
            cache = value.speaker_cache
            logits.append(value.logits.cpu())
            if last:
                break
            frame += processor.num_mel_frames_per_step
            first = False
        groups = processor.extract_speaker_dict(torch.cat(logits,dim=1))[0]
    turns = [dict(start=max(0,float(v['Start'])),end=min(len(audio)/16000,float(v['End'])),speaker=str(v['Speaker']))
             for v in groups if float(v['End'])>float(v['Start']) and float(v['Start'])<len(audio)/16000]
    torch.cuda.synchronize()
    save(request['output'],dict(turns=turns,load_seconds=ready-started,inference_seconds=time.monotonic()-ready))


if __name__ == '__main__':
    try:
        guard()
        request = json.loads(Path(sys.argv[1]).read_text())
        if request['stage'] not in {'prepare','moss'}:
            import torch
            torch.set_num_threads(4)
            torch.manual_seed(0)
            if not torch.cuda.is_available() or torch.cuda.device_count()!=1:
                raise RuntimeError('선택한 NVIDIA GPU 한 개를 확인해 주세요.')
            if request['stage'] in {'qwen','aligner'} and not torch.cuda.is_bf16_supported():
                raise RuntimeError('이 전사 방식은 BF16을 지원하는 NVIDIA GPU가 필요합니다.')
        {'prepare':prepare,'moss':moss,'qwen':qwen,'aligner':align,'nemotron':nemotron}[request['stage']](request)
    except Exception as error:
        if 'request' in locals() and request.get('output'):
            save(Path(request['output']).with_suffix('.error.json'),
                 {'cuda_oom':is_cuda_oom(type(error).__module__+'.'+type(error).__name__+': '+str(error)),
                  'context_capacity':request.get('stage')=='moss' and is_moss_capacity_error(str(error))})
        emit(phase='error',error=str(error)[-1800:])
        raise
