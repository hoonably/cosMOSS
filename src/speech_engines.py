"""Pinned, independently installed engines for the measured server presets."""
from __future__ import annotations

ENGINE_VERSION = 'speech-engines-sg019-qwen006-nemo5202-v1'
ENGINE_PIPELINES = {'moss-sglang', 'qwen-transformers', 'moss-qwen-sequential'}
MODELS = {
    'moss': ('OpenMOSS-Team/MOSS-Transcribe-Diarize', '704aa4a9c304e8520be88901e0d1960158ef5b15'),
    'qwen': ('Qwen/Qwen3-ASR-1.7B', '7278e1e70fe206f11671096ffdd38061171dd6e5'),
    'aligner': ('Qwen/Qwen3-ForcedAligner-0.6B', 'c7cbfc2048c462b0d63a45797104fc9db3ad62b7'),
    'nemotron': ('nvidia/Nemotron-3-Diarization', 'f667ed73aee57d40cc39428eb768b4fd87a0a29e'),
}
REQUIREMENTS = {
    'sglang': ['sglang-omni==0.1.9', 'sglang==0.5.22', 'torch==2.14.1',
               'transformers==5.19.0', 'nvidia-cutlass-dsl==4.8.0',
               'nvidia-cuda-nvcc==13.4.92', 'nvidia-cuda-crt==13.4.92',
               'nvidia-cuda-cccl==13.3.4.3.1'],
    'qwen': ['qwen-asr==0.0.6', 'torch==2.9.1', 'torchaudio==2.9.1',
             'transformers==4.57.6', 'nvidia-cutlass-dsl==4.2.1',
             'https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3%2Bcu12torch2.9cxx11abiTRUE-cp312-cp312-linux_x86_64.whl'],
    'nemotron': ['torch==2.8.0+cu128', 'torchaudio==2.8.0+cu128', 'transformers==5.20.2',
                 'numpy==2.2.6', 'soundfile==0.13.1', 'librosa==0.11.0', 'accelerate==1.11.0'],
}


def stages(key):
    if key == 'moss-sglang':
        return [('moss', 'sglang'), ('nemotron', 'nemotron')]
    if key == 'qwen-transformers':
        return [('qwen', 'qwen'), ('aligner', 'qwen'), ('nemotron', 'nemotron')]
    if key == 'moss-qwen-sequential':
        return [('moss', 'sglang'), ('qwen', 'qwen'), ('nemotron', 'nemotron')]
    raise ValueError('전사 방식을 다시 선택해 주세요.')


def model_keys(key):
    return [stage for stage, _ in stages(key)]


def runtime_for(key):
    return ENGINE_VERSION if key in ENGINE_PIPELINES else 'speech-torch280-transformers5202-moss-e3bdd93f-v1'


def moss_segments(text, duration):
    import math
    import re
    rows = []
    for start, speaker, body, end in re.findall(r'\[([\d.]+)\]\[(S\d+)\](.*?)\[([\d.]+)\]', text, re.S):
        a, b = float(start), float(end)
        if math.isfinite(a) and math.isfinite(b) and 0 <= a <= b and a < duration and body.strip():
            rows.append(dict(start=a, end=min(b, duration), text=body.strip(), native_speaker=speaker))
    if text.strip() and not rows:
        raise RuntimeError('MOSS 결과의 시간 표시를 읽지 못했습니다. 원문은 보존하고 작업을 중단했습니다.')
    return rows
