"""Measured CUDA speech presets and conservative, explicit speaker attribution.

This module has no ML dependencies; setup and UI planning stay cheap on the Mac.
"""
from __future__ import annotations

import math
from statistics import median

try:
    from src.speech_engines import ENGINE_PIPELINES, ENGINE_VERSION
except ModuleNotFoundError:
    from speech_engines import ENGINE_PIPELINES, ENGINE_VERSION

PIPELINES = [
    {'key': 'moss-sglang', 'name': 'MOSS 0.9B · SGLang Omni', 'badge': '추천',
     'description': '전체 녹음의 문맥을 유지해 전사한 뒤 Nemotron 0.1B로 화자를 구분합니다. 최대 90분.',
     'reference': 'H200 · 65분37초 · 모델 준비 포함 3분19초 · Peak 약24.4 GiB'},
    {'key': 'qwen-transformers', 'name': 'Qwen3-ASR 1.7B · Transformers', 'badge': '빠른 전사',
     'description': '공식 SDK로 약 3분씩 전사한 뒤 시간 정렬과 Nemotron 0.1B를 순서대로 실행합니다.',
     'reference': 'H200 · 65분37초 · 모델 준비 포함 1분27초 · Peak 약31.3 GiB'},
    {'key': 'moss-qwen-sequential', 'name': 'MOSS 0.9B + Qwen3-ASR 1.7B', 'badge': '보조 대본 비교',
     'description': 'MOSS 전체 전사 → 모델 해제 → Qwen3-ASR 1.7B 약20분 보조 전사 → Nemotron 0.1B. 본문은 MOSS를 유지합니다.',
     'reference': 'H200 · 65분37초 · 모델 준비 포함 6분24초 · Peak 약35.2 GiB'},
]
DEFAULT_PIPELINE = 'moss-sglang'
# Old saved jobs remain executable; new selections expose only PIPELINES.
FAST_PIPELINES = {'moss-batch', 'qwen-batch'} | ENGINE_PIPELINES
BATCHES = (1, 4, 8, 16, 24)
CHUNK_SECONDS = 180
RUNTIME_VERSION = 'speech-torch280-transformers5202-moss-e3bdd93f-v1'
MODELS = {
    'moss': ('OpenMOSS-Team/MOSS-Transcribe-Diarize', '704aa4a9c304e8520be88901e0d1960158ef5b15'),
    'qwen': ('Qwen/Qwen3-ASR-1.7B-hf', 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3'),
    'aligner': ('Qwen/Qwen3-ForcedAligner-0.6B-hf', 'c07281df297b9905d24a508279258cccf987a064'),
    'nemotron': ('nvidia/Nemotron-3-Diarization', 'f667ed73aee57d40cc39428eb768b4fd87a0a29e'),
}

# Full 3937.13 s recording; native precision, same low-energy chunks, one H200.
# These are reference measurements, not an accuracy ranking or a GPU speed map.
REFERENCE = {
    'moss-batch': [(8, 99.508, 8.2344), (23, 76.7419, 17.8887)],
    'qwen-batch': [(1, 208.3958, 9.4980), (4, 80.2331, 13.5469),
                   (8, 56.2053, 20.2148), (23, 46.5799, 31.3398)],
}


def linear_at(value, points):
    left, right = points[:2]
    for a, b in zip(points, points[1:]):
        left, right = a, b
        if value <= b[0]:
            break
    return left[1] + (right[1] - left[1]) * (value - left[0]) / (right[0] - left[0])


def estimate_pipeline(key, duration, gpu, batch=0, observations=()):
    if key not in FAST_PIPELINES or not math.isfinite(duration) or duration <= 0:
        raise ValueError('전사 방식과 음성 길이를 확인해 주세요.')
    if batch not in (0, *BATCHES):
        raise ValueError('동시 청크 수를 다시 선택해 주세요.')
    if key in ENGINE_PIPELINES:
        return estimate_engine(key, duration, gpu, batch, observations)
    # Quiet boundaries may be five seconds earlier than the nominal cut.
    # Budget for that extra tail chunk instead of accidentally turning a
    # measured single batch into two GPU waves (65:37 has 23 actual chunks).
    chunks = 1 if duration <= CHUNK_SECONDS else math.ceil(duration / (CHUNK_SECONDS - 5))
    capacity = (gpu or {}).get('memory_total_mib')
    capacity = capacity / 1024 if isinstance(capacity, (int, float)) and capacity > 0 else None
    free = (gpu or {}).get('memory_free_mib')
    free = free / 1024 if isinstance(free, (int, float)) and free >= 0 else None
    points = [(size, peak) for size, _, peak in REFERENCE[key]]

    def required(size):
        # Short audio does not remove the resident models. For unmeasured small
        # MOSS batches retain the measured batch-8 footprint as the floor.
        peak = max(points[0][1], linear_at(size, points))
        return round(peak + max(1, peak * .12), 1)

    candidates = sorted({min(chunks, value) for value in BATCHES})
    recommended = max((size for size in candidates if capacity and required(size) <= capacity * .7), default=1)
    actual = min(chunks, batch or recommended)
    peak = required(actual)
    time_seconds, source = None, 'unmeasured'
    name = ' '.join((gpu or {}).get('name', '').lower().removeprefix('nvidia ').split())
    # Never transfer H200 latency to a GPU with a different name or VRAM class.
    compatible = [row for row in observations if row.get('pipeline') == key
                  and row.get('gpu_name') == name and row.get('batch') == actual
                  and capacity and abs(row.get('capacity_gib', 0) - capacity) < 4
                  and row.get('runtime') == RUNTIME_VERSION]
    if compatible:
        row = compatible[-1]
        time_seconds = round(row['inference_seconds'] * duration / row['audio_seconds'] + row['load_seconds'])
        peak = max(peak, round(row['peak_process_gib'] * 1.12 + .5, 1))
        source = 'observed'
    elif name == 'h200' and capacity and capacity >= 120:
        # Batching is nonlinear. Interpolate per-wave latency, then scale the
        # number of waves; don't assume doubling batch doubles throughput.
        costs = [(size, elapsed / math.ceil(23 / size)) for size, elapsed, _ in REFERENCE[key]]
        per_wave = max(1, linear_at(actual, costs))
        time_seconds = round(per_wave * math.ceil(chunks / actual) * min(1, duration / CHUNK_SECONDS) + 10)
        source = 'reference'
    return {'pipeline': key, 'batch': actual, 'requested_batch': batch, 'recommended_batch': recommended,
            'chunk_count': chunks, 'chunk_seconds': CHUNK_SECONDS, 'memory_expected_gib': peak,
            'memory_high_gib': peak, 'capacity_gib': capacity, 'free_gib': free,
            'budget_gib': round(capacity * .7, 1) if capacity else None,
            'exceeds_budget': bool(capacity and peak > capacity * .7),
            'exceeds_capacity': bool(capacity and peak > capacity),
            'exceeds_free': bool(free is not None and peak > free),
            'capacity_kind': 'VRAM', 'time_seconds': max(1, time_seconds) if time_seconds is not None else None,
            'time_source': source, 'memory_source': 'reference-plus-headroom'}


def estimate_engine(key, duration, gpu, batch, observations):
    # Prediction runs on the client. Remote stage catalogs use only attribution.
    from src.duration_estimates import server_estimate
    if key != 'qwen-transformers' and duration > 5400:
        raise ValueError('MOSS 전체 입력은 90분 이하 녹음을 지원합니다. 녹음을 나누어 주세요.')
    capacity = (gpu or {}).get('memory_total_mib')
    capacity = capacity / 1024 if capacity else None
    free = (gpu or {}).get('memory_free_mib')
    free = free / 1024 if free is not None else None
    if key == 'qwen-transformers':
        legacy = estimate_pipeline('qwen-batch',duration,gpu,batch)
        actual,chunks,peak = legacy['batch'],legacy['chunk_count'],legacy['memory_expected_gib']
        seconds = 180
    else:
        # MOSS keeps one context; a combined preset batches only the auxiliary
        # Qwen SDK20m cuts. Neither control splits MOSS into independent contexts.
        chunks = 1
        actual = 1 if key=='moss-sglang' else min(4,batch or 4,math.ceil(duration/1195))
        peak = round((24.42 if key=='moss-sglang' else 35.23)*1.12+1,1)
        seconds = duration
    name = ' '.join((gpu or {}).get('name','').lower().removeprefix('nvidia ').split())
    compatible = [r for r in observations if r.get('pipeline')==key and r.get('runtime')==ENGINE_VERSION
                  and r.get('gpu_name')==name and r.get('batch')==actual and capacity
                  and abs(r.get('capacity_gib',0)-capacity)<4]
    measured = server_estimate(key, duration, gpu, ENGINE_VERSION, actual)
    elapsed,source = None,'unmeasured'
    if measured:
        elapsed, peak, source = measured['time_seconds'], measured['memory_expected_gib'], measured['time_source']
        # A recent observation at this length can replace this point. A short
        # sample must never flatten the nonlinear curve for all longer inputs.
        nearby = [r for r in compatible if abs(r['audio_seconds'] - duration) <= max(1, duration * .01)]
        if nearby:
            elapsed = round(median(r['inference_seconds'] + r['load_seconds'] for r in nearby))
            raw_memory = max(measured['memory_raw_gib'], max(r['peak_process_gib'] for r in nearby))
            peak = round(raw_memory * 1.12 + 1, 1)
            measured.update(memory_raw_gib=raw_memory, setup_seconds=median(r['load_seconds'] for r in nearby),
                            inference_seconds=median(r['inference_seconds'] for r in nearby))
            source = 'observed'
    elif compatible:
        row=compatible[-1]
        elapsed=round(row['inference_seconds']*duration/row['audio_seconds']+row['load_seconds'])
        peak=max(peak,round(row['peak_process_gib']*1.12+1,1))
        source='observed'
    result = dict(pipeline=key,batch=actual,requested_batch=batch,recommended_batch=actual,
        chunk_count=chunks,chunk_seconds=seconds,memory_expected_gib=peak,memory_high_gib=peak,
        capacity_gib=capacity,free_gib=free,budget_gib=round(capacity*.7,1) if capacity else None,
        exceeds_budget=bool(capacity and peak>capacity*.7),exceeds_capacity=bool(capacity and peak>capacity),
        exceeds_free=bool(free is not None and peak>free),capacity_kind='VRAM',time_seconds=elapsed,
        time_source=source,memory_source='reference-plus-headroom')
    if measured:
        result.update({k:v for k,v in measured.items() if k not in {'time_seconds','memory_expected_gib','memory_high_gib','time_source'}})
    return result


def attribute_speakers(items, turns):
    """Keep ambiguous words unassigned, including zero-duration alignment words.

    A unique active voice at a point can identify a zero-duration word. A nearby
    voice on either side is not evidence. Text and timestamps are never changed.
    """
    result = []
    for item in items:
        row = dict(item)
        start, end = float(row['start']), float(row['end'])
        scores = {}
        if end > start:
            for turn in turns:
                overlap = max(0, min(end, turn['end']) - max(start, turn['start']))
                if overlap:
                    label = str(turn['speaker'])
                    scores[label] = scores.get(label, 0) + overlap
        else:
            active = {str(t['speaker']) for t in turns if t['start'] <= start < t['end']}
            if len(active) == 1:
                scores[next(iter(active))] = 1
        winner = max(scores, key=scores.get) if scores else None
        share = scores[winner] / sum(scores.values()) if winner else 0
        row.update(speaker=winner if share >= .85 else '미확인', speaker_share=round(share, 4),
                   speaker_uncertain=winner is None or share < .85)
        result.append(row)
    labels = sorted({str(t['speaker']) for t in turns})
    mapping = {label: f'S{index + 1:02d}' for index, label in enumerate(labels)}
    for row in result:
        row['speaker'] = mapping.get(row['speaker'], '미확인')
    return result


def utterances(items, *, max_seconds=18):
    """Group aligned Korean tokens for editing, retaining the underlying words."""
    rows = []
    for item in items:
        if not item.get('text', '').strip():
            continue
        same = (rows and rows[-1]['speaker'] == item['speaker']
                and bool(rows[-1].get('speaker_uncertain')) == bool(item.get('speaker_uncertain'))
                and item['start'] - rows[-1]['end'] <= 1.2
                and item['end'] - rows[-1]['start'] <= max_seconds
                and not rows[-1]['text'].rstrip().endswith(('.', '?', '!', '。', '？', '！')))
        if same:
            rows[-1]['text'] += ' ' + item['text'].strip()
            rows[-1]['end'] = max(rows[-1]['end'], item['end'])
        else:
            rows.append({key: item[key] for key in ('start', 'end', 'speaker', 'text', 'speaker_uncertain')})
    return [{**row, 'id': index} for index, row in enumerate(rows)]
