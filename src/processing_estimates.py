"""Point estimates from matching measured profiles, with a structural fallback.

Time is the sum of per-chunk costs plus boundary bridges. Piecewise linear
interpolation preserves measured context-length effects; duration alone is not
proportional to latency. VRAM plans use peak reserved memory plus context
headroom, not only live tensor allocation. One recording is not a guarantee.
"""
import math
from collections import defaultdict
from statistics import median

from src.performance_history import matching_profile, seed_profiles

GIB = 1024 ** 3
ESTIMATE_VERSION = 'measured-profiles-20261021-v3'
CUDA_CONTEXT_HEADROOM_GIB = 1.0


def profile_points(samples, kind, field):
    # Minute buckets combine repeated runs without treating small audio-frame
    # differences as separate interpolation points. Medians reduce timing noise.
    groups = defaultdict(list)
    for sample in samples:
        if sample['kind'] == kind:
            groups[max(1, round(sample['audio_seconds'] / 60))].append(sample)
    points = []
    for rows in groups.values():
        duration = median(row['audio_seconds'] for row in rows)
        cost = max(row[field] for row in rows) if field == 'peak_reserved_gib' else median(row[field] for row in rows)
        points.append((duration, cost))
    ordered = []
    for duration, cost in sorted(points):
        ordered.append((duration, max(cost, ordered[-1][1] if ordered else 0)))
    return ordered


def project(value, points, *, proportional_below=True):
    if len(points) == 1 or (proportional_below and value < points[0][0]):
        return points[0][1] * value / points[0][0]
    return max(0, interpolate(value, points))


def interpolate(value, points):
    """Interpolate between observations; extrapolate with the nearest slope."""
    left, right = points[:2]
    for a, b in zip(points, points[1:]):
        left, right = a, b
        if value <= b[0]:
            break
    return left[1] + (right[1] - left[1]) * (value - left[0]) / (right[0] - left[0])


def memory_gib(gpu, field):
    value = (gpu or {}).get(field)
    return value / 1024 if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def estimate_processing(device, duration, count, gpu=None, local_memory_gib=None, profiles=None):
    seconds = duration / count
    backend = device.get('backend', 'mlx')
    options = device.get('options', {})
    key = device.get('model_key', 'mlx-8bit' if backend == 'mlx' else 'cuda-original')
    result = dict(version=ESTIMATE_VERSION, chunk_seconds=seconds, chunk_count=count,
                  time_source='initial-heuristic', memory_source='structural-heuristic',
                  memory_expected_gib=None, memory_low_gib=None, memory_high_gib=None, time_seconds=None,
                  capacity_gib=local_memory_gib if backend == 'mlx' else memory_gib(gpu, 'memory_total_mib'),
                  free_gib=memory_gib(gpu, 'memory_free_mib'),
                  capacity_kind='통합 메모리' if backend == 'mlx' else 'VRAM', exceeds_capacity=False, exceeds_free=False,
                  note='초기 추정 · 모델 준비·파일 전송 제외')
    profile = matching_profile(device, gpu, seed_profiles() if profiles is None else profiles)
    if key == 'custom' and not profile:
        result['note'] = '사용자 지정 모델은 메모리·시간 추정을 지원하지 않습니다.'
        return result
    precision = options.get('precision', 'auto')
    if backend == 'cuda' and (precision not in {'auto', 'bf16', 'fp16'} or options.get('cuda_kv_bits', 0)
                              or key in {'cuda-4bit', 'cuda-8bit'}):
        result['note'] = '서버 양자화 지원이 종료되었습니다. 원본 모델을 선택해 주세요.'
        return result
    audio_tokens = seconds * 12.5
    output_tokens = min(options.get('max_tokens', 32768), seconds * 6)
    tokens = audio_tokens + output_tokens
    weights = {'mlx-4bit': .90, 'mlx-8bit': 1.18, 'mlx-16bit': 1.69}.get(key, 1.70)
    kv_bytes = 1 if backend == 'mlx' and options.get('kv_bits', 8) == 8 else 2
    kv = 2 * 28 * 8 * 128 * kv_bytes * tokens / GIB
    if backend == 'cuda' and options.get('use_cache', True) is False:
        kv = 0
    peak = weights + 1.5 + kv + seconds / 60 * .08
    if backend == 'cuda' and options.get('attention', 'sdpa') == 'eager':
        peak += 16 * tokens ** 2 * 2 / GIB
    result.update(memory_expected_gib=round(peak, 1), memory_low_gib=round(peak * .75, 1),
                  memory_high_gib=round(peak * 1.4, 1))
    ratio = .65 if backend == 'mlx' else .40
    work = duration * ratio * (.55 + .45 * seconds / 1800)
    bridge_seconds = options.get('bridge_seconds', 30)
    work += (count - 1) * bridge_seconds * ratio + count * 3
    if backend == 'cuda' and options.get('use_cache', True) is False:
        work *= 3
    if backend == 'cuda' and options.get('attention') == 'eager':
        work *= 1.5
    if profile and 0 < seconds <= 5400:
        samples = profile['samples']
        times = profile_points(samples, 'main', 'inference_seconds')
        memory = profile_points(samples, 'main', 'peak_reserved_gib')
        bridges = profile_points(samples, 'bridge', 'inference_seconds')
        if times and memory:
            bridge_duration = min(bridge_seconds, seconds)
            boundary_cost = project(bridge_duration, bridges or times) if bridge_duration else 0
            work = count * project(seconds, times) + (count - 1) * boundary_cost
            reserved = project(seconds, memory, proportional_below=False)
            # A short bridge still includes the resident model/runtime footprint.
            floor = min(row['peak_reserved_gib'] for row in samples)
            reserved = max(floor, reserved)
            # Bridges are never longer than a main chunk. Their reserved
            # counters can retain allocator blocks from preceding long chunks;
            # do not mistake those blocks for an intrinsic short-bridge cost.
            observed = profile.get('observed_samples', 0)
            result.update(time_source='measured-profile' if observed else 'reference-profile',
                          memory_source='measured-reserved-plus-headroom',
                          calibration_samples=len(samples), observed_samples=observed,
                          profile_environment=profile.get('software', {}),
                          memory_expected_gib=round(reserved + CUDA_CONTEXT_HEADROOM_GIB, 1),
                          memory_low_gib=round(reserved * .9 + CUDA_CONTEXT_HEADROOM_GIB, 1),
                          memory_high_gib=round(reserved * 1.25 + CUDA_CONTEXT_HEADROOM_GIB, 1),
                          note=f"{(gpu or {}).get('name', 'GPU')} 실측 보정 · 예약 메모리와 여유 포함 · 모델 준비·전송 제외")
            if not times[0][0] <= seconds <= times[-1][0]:
                result['note'] += ' · 실측 길이 밖은 외삽'
    elif backend == 'cuda':
        result['note'] = '실측 전 초기 추정 · 전사 완료 후 보정 · 모델 준비·전송 제외'
    result['time_seconds'] = max(1, round(work))
    result['exceeds_capacity'] = bool(result['capacity_gib'] and result['memory_high_gib'] > result['capacity_gib'])
    result['exceeds_free'] = bool(result['free_gib'] is not None and result['memory_high_gib'] > result['free_gib'])
    return result
