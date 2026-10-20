"""Versioned initial planning heuristics, not measured hardware benchmarks.

Tune the coefficients here after collecting real inference measurements.
MOSS's published config has 28 layers, 8 KV heads, head_dim=128,
and about 12.5 audio tokens/s after the Whisper frontend's 4x merge.
"""
import math

GIB = 1024 ** 3
ESTIMATE_VERSION = 'initial-v1'
# Seconds of processing per second of audio at a 30-minute chunk, guessed.
TIME_RATIOS = [('h200', .08), ('h100', .10), ('a100', .16), ('4090', .22),
               ('3090', .30), ('6000 ada', .24), ('a6000', .32)]


def memory_gib(gpu, field):
    value = (gpu or {}).get(field)
    return value / 1024 if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def estimate_processing(device, duration, count, gpu=None, local_memory_gib=None):
    seconds = duration / count
    backend = device.get('backend', 'mlx')
    options = device.get('options', {})
    key = device.get('model_key', 'mlx-8bit' if backend == 'mlx' else 'cuda-original')
    result = dict(version=ESTIMATE_VERSION, chunk_seconds=seconds, chunk_count=count,
                  memory_low_gib=None, memory_high_gib=None, time_low_seconds=None, time_high_seconds=None,
                  capacity_gib=local_memory_gib if backend == 'mlx' else memory_gib(gpu, 'memory_total_mib'),
                  free_gib=memory_gib(gpu, 'memory_free_mib'),
                  capacity_kind='통합 메모리' if backend == 'mlx' else 'VRAM', exceeds_capacity=False, exceeds_free=False,
                  note='초기 추정 · 모델 준비·파일 전송 제외')
    if key == 'custom':
        result['note'] = '사용자 지정 모델은 메모리·시간 추정을 지원하지 않습니다.'
        return result
    audio_tokens = seconds * 12.5
    output_tokens = min(options.get('max_tokens', 32768), seconds * 6)
    tokens = audio_tokens + output_tokens
    precision = options.get('precision', 'auto')
    weights = {'mlx-4bit':.90, 'mlx-8bit':1.18, 'mlx-16bit':1.69, 'cuda-4bit':1.0, 'cuda-8bit':1.3}.get(key, 1.70)
    if backend == 'cuda' and precision in ('int4', 'int8'):
        weights = 1.0 if precision == 'int4' else 1.3
    kv_bytes = 1 if backend == 'mlx' and options.get('kv_bits', 8) == 8 else 2
    kv = 2 * 28 * 8 * 128 * kv_bytes * tokens / GIB
    if backend == 'cuda' and options.get('use_cache', True) is False:
        kv = 0
    # Runtime, audio-encoder batching and temporary activations are deliberately
    # coarse allowances. Eager attention additionally materializes LxL scores.
    peak = weights + 1.5 + kv + seconds / 60 * .08
    if backend == 'cuda' and options.get('attention', 'sdpa') == 'eager':
        peak += 16 * tokens ** 2 * 2 / GIB
    result.update(memory_low_gib=round(peak * .75, 1), memory_high_gib=round(peak * 1.4, 1))
    name = (gpu or {}).get('name', '').lower()
    ratio = .65 if backend == 'mlx' else next((value for marker, value in TIME_RATIOS if marker in name), .40)
    # A tunable mixture of linear work and attention's context-length cost.
    work = duration * ratio * (.55 + .45 * seconds / 1800)
    bridges = (count - 1) * options.get('bridge_seconds', 30)
    work += bridges * ratio + count * 3
    if backend == 'cuda' and options.get('use_cache', True) is False:
        work *= 3
    if backend == 'cuda' and options.get('attention') == 'eager':
        work *= 1.5
    result.update(time_low_seconds=max(1, math.ceil(work * .6)), time_high_seconds=max(1, math.ceil(work * 1.8)))
    result['exceeds_capacity'] = bool(result['capacity_gib'] and result['memory_high_gib'] > result['capacity_gib'])
    result['exceeds_free'] = bool(result['free_gib'] is not None and result['memory_high_gib'] > result['free_gib'])
    return result
