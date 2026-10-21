"""Isolated CUDA diarization; no transcript generation or edits happen here."""
from __future__ import annotations

import json
import os
import resource
import sys
import time
import threading
from pathlib import Path

MODELS = {
    'nemotron-3': ('nvidia/Nemotron-3-Diarization', 'f667ed73aee57d40cc39428eb768b4fd87a0a29e'),
    'pyannote-community-1': ('pyannote/speaker-diarization-community-1', '3533c8cf8e369892e6b79ff1bf80f7b0286a54ee'),
}


def model_files(key):
    return (['config.json', 'processor_config.json', 'model.safetensors'] if key == 'nemotron-3'
            else ['config.yaml', 'embedding/*', 'segmentation/*', 'plda/*'])


def emit(**event):
    print('COSMOSS_DIARIZATION:' + json.dumps(event, ensure_ascii=False), flush=True)


def setup(key):
    from huggingface_hub import get_token, snapshot_download
    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, LocalEntryNotFoundError
    repo, revision = MODELS[key]
    try:
        return snapshot_download(repo, revision=revision, allow_patterns=model_files(key), local_files_only=True)
    except (OSError, ValueError, LocalEntryNotFoundError):
        pass
    if key == 'pyannote-community-1' and not get_token():
        emit(phase='auth_required', error='서버에서 Hugging Face에 먼저 로그인해 주세요. hf auth login', auth_url='https://huggingface.co/settings/tokens')
        return None
    try:
        emit(phase='downloading')
        return snapshot_download(repo, revision=revision, allow_patterns=model_files(key), local_files_only=False)
    except GatedRepoError:
        emit(phase='auth_required', error='서버의 Hugging Face 계정으로 Community-1 이용 조건에 동의한 뒤 다시 준비해 주세요.',
             auth_url='https://huggingface.co/pyannote/speaker-diarization-community-1')
        return None
    except HfHubHTTPError as error:
        if error.response is not None and error.response.status_code in {401, 403}:
            emit(phase='auth_required', error='서버에서 Hugging Face 로그인을 다시 확인해 주세요. hf auth login',
                 auth_url='https://huggingface.co/settings/tokens')
            return None
        raise


def main(request):
    from process_lifecycle import arm_worker_parent_guard
    arm_worker_parent_guard()
    if os.environ.get('COSMOSS_DIARIZATION_PARENT_PID'):
        parent = int(os.environ['COSMOSS_DIARIZATION_PARENT_PID'])
        def watch_parent():
            while os.getppid() == parent: time.sleep(.5)
            os._exit(1)
        threading.Thread(target=watch_parent, daemon=True).start()
    key = request['model_key']
    if request['action'] == 'install':
        path = setup(key)
        if path:
            emit(phase='installed', model_key=key, revision=MODELS[key][1], model_path=path)
        return
    import numpy as np
    import torch
    import wave
    from huggingface_hub import snapshot_download
    torch.set_num_threads(4)
    repo, revision = MODELS[key]
    path = snapshot_download(repo, revision=revision, allow_patterns=model_files(key), local_files_only=True)
    with wave.open(request['audio'], 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono 16 kHz PCM16 audio')
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
    device = request.get('device', 'cuda')
    if device == 'auto': device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    def synchronize():
        if device == 'cuda': torch.cuda.synchronize()
        elif device == 'mps': torch.mps.synchronize()
    if device == 'cuda':
        torch.cuda.init()
        torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    emit(phase='loading')
    if key == 'pyannote-community-1':
        from pyannote.audio import Pipeline
        model = Pipeline.from_pretrained(path).to(torch.device(device))
    else:
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor
        processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        # Recorded audio can use the published 30.4 s buffer, rather than the
        # low-latency 1.04 s streaming default. Speaker cache spans the recording.
        processor.streaming_modes['offline'] = (340, 40)
        processor.set_streaming_mode(request.get('mode', 'offline'))
        dtype = torch.bfloat16 if request.get('precision') == 'bf16' else torch.float32
        model = AutoModelForAudioFrameClassification.from_pretrained(path, local_files_only=True, dtype=dtype).to(device).eval()
    synchronize()
    loaded = time.perf_counter()
    emit(phase='running')
    with torch.inference_mode():
        if key == 'pyannote-community-1':
            options = {'num_speakers': request['num_speakers']} if request.get('num_speakers') else {}
            output = model({'waveform': torch.from_numpy(audio).unsqueeze(0), 'sample_rate': 16000}, **options)
            turns = [{'start': float(turn.start), 'end': float(turn.end), 'speaker': speaker}
                     for turn, speaker in output.speaker_diarization]
        else:
            cache, logits, frame, first = None, [], 0, True
            while True:
                start = 0 if first else processor.audio_chunk_start(frame)
                size = processor.num_samples_first_audio_chunk if first else processor.num_samples_per_audio_chunk
                last = start + size >= len(audio)
                inputs = processor(audio[start:start + size], sampling_rate=16000, is_streaming=True,
                                   is_first_audio_chunk=first, is_last_audio_chunk=last)
                inputs = inputs.to(model.device, dtype=model.dtype)
                output = model(**inputs, speaker_cache=cache)
                logits.append(output.logits.cpu())
                cache = output.speaker_cache
                if last:
                    break
                frame += processor.num_mel_frames_per_step
                first = False
            values = processor.extract_speaker_dict(torch.cat(logits, dim=1))[0]
            turns = [{'start': float(value['Start']), 'end': float(value['End']), 'speaker': str(value['Speaker'])} for value in values]
    synchronize()
    finished = time.perf_counter()
    result = {'model_key': key, 'model_id': repo, 'revision': revision, 'turns': turns,
              'speaker_count': len({turn['speaker'] for turn in turns}), 'duration': len(audio) / 16000,
              'load_seconds': loaded - started, 'inference_seconds': finished - loaded,
              'total_seconds': finished - started, 'peak_allocated_gib': torch.cuda.max_memory_allocated() / 1024**3 if device == 'cuda' else None,
              'peak_reserved_gib': torch.cuda.max_memory_reserved() / 1024**3 if device == 'cuda' else None,
              'peak_host_rss_gib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3 if sys.platform == 'darwin' else 1024**2),
              'device': device, 'gpu_name': torch.cuda.get_device_name() if device == 'cuda' else 'Apple Silicon · MPS' if device == 'mps' else 'CPU', 'mode': request.get('mode', 'offline'),
              'precision': 'fp32' if key == 'pyannote-community-1' else request.get('precision', 'fp32'),
              'num_speakers_requested': request.get('num_speakers')}
    Path(request['result']).write_text(json.dumps(result, ensure_ascii=False))
    emit(phase='done', metrics={key: value for key, value in result.items() if key != 'turns'})


if __name__ == '__main__':
    try:
        main(json.loads(Path(sys.argv[1]).read_text()))
    except Exception as error:
        emit(phase='error', error=str(error)[-1200:])
        raise
