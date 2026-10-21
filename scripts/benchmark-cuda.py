"""Run one isolated, UUID-bound MOSS ablation. Transcripts stay in private output.

Use the prepared CUDA runtime and cached model; this script never downloads a
model, updates a shared runtime, or changes the app's saved device settings.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time
import traceback
import wave


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def audio_slice(source, target, offset, duration):
    with wave.open(str(source), 'rb') as audio:
        rate = audio.getframerate()
        frames = audio.getnframes()
        start = min(frames, round(offset * rate))
        count = min(frames - start, round(duration * rate))
        parameters = audio.getparams()
        audio.setpos(start)
        with wave.open(str(target), 'wb') as output:
            output.setparams(parameters)
            output.writeframes(audio.readframes(count))
    return count / rate


def run(args):
    if args.seconds < 0 or args.bridge_seconds < 0 or args.budget_seconds < 0:
        raise ValueError('Durations and inference budget cannot be negative')
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.gpu.startswith('GPU-'):
        raise ValueError('An explicit GPU UUID is required')
    os.environ.update(CUDA_VISIBLE_DEVICES=args.gpu, MOSS_MODEL_PATH=str(args.model),
                      HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      COSMOSS_MODEL_OPTIONS=json.dumps({'precision': 'bf16', 'attention': 'sdpa',
                          'use_cache': True}))
    import torch
    from src import cuda_server as worker
    from moss_transcribe_diarize.inference_utils import build_transcription_messages, generate_transcription

    with wave.open(str(args.audio), 'rb') as audio:
        available = audio.getnframes() / audio.getframerate()
    duration = min(available, args.seconds) if args.seconds else available
    result = {'schema_version': 1, 'case': args.output.name, 'gpu_uuid': args.gpu,
              'gpu_name': torch.cuda.get_device_name(0), 'duration': duration,
              'source_sha256': hashlib.sha256(args.audio.read_bytes()).hexdigest(),
              'chunks': args.chunks, 'bridge_seconds': args.bridge_seconds,
              'kv_bits': args.kv_bits, 'weight_precision': 'bf16', 'attention': 'sdpa',
              'seed': 0, 'max_new_tokens': args.max_tokens, 'runs': [], 'phase': 'loading',
              'torch_threads': torch.get_num_threads(), 'torch_interop_threads': torch.get_num_interop_threads(),
              'worker_sha256': hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest(),
              'versions': {name: importlib.metadata.version(name)
                           for name in ['torch', 'transformers', 'moss-transcribe-diarize']},
              'measurement_scope': 'One GPU; sequential chunks and optional boundary bridges. '
                   'Inference includes input feature preparation and output decode; excludes model load, '
                   'warmup, transfer, audio slicing, final speaker merge and app startup.'}
    report = args.output / 'result.json'
    progress = args.output / 'progress.json'
    atomic_json(report, result)
    torch.manual_seed(0)
    started = time.perf_counter()
    worker.load_model()
    # Historical ablation only: quantization is deliberately absent from the app.
    if args.kv_bits:
        worker.MODEL.generation_config.cache_implementation = 'quantized'
        worker.MODEL.generation_config.cache_config = dict(backend='hqq', nbits=args.kv_bits,
            axis_key=1, axis_value=1, q_group_size=64, residual_length=128)
    torch.cuda.synchronize()
    result['load_seconds'] = time.perf_counter() - started
    result['cache_implementation'] = worker.MODEL.generation_config.cache_implementation or 'dynamic'
    cache_seen = {}

    def observe_cache(module, positional, kwargs):
        cache = kwargs.get('past_key_values')
        if cache is not None:
            cache_seen['class'] = type(cache).__name__
            if getattr(cache, 'layers', None):
                cache_seen['layer_class'] = type(cache.layers[0]).__name__

    hook = worker.MODEL.register_forward_pre_hook(observe_cache, with_kwargs=True)
    warmup = args.output / 'warmup.wav'
    audio_slice(args.audio, warmup, 0, 10)
    started = time.perf_counter()
    generate_transcription(worker.MODEL, worker.PROCESSOR, build_transcription_messages(warmup),
                           max_new_tokens=64, do_sample=False, device=torch.device('cuda:0'), dtype=worker.DTYPE)
    torch.cuda.synchronize()
    warmup.unlink()
    result['warmup_seconds'] = time.perf_counter() - started
    result['observed_cache'] = cache_seen.copy()
    if args.kv_bits and (cache_seen.get('class') != 'QuantizedCache'
                        or cache_seen.get('layer_class') != 'HQQQuantizedLayer'):
        raise RuntimeError('Requested KV quantization was not active during the warmup')
    torch.cuda.empty_cache()
    jobs = [('chunk', index, index * duration / args.chunks, duration / args.chunks)
            for index in range(args.chunks)]
    if args.chunks > 1 and args.bridge_seconds:
        jobs += [('bridge', index - 1, index * duration / args.chunks - args.bridge_seconds / 2,
                  args.bridge_seconds) for index in range(1, args.chunks)]
    result['phase'] = 'running'
    atomic_json(report, result)
    total_started = time.perf_counter()
    for kind, index, offset, seconds in jobs:
        audio = args.output / f'{kind}-{index}.wav'
        seconds = audio_slice(args.audio, audio, offset, seconds)
        run = {'kind': kind, 'index': index, 'offset': offset, 'duration': seconds}
        status = {'case': args.output.name, 'kind': kind, 'index': index, 'phase': 'preparing',
                  'generated_tokens': 0, 'started_unix': time.time()}
        atomic_json(progress, status)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        last_written = 0
        def input_ready(tokens):
            torch.cuda.synchronize()
            run.update(prompt_tokens=tokens, input_seconds=time.perf_counter() - started)
            status.update(phase='prefill', prompt_tokens=tokens)
            atomic_json(progress, status)
        def generated(tokens):
            nonlocal last_written
            now = time.perf_counter()
            if 'first_token_seconds' not in run:
                torch.cuda.synchronize()
                run['first_token_seconds'] = time.perf_counter() - started
            if args.budget_seconds and now - started >= args.budget_seconds:
                result.update(phase='error', censored_after_seconds=now-started,
                    observed_generated_tokens=tokens,
                    stop_reason='Exceeded inference budget; not a completed latency measurement.')
                atomic_json(report, result)
                raise TimeoutError('Inference budget exceeded')
            if now - last_written >= 5:
                status.update(phase='decoding', generated_tokens=tokens, elapsed_seconds=now-started)
                atomic_json(progress, status)
                last_written = now
        value = generate_transcription(worker.MODEL, worker.PROCESSOR, build_transcription_messages(audio),
                    max_new_tokens=args.max_tokens, do_sample=False, device=torch.device('cuda:0'),
                    dtype=worker.DTYPE, input_callback=input_ready, token_callback=generated)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        run.update(inference_seconds=elapsed, generated_tokens=value['generated_tokens'],
                   prompt_tokens=value['prompt_len'], hit_token_limit=value['generated_tokens'] >= args.max_tokens,
                   peak_allocated_gib=torch.cuda.max_memory_allocated()/1024**3,
                   peak_reserved_gib=torch.cuda.max_memory_reserved()/1024**3)
        run['decode_tokens_per_second'] = max(0,value['generated_tokens']-1)/max(.001,elapsed-run['first_token_seconds'])
        transcript = args.output / f'{kind}-{index}.txt'
        transcript.write_text(value['text'])
        run['transcript_sha256'] = hashlib.sha256(transcript.read_bytes()).hexdigest()
        result['runs'].append(run)
        atomic_json(report, result)
        audio.unlink()
        torch.cuda.empty_cache()
    hook.remove()
    result.update(phase='done', wall_seconds=time.perf_counter()-total_started,
                  inference_seconds=sum(r['inference_seconds'] for r in result['runs']),
                  peak_allocated_gib=max(r['peak_allocated_gib'] for r in result['runs']),
                  peak_reserved_gib=max(r['peak_reserved_gib'] for r in result['runs']))
    result['rtf'] = result['inference_seconds'] / duration
    atomic_json(report, result)
    atomic_json(progress, {'case':args.output.name, 'phase':'done'})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=1800)
    parser.add_argument('--chunks', type=int, choices=range(1, 9), default=1)
    parser.add_argument('--bridge-seconds', type=float, default=30)
    parser.add_argument('--kv-bits', type=int, choices=(0, 4, 8), default=0)
    parser.add_argument('--max-tokens', type=int, default=32768)
    parser.add_argument('--budget-seconds', type=float, default=900,
                        help='Per-chunk inference budget; 0 disables the limit. Aborted cases are not completed measurements.')
    args = parser.parse_args()
    try:
        summary = run(args)
        print(json.dumps({k:summary[k] for k in ['case','phase','inference_seconds','peak_allocated_gib']}),flush=True)
    except BaseException as error:
        report = args.output / 'result.json'
        result = json.loads(report.read_text()) if report.exists() else {'case':args.output.name}
        result.update(phase='error', error_type=type(error).__name__, error=str(error))
        atomic_json(report,result)
        traceback.print_exc()
        raise
