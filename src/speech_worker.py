"""Finite, native-precision CUDA ASR + alignment + Nemotron job.

Downloaded models and the isolated runtime are prepared before acquiring a GPU.
The bootstrap owns the GPU lock, lease, private input and process group.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
import wave

from speech_pipeline import MODELS, RUNTIME_VERSION, attribute_speakers, utterances


def emit(**event):
    print('COSMOSS_SPEECH:' + json.dumps(event, ensure_ascii=False), flush=True)


def model_keys(pipeline):
    return ('moss', 'nemotron') if pipeline == 'moss-batch' else ('qwen', 'aligner', 'nemotron')


def model_files(key):
    if key == 'nemotron':
        return ['config.json', 'processor_config.json', 'model.safetensors']
    return ['*.json', '*.safetensors', '*.py', '*.txt', '*.jinja', '*.model']


def prepare(pipeline, *, offline):
    from huggingface_hub import snapshot_download
    return {key: snapshot_download(MODELS[key][0], revision=MODELS[key][1],
                                  allow_patterns=model_files(key), local_files_only=offline)
            for key in model_keys(pipeline)}


def quiet_chunks(audio, sr=16000, seconds=180):
    """Near-three-minute low-energy cuts, without dropped or duplicated samples."""
    import numpy as np
    start, chunks = 0, []
    maximum, search, window = int(seconds * sr), 5 * sr, int(.1 * sr)
    while len(audio) - start > maximum:
        center = start + maximum
        left, right = max(start, center - search), min(len(audio), center + search)
        region = np.abs(audio[left:right])
        sums = np.convolve(region, np.ones(window, dtype=np.float32), mode='valid')
        low = int(np.argmin(sums))
        boundary = left + low + int(np.argmin(region[low:low + window]))
        boundary = min(len(audio), max(start + 1, boundary))
        chunks.append((audio[start:boundary], start / sr))
        start = boundary
    chunks.append((audio[start:], start / sr))
    return chunks


def main(request):
    from process_lifecycle import arm_worker_parent_guard
    arm_worker_parent_guard()
    key = request['model_key']
    if request['action'] == 'install':
        emit(phase='downloading')
        prepare(key, offline=False)
        emit(phase='installed')
        return
    process_started = time.perf_counter()
    import numpy as np
    import torch
    import transformers
    from transformers import AutoProcessor, AutoModelForAudioFrameClassification
    torch.set_num_threads(4)
    torch.manual_seed(0)
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('선택한 CUDA GPU 하나를 확인하지 못했습니다.')
    torch.cuda.init()
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    paths = prepare(key, offline=True)
    with wave.open(request['audio'], 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Expected mono 16 kHz PCM16 audio')
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
    if len(audio) < 1:
        raise ValueError('빈 음성은 전사할 수 없습니다.')
    duration = len(audio) / 16000
    emit(phase='loading')
    nprocessor = AutoProcessor.from_pretrained(paths['nemotron'], local_files_only=True)
    nprocessor.streaming_modes['offline'] = (340, 40)
    nprocessor.set_streaming_mode('offline')
    nmodel = AutoModelForAudioFrameClassification.from_pretrained(paths['nemotron'], local_files_only=True,
        dtype=torch.float32, attn_implementation='sdpa').to('cuda').eval()
    if key == 'moss-batch':
        from transformers import AutoModelForCausalLM
        from moss_transcribe_diarize.inference_utils import build_transcription_messages
        model = AutoModelForCausalLM.from_pretrained(paths['moss'], trust_remote_code=True, local_files_only=True,
            dtype=dtype, attn_implementation='sdpa').to('cuda').eval()
        model.generation_config.use_cache = True
        model.generation_config.cache_implementation = None
        processor = AutoProcessor.from_pretrained(paths['moss'], trust_remote_code=True, local_files_only=True)
    else:
        from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification
        model = AutoModelForMultimodalLM.from_pretrained(paths['qwen'], local_files_only=True,
            dtype=dtype, attn_implementation='sdpa').to('cuda').eval()
        processor = AutoProcessor.from_pretrained(paths['qwen'], local_files_only=True)
        processor.tokenizer.padding_side = 'left'
        aligner = AutoModelForTokenClassification.from_pretrained(paths['aligner'], local_files_only=True,
            dtype=dtype, attn_implementation='sdpa').to('cuda').eval()
        aprocessor = AutoProcessor.from_pretrained(paths['aligner'], local_files_only=True)
        # Initialize the short-input kernels/tokenizers before the large batch,
        # just as in the measured benchmark. No warmup text is ever saved.
        warm = processor.apply_transcription_request(audio=[audio[:160000]],
            language=[request.get('language') or None],
            processor_kwargs={'padding': True, 'return_tensors': 'pt'}).to('cuda', dtype)
        with torch.inference_mode():
            warm_ids = model.generate(**warm, max_new_tokens=256, do_sample=False, use_cache=True)
        warm_text = processor.decode(warm_ids[:, warm['input_ids'].shape[1]:], return_format='parsed')[0]
        del warm, warm_ids
        if warm_text['transcription'].strip():
            warm_align, _ = aprocessor.prepare_forced_aligner_inputs(audio=[audio[:160000]],
                transcript=[warm_text['transcription']], language=[request.get('language') or warm_text.get('language') or 'Korean'],
                processor_kwargs={'padding': True, 'return_tensors': 'pt'})
            with torch.inference_mode():
                warm_output = aligner(**warm_align.to('cuda', dtype))
            del warm_align, warm_output
    torch.cuda.synchronize()
    # Loading three models can leave temporary allocator blocks behind. They
    # are not useful KV buffers and mustn't inflate the whole job's footprint.
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    loaded = time.perf_counter()
    torch.cuda.reset_peak_memory_stats()
    chunks = quiet_chunks(audio)
    batch = min(request['batch'], len(chunks))
    items, text_chunks = [], []
    memory_phases = {}
    align_seconds = 0
    emit(phase='transcribing', chunk_count=len(chunks), batch=batch)
    for begin in range(0, len(chunks), batch):
        current = chunks[begin:begin + batch]
        arrays = [np.pad(a, (0, max(0, 8000 - len(a)))) for a, _ in current]
        if key == 'moss-batch':
            prompts = [processor.apply_chat_template(build_transcription_messages('input.wav',
                **({'prompt': request['prompt']} if request.get('prompt') else {})),
                tokenize=False, add_generation_prompt=True) for _ in arrays]
            with torch.amp.autocast('cuda', dtype=dtype):
                inputs = processor(text=prompts, audio=arrays, max_length=131072,
                    audio_kwargs={'device': 'cuda:0'}, return_tensors='pt').to('cuda')
            # The custom processor right-pads. Move both tokens and masks to
            # the left without changing audio-feature order or lengths.
            for row in range(len(arrays)):
                valid = inputs['input_ids'][row][inputs['attention_mask'][row].bool()].clone()
                inputs['input_ids'][row].fill_(processor.tokenizer.pad_token_id)
                inputs['input_ids'][row, -len(valid):] = valid
                inputs['attention_mask'][row].zero_()
                inputs['attention_mask'][row, -len(valid):] = 1
            maximum = 4096
            with torch.inference_mode(), torch.amp.autocast('cuda', dtype=dtype):
                generated = model.generate(**inputs, max_new_tokens=maximum, do_sample=False, use_cache=True)
            generated = generated[:, inputs['input_ids'].shape[1]:]
            texts = [processor.tokenizer.decode(row, skip_special_tokens=True).strip() for row in generated]
            for (array, offset), text in zip(current, texts):
                parsed = re.findall(r'\[([\d.]+)\]\[(S\d+)\](.*?)\[([\d.]+)\]', text, re.S)
                if text.strip() and not parsed:
                    raise RuntimeError('MOSS 결과의 시간 표시를 해석하지 못했습니다. 긴 문맥 방식으로 다시 시도해 주세요.')
                for start, speaker, body, end in parsed:
                    a, b = float(start) + offset, min(float(end) + offset, offset + len(array) / 16000, duration)
                    if body.strip() and 0 <= a <= b:
                        items.append({'start': a, 'end': b, 'text': body.strip(), 'native_speaker': speaker})
                text_chunks.append({'offset': offset, 'text': text})
        else:
            inputs = processor.apply_transcription_request(audio=arrays,
                language=[request.get('language') or None] * len(arrays),
                prompt=request.get('hotwords') or None,
                processor_kwargs={'padding': True, 'return_tensors': 'pt'}).to('cuda', dtype)
            maximum = 2048
            with torch.inference_mode():
                generated = model.generate(**inputs, max_new_tokens=maximum, do_sample=False, use_cache=True)
            generated = generated[:, inputs['input_ids'].shape[1]:]
            parsed = processor.decode(generated, return_format='parsed')
            memory_phases['asr_reserved_gib'] = torch.cuda.max_memory_reserved() / 1024**3
            if generated.shape[1] >= maximum:
                raise RuntimeError('출력 길이 한도에 도달했습니다. 긴 문맥 방식으로 다시 시도해 주세요.')
            # ASR and aligner use different tensor shapes. Release ASR buffers
            # before alignment so the allocator needn't retain both footprints.
            del inputs, generated
            torch.cuda.empty_cache()
            active = [(array, offset, value) for (array, offset), value in zip(current, parsed) if value['transcription'].strip()]
            text_chunks.extend({'offset': offset, 'text': value['transcription']} for (_, offset), value in zip(current, parsed))
            if active:
                emit(phase='aligning', completed=begin, chunk_count=len(chunks))
                torch.cuda.synchronize()
                align_started = time.perf_counter()
                align_inputs, word_lists = aprocessor.prepare_forced_aligner_inputs(
                    audio=[np.pad(a, (0, max(0, 8000 - len(a)))) for a, _, _ in active],
                    transcript=[v['transcription'] for _, _, v in active],
                    language=[request.get('language') or v.get('language') or 'Korean' for _, _, v in active],
                    processor_kwargs={'padding': True, 'return_tensors': 'pt'})
                align_inputs = align_inputs.to('cuda', dtype)
                with torch.inference_mode():
                    values = aligner(**align_inputs)
                aligned = aprocessor.decode_forced_alignment(logits=values.logits, input_ids=align_inputs['input_ids'],
                    word_lists=word_lists, timestamp_token_id=aligner.config.timestamp_token_id)
                for (array, offset, value), words in zip(active, aligned):
                    chunk_end = min(duration, offset + len(array) / 16000)
                    for word in words:
                        start = min(chunk_end, max(offset, offset + float(word['start_time'])))
                        end = min(chunk_end, max(start, offset + float(word['end_time'])))
                        items.append({'start': start, 'end': end, 'text': word['text']})
                    if value['transcription'].strip() and not words:
                        # Keep the whole ASR text if alignment produced no words.
                        items.append({'start': offset, 'end': chunk_end, 'text': value['transcription'], 'unaligned': True})
                torch.cuda.synchronize()
                align_seconds += time.perf_counter() - align_started
                memory_phases['aligned_reserved_gib'] = torch.cuda.max_memory_reserved() / 1024**3
                del align_inputs, values
        if key == 'moss-batch':
            if generated.shape[1] >= maximum:
                raise RuntimeError('출력 길이 한도에 도달했습니다. 잘린 결과를 저장하지 않았습니다. 긴 문맥 방식으로 다시 시도해 주세요.')
            del inputs, generated
        emit(phase='transcribing', completed=begin + len(current), chunk_count=len(chunks))
    torch.cuda.synchronize()
    asr_finished = time.perf_counter()
    emit(phase='diarizing', completed=len(chunks), chunk_count=len(chunks))
    cache, logits, frame, first = None, [], 0, True
    with torch.inference_mode():
        while True:
            start = 0 if first else nprocessor.audio_chunk_start(frame)
            size = nprocessor.num_samples_first_audio_chunk if first else nprocessor.num_samples_per_audio_chunk
            last = start + size >= len(audio)
            inputs = nprocessor(audio[start:start + size], sampling_rate=16000, is_streaming=True,
                is_first_audio_chunk=first, is_last_audio_chunk=last).to('cuda', torch.float32)
            output = nmodel(**inputs, speaker_cache=cache)
            logits.append(output.logits.cpu())
            cache = output.speaker_cache
            if last:
                break
            frame += nprocessor.num_mel_frames_per_step
            first = False
        groups = nprocessor.extract_speaker_dict(torch.cat(logits, dim=1))[0]
    turns = [{'start': max(0, float(t['Start'])), 'end': min(duration, float(t['End'])), 'speaker': str(t['Speaker'])}
             for t in groups if float(t['End']) > float(t['Start']) and float(t['Start']) < duration]
    attributed = attribute_speakers(items, turns)
    for row in attributed:
        if row.get('unaligned'):
            row.update(speaker='미확인', speaker_uncertain=True)
    segments = utterances(attributed) if key == 'qwen-batch' else [{**row, 'id': index} for index, row in enumerate(attributed)]
    torch.cuda.synchronize()
    finished = time.perf_counter()
    props = torch.cuda.get_device_properties(0)
    result = {'segments': segments, 'words': attributed if key == 'qwen-batch' else [], 'turns': turns,
        'asr_chunks': text_chunks, 'pipeline': key, 'runtime': RUNTIME_VERSION, 'batch': batch,
        'chunk_count': len(chunks), 'audio_seconds': duration, 'inference_seconds': finished - loaded,
        'load_seconds': loaded - process_started, 'asr_seconds': asr_finished - loaded - align_seconds,
        'alignment_seconds': align_seconds, 'diarization_seconds': finished - asr_finished,
        'precision': 'bf16' if dtype == torch.bfloat16 else 'fp16', 'speaker_count': len({t['speaker'] for t in turns}),
        'uncertain_segments': sum(bool(s.get('speaker_uncertain')) for s in segments),
        'zero_duration_words': sum(w['start'] == w['end'] for w in items) if key == 'qwen-batch' else 0,
        'gpu_name': torch.cuda.get_device_name(0), 'capacity_gib': props.total_memory / 1024**3,
        'peak_reserved_gib': torch.cuda.max_memory_reserved() / 1024**3,
        'peak_allocated_gib': torch.cuda.max_memory_allocated() / 1024**3,
        'memory_phases': memory_phases,
        'software': {'torch': str(torch.__version__), 'transformers': transformers.__version__},
        'model_revisions': {name: MODELS[name][1] for name in model_keys(key)}}
    Path(request['result']).write_text(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main(json.loads(Path(sys.argv[1]).read_text()))
    except Exception as error:
        emit(phase='error', error=str(error)[-1200:])
        raise
