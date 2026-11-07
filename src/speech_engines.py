"""Pinned, independently installed engines for the measured server presets."""
from __future__ import annotations

ENGINE_VERSION = 'speech-engines-sg019-qwen006-nemo5202-v1'
ENGINE_PIPELINES = {'moss-sglang', 'qwen-transformers', 'moss-qwen-sequential'}


def transcribe_qwen_adaptively(parts, infer, parse, on_progress, *, batch, sample_rate=16000, min_seconds=5):
    """Keep successful chunks; split only truncated/repeating chunks without gaps."""
    from collections import Counter, deque
    import math

    if batch < 1 or sample_rate < 1 or min_seconds <= 0:
        raise ValueError('Invalid Qwen chunk settings')
    pending = deque(parts)
    chunks, retries = [], []
    minimum = max(1, math.ceil(min_seconds * sample_rate))
    while pending:
        selected = [pending.popleft() for _ in range(min(batch, len(pending)))]
        on_progress(chunk_count=len(chunks) + len(selected) + len(pending), completed=len(chunks))
        results = infer(selected)
        if len(results) != len(selected):
            raise RuntimeError('Qwen 구간별 출력 개수가 일치하지 않습니다. 불완전한 전사를 저장하지 않았습니다.')
        split_parts = []
        for (audio, offset), (raw, audit) in zip(selected, results):
            ids = audit['token_ids']
            repeating = len(ids) > 320 and max(Counter(tuple(ids[i:i+64]) for i in range(len(ids)-63)).values(), default=0) >= 5
            reason = 'length' if audit['finish_reason'] != 'stop' else 'repetition' if repeating else None
            if reason:
                end = offset + len(audio) / sample_rate
                if len(audio) < 2 * minimum:
                    raise RuntimeError(f'Qwen {offset:.1f}–{end:.1f}초 구간에서 '
                                       '출력 한도·반복 문제가 지속됩니다. 불완전한 전사를 저장하지 않았습니다.')
                # Search short windows near the midpoint, so boundaries favor
                # pauses while both children remain large enough to recognize.
                middle = len(audio) // 2
                radius, window = 2 * sample_rate, max(1, sample_rate // 50)
                low, high = max(minimum, middle-radius), min(len(audio)-minimum, middle+radius)
                candidates = range(low, high+1, window)
                cut = min(candidates, key=lambda at: (
                    sum(float(x)**2 for x in audio[at:at+window]), abs(at-middle)))
                split_parts.extend([(audio[:cut], offset), (audio[cut:], offset+cut/sample_rate)])
                retries.append(dict(offset=float(offset), end=float(end), reason=reason, generated_tokens=len(ids)))
                continue
            language, text = parse(raw)
            chunks.append(dict(offset=float(offset), end=float(offset+len(audio)/sample_rate),
                               text=text, language=language, **audit))
        pending.extendleft(reversed(split_parts))
        if split_parts:
            on_progress(chunk_count=len(chunks)+len(pending), completed=len(chunks), retrying=True,
                        retry_offset=retries[-1]['offset'], retry_end=retries[-1]['end'])
    chunks.sort(key=lambda chunk: chunk['offset'])
    on_progress(chunk_count=len(chunks), completed=len(chunks))
    return chunks, retries


MODELS = {
    'moss': ('OpenMOSS-Team/MOSS-Transcribe-Diarize', '704aa4a9c304e8520be88901e0d1960158ef5b15'),
    'qwen': ('Qwen/Qwen3-ASR-1.7B', '7278e1e70fe206f11671096ffdd38061171dd6e5'),
    'aligner': ('Qwen/Qwen3-ForcedAligner-0.6B', 'c7cbfc2048c462b0d63a45797104fc9db3ad62b7'),
    'nemotron': ('nvidia/Nemotron-3-Diarization', 'f667ed73aee57d40cc39428eb768b4fd87a0a29e'),
}
REQUIREMENTS = {
    'sglang': ['sglang-omni==0.1.9', 'sglang==0.5.22', 'torch==2.14.1',
               'transformers==5.19.0', 'nvidia-cutlass-dsl==4.8.0',
               # Match Torch's CUDA 13.0 headers and keep the PTX producer and assembler together.
               'nvidia-cuda-nvcc==13.0.88', 'nvidia-nvvm==13.0.88',
               'nvidia-cuda-runtime==13.0.96', 'nvidia-cuda-crt==13.0.88',
               'nvidia-cuda-cccl==13.0.85', 'ninja==1.13.0'],
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


def moss_output_prefix(text, duration):
    import math
    import re
    rows, cursor = [], 0
    for match in re.finditer(r'\[([\d.]+)\]\[(S\d+)\](.*?)\[([\d.]+)\]', text, re.S):
        if text[cursor:match.start()].strip():
            raise RuntimeError('MOSS 전사 중간의 시간 표시를 읽지 못했습니다.')
        start, speaker, body, end = match.groups()
        a, b = float(start), float(end)
        if math.isfinite(a) and math.isfinite(b) and 0 <= a <= b and a < duration and body.strip():
            rows.append(dict(start=a, end=min(b, duration), text=body.strip(), native_speaker=speaker))
        cursor = match.end()
    if text.strip() and not rows:
        raise RuntimeError('MOSS 결과의 시간 표시를 읽지 못했습니다. 원문은 보존하고 작업을 중단했습니다.')
    return rows, text[cursor:].strip()


def moss_segments(text, duration):
    rows, trailing = moss_output_prefix(text, duration)
    if trailing:
        raise RuntimeError('MOSS 출력이 시간 표시 또는 문장 중간에서 끊겼습니다.')
    return rows


def transcribe_moss_continuations(duration, infer, on_progress, *, start_offset=0, max_attempts=16):
    """Preserve the full-context prefix and finish only an incomplete suffix."""
    import math
    if not math.isfinite(duration) or not 0 <= start_offset < duration:
        raise ValueError('Invalid MOSS audio duration')
    offset, items, chunks = start_offset, [], []
    for attempt in range(max_attempts):
        remaining = duration-offset
        maximum = max(512, min(65536, math.ceil(remaining*20)))
        on_progress(chunk_count=attempt+1, completed=attempt, retrying=attempt>0, resume_offset=offset)
        response = infer(offset, maximum)
        text = response.get('text', '')
        rows, trailing = moss_output_prefix(text, remaining)
        usage = response.get('usage') or {}
        finish = response.get('finish_reason')
        finish = finish.get('type') if isinstance(finish, dict) else finish
        truncated = bool(trailing or finish in {'length','max_tokens','max_new_tokens','max_length'}
                         or usage.get('completion_tokens', 0) >= maximum)
        shifted = [{**row, 'start':row['start']+offset, 'end':row['end']+offset} for row in rows]
        if any(row['end']-row['start'] < 0 for row in shifted):
            raise RuntimeError('MOSS 구간 시각이 잘못되었습니다.')
        items.extend(shifted)
        chunks.append(dict(offset=offset, end=duration, text=text, truncated=truncated,
                           transcribed_end=max((row['end'] for row in shifted), default=offset),
                           requested_max_new_tokens=maximum, usage=response.get('usage'), finish_reason=finish))
        if not truncated:
            return items, chunks
        next_offset = max((row['end'] for row in shifted), default=offset)
        if next_offset >= duration-0.02:
            return items, chunks
        if next_offset-offset < 1:
            raise RuntimeError('MOSS 잘린 출력에서 재시작할 위치를 확인하지 못했습니다.')
        offset = next_offset
    raise RuntimeError('MOSS 남은 음성 재시도 횟수를 초과했습니다. 불완전한 전사를 저장하지 않았습니다.')


def missing_moss_tail(items, turns):
    """Use detected speech, rather than trailing silence, to check completion."""
    last_text = max((row['end'] for row in items), default=0)
    last_voice = max((row['end'] for row in turns), default=0)
    voiced = sum(max(0, row['end']-max(last_text+1, row['start'])) for row in turns)
    return last_text if last_voice-last_text > 5 and voiced >= 3 else None


def balanced_moss_ranges(duration):
    """Use the fewest <=90-minute requests, with equally sized contexts."""
    import math
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid MOSS audio duration')
    count = math.ceil(duration / 5400)
    return [(duration*i/count, duration*(i+1)/count) for i in range(count)]


def transcribe_moss_balanced(duration, infer, on_progress, *, allow_split=False, start_offset=0, end_offset=None):
    ranges = balanced_moss_ranges(duration)
    if len(ranges) > 1 and not allow_split:
        raise ValueError('90분을 넘는 MOSS 녹음은 균등 분할 확인이 필요합니다.')
    end_offset = duration if end_offset is None else end_offset
    if not 0 <= start_offset < end_offset <= duration:
        raise ValueError('Invalid MOSS audio range')
    items, chunks = [], []
    for index, (a, b) in enumerate(ranges):
        a, b = max(a, start_offset), min(b, end_offset)
        if a >= b:
            continue
        def progress(**event):
            event.update(partition_index=index+1, partition_count=len(ranges),
                         chunk_count=len(ranges), completed=index,
                         resume_offset=event.get('resume_offset', 0)+a)
            on_progress(**event)
        rows, audit = transcribe_moss_continuations(b-a,
            lambda offset, maximum: infer(a+offset, b, maximum), progress)
        items.extend({**row, 'start':row['start']+a, 'end':row['end']+a} for row in rows)
        chunks.extend({**chunk, 'offset':chunk['offset']+a, 'end':b,
                       'transcribed_end':chunk['transcribed_end']+a,
                       'partition_index':index+1, 'partition_count':len(ranges)} for chunk in audit)
        on_progress(partition_index=index+1, partition_count=len(ranges),
                    chunk_count=len(ranges), completed=index+1)
    return items, chunks


def missing_moss_partition_tail(items, turns, duration):
    for start, end in balanced_moss_ranges(duration):
        rows = [{'end':start}] + [{'end':min(row['end'],end)} for row in items
                                if row['start'] < end and row['end'] > start]
        speech = [dict(start=max(turn['start'],start),end=min(turn['end'],end)) for turn in turns
                  if turn['start'] < end and turn['end'] > start]
        offset = missing_moss_tail(rows, speech)
        if offset is not None:
            return offset, end
    return None
