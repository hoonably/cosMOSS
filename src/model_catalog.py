"""Mac model variants, CUDA speech presets and conservative memory profiles."""
from __future__ import annotations
import re
import platform
import subprocess
from functools import lru_cache
from src.speech_pipeline import PIPELINES, DEFAULT_PIPELINE

ORIGINAL = 'OpenMOSS-Team/MOSS-Transcribe-Diarize'
MLX_8BIT = 'vanch007/mlx-MOSS-Transcribe-Diarize-8bit'
MODELS = [
    dict(key='mlx-8bit', name='MOSS 0.9B · MLX 8bit', backend='mlx', repo=MLX_8BIT, bytes=1258427152, precision='auto', description='텍스트 디코더 8비트 · Mac 기본 추천'),
    dict(key='mlx-4bit', name='MOSS 0.9B · MLX 4bit', backend='mlx', repo='vanch007/mlx-MOSS-Transcribe-Diarize-4bit', bytes=960434705, precision='auto', description='위험 · 실측에서 시간표시 오류와 긴 녹음의 출력 중단 확인'),
    dict(key='mlx-16bit', name='MOSS 0.9B · MLX 원본 16bit', backend='mlx', repo='vanch007/mlx-MOSS-Transcribe-Diarize', bytes=1817113512, precision='auto', description='원본 정밀도 · 메모리 사용량 증가'),
    dict(key='cuda-original', name='MOSS 0.9B · 원본 BF16 / FP16', backend='cuda', repo=ORIGINAL, bytes=1817113576, precision='auto', description='GPU가 지원하면 BF16, 그 외 FP16'),
]
DEFAULT_OPTIONS = dict(chunk_minutes=30, bridge_seconds=30, max_tokens=32768,
                       kv_bits=8, prefill_step_size=512, precision='auto', attention='sdpa', use_cache=True)


def has_model_selection(device):
    # Old registrations automatically chose MOSS without a model_key. Keep
    # explicit legacy choices, but don't count registration as model selection.
    return device.get('model_selected', bool(device.get('model_key') or device.get('options', {}).get('speech_pipeline')))


def default_chunk_minutes(backend, memory_gib=None):
    # Leave room for driver-reserved VRAM on nominal 48/80 GB cards.
    if backend != 'cuda' or memory_gib is None:
        return 30
    return 90 if memory_gib >= 72 else 60 if memory_gib >= 40 else 30


def model_selection(backend, key='', repo=''):
    if key == 'custom':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', repo) or '--' in repo or '..' in repo or any(part.endswith(('.', '-')) for part in repo.split('/')):
            raise ValueError('올바른 Hugging Face 모델 ID를 입력해 주세요.')
        return dict(key=key, backend=backend, repo=repo, precision='auto', name=repo)
    if not key:
        key = 'mlx-8bit' if backend == 'mlx' else 'cuda-original'
    selected = next((m for m in MODELS if m['key'] == key), None)
    if not selected or selected['backend'] != backend:
        raise ValueError('이 장치의 실행 방식에 맞는 모델을 선택해 주세요.')
    return selected.copy()


def recommendation(backend, memory_gib=None):
    options = DEFAULT_OPTIONS.copy()
    if backend == 'mlx':
        low = memory_gib is not None and memory_gib < 16
        key = 'mlx-4bit' if low else 'mlx-8bit'
        options.update(prefill_step_size=256 if low else 512)
    else:
        key = 'cuda-original'
        options['speech_pipeline'] = DEFAULT_PIPELINE
    options['chunk_minutes'] = default_chunk_minutes(backend, memory_gib)
    return dict(model_key=key, options=options)


@lru_cache(maxsize=1)
def local_hardware():
    if platform.system() != 'Darwin':
        return dict(name=platform.machine(), memory_gib=None)
    try:
        memory = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=2)) / 1024**3
        name = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True, timeout=2).strip()
        return dict(name=name, memory_gib=round(memory, 1))
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(name='Apple Silicon', memory_gib=None)


def catalog(backend, hardware=None):
    hardware = hardware or (local_hardware() if backend == 'mlx' else {})
    profiles = []
    if backend == 'cuda':
        for gpu in hardware.get('gpus', []):
            memory = gpu.get('memory_gib')
            profiles.append(dict(name=gpu['name'], index=gpu.get('index'), memory_gib=memory, **recommendation(backend, memory)))
        memories = [p['memory_gib'] for p in profiles if p['memory_gib'] is not None]
        memory = min(memories) if memories else None
    else:
        memory = hardware.get('memory_gib')
    return dict(models=[{**m, 'supported': m['backend'] == backend} for m in MODELS], speech_presets=PIPELINES,
                hardware=hardware, profiles=profiles, recommendation=recommendation(backend, memory),
                unavailable=[dict(name='MOSS-Transcribe-preview-2B', reason='영어 ASR 전용 · 화자 분리 어댑터 미지원'),
                             dict(name='GGUF / CoreML / ONNX 변환본', reason='별도 실행 엔진 필요')])
