"""Client-side, hardware-scoped length calibration; no ML dependencies."""
import json
import math
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def calibrations():
    return json.loads(Path(__file__).with_name('duration_calibrations.json').read_text())


def interpolate(value, points, *, memory=False):
    if value <= points[0][0]:
        return points[0][1] if memory else points[0][1] * (.25 + .75 * value / points[0][0])
    if len(points) == 1:
        return points[0][1] if memory else points[0][1] * value / points[0][0]
    left, right = points[-2:]
    for a, b in zip(points, points[1:]):
        left, right = a, b
        if value <= b[0]:
            break
    return max(0, left[1] + (right[1] - left[1]) * (value - left[0]) / (right[0] - left[0]))


def metadata(profile, value):
    first, last = profile['time_knots'][0][0], profile['time_knots'][-1][0]
    extrapolated = not first <= value <= last
    provisional = profile['validation']['status'] != 'checked'
    label = '실측 참고 · 시간 예측 검증 보완 중' if provisional else '실측 기반'
    if extrapolated:
        label += ' · 측정 범위 밖 추정'
    return dict(calibration_version=calibrations()['version'], calibration_model=profile['model'],
                calibration_status='provisional' if provisional else 'checked',
                calibration_extrapolated=extrapolated, calibration_note=label,
                validation_time_error_percent=profile['validation']['time_absolute_error_percent'],
                time_source='duration-calibration', memory_source=profile['memory_metric'] + '-plus-headroom')


def mac_estimate(device, duration, count, hardware, runtime):
    key = device.get('model_key', 'mlx-8bit')
    profile = calibrations()['profiles'].get(key)
    if not profile or profile['platform'] != 'mac' or not hardware or runtime != profile['runtime']:
        return None
    expected = profile['hardware']
    if (hardware.get('name') != expected['name'] or not isinstance(hardware.get('memory_gib'), (int, float))
            or not math.isfinite(hardware['memory_gib'])
            or abs(hardware['memory_gib'] - expected['memory_gib']) > .5):
        return None
    options = device.get('options', {})
    if any(options.get(k, v) != v for k, v in profile['options'].items()):
        return None
    repo = device.get('model_id') or device.get('model')
    if repo and repo != profile['model_repo']:
        return None
    path = device.get('model_path', '')
    if '/snapshots/' in path and path.rstrip('/').rsplit('/', 1)[-1] != profile['model_revision']:
        return None
    if device.get('model_source') == 'folder' and '/snapshots/' not in path:
        return None
    seconds = duration / count
    if not math.isfinite(seconds) or not 0 < seconds <= 5400:
        return None
    bridge = min(options.get('bridge_seconds', 30), seconds)
    bridge_cost = interpolate(bridge, profile['bridge_time_knots'] or profile['time_knots']) if bridge else 0
    inference = count * interpolate(seconds, profile['time_knots']) + (count - 1) * bridge_cost
    raw_memory = interpolate(seconds, profile['memory_knots'], memory=True)
    peak = raw_memory * profile['memory_multiplier'] + profile['memory_headroom_gib']
    result = {**metadata(profile, seconds), 'inference_seconds':inference,
              'setup_seconds':profile['cold_setup_seconds'],
              'time_seconds':max(1, round(profile['cold_setup_seconds'] + inference)),
              'memory_raw_gib':raw_memory, 'memory_expected_gib':round(peak, 1),
              'memory_low_gib':round(raw_memory, 1), 'memory_high_gib':round(peak, 1),
              'note':'모델 준비 포함 · 전송 제외'}
    # No successful full-context timing beyond 30m exists for the 4-bit profile.
    if key == 'mlx-4bit' and seconds > profile['time_knots'][-1][0]:
        result.update(time_seconds=None, calibration_note='실측 참고 · 긴 청크 시간은 추가 측정 필요')
    return result


def server_estimate(key, duration, gpu, runtime, batch):
    profile = calibrations()['profiles'].get(key)
    if not profile or profile['platform'] != 'server' or runtime != profile['runtime']:
        return None
    name = ' '.join((gpu or {}).get('name', '').lower().removeprefix('nvidia ').split())
    capacity = (gpu or {}).get('memory_total_mib')
    if (name != profile['hardware']['name'] or not isinstance(capacity, (int, float)) or not math.isfinite(capacity)
            or abs(capacity / 1024 - profile['hardware']['capacity_gib']) >= 4):
        return None
    chunks = 1 if duration <= 180 else math.ceil(duration / 175)
    automatic = min(24, chunks) if key == 'qwen-transformers' else 1 if key == 'moss-sglang' else min(4, math.ceil(duration / 1195))
    if batch != automatic:
        return None
    # Extra Qwen GPU waves are structural extrapolation, not another measured point.
    waves = max(1, math.ceil(chunks / 24)) if key == 'qwen-transformers' else 1
    effective = duration / waves
    inference = interpolate(effective, profile['time_knots']) * waves
    raw_memory = interpolate(min(duration, 24 * 175) if waves > 1 else duration, profile['memory_knots'], memory=True)
    peak = raw_memory * profile['memory_multiplier'] + profile['memory_headroom_gib']
    result = {**metadata(profile, effective), 'inference_seconds':inference,
              'setup_seconds':profile['cold_setup_seconds'],
              'time_seconds':max(1, round(profile['cold_setup_seconds'] + inference)),
              'memory_raw_gib':raw_memory, 'memory_expected_gib':round(peak, 1), 'memory_high_gib':round(peak, 1),
              'note':'모델 준비 포함 · 전송 제외'}
    if waves > 1:
        result.update(calibration_extrapolated=True, calibration_note='실측 참고 · 동시 처리 상한을 넘는 길이는 추정')
    if key != 'moss-sglang' and abs(duration - 180) <= 2:
        result['calibration_note'] = '실측 참고 · 이 길이의 일부 전사에서 출력 오류 관측'
        result['calibration_status'] = 'caution'
    return result
