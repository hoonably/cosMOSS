"""CPU-only speaker embeddings inside the optional, isolated voice runtime."""
import array
import json
import math
import sys
import wave

import numpy as np
import sherpa_onnx


def main():
    request = json.load(sys.stdin)
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=request['model'], num_threads=2, debug=False, provider='cpu')
    if not config.validate():
        raise ValueError('목소리 비교 모델 설정을 확인해 주세요.')
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    embeddings = []
    for filename in request['files']:
        with wave.open(filename, 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                raise ValueError('16kHz mono PCM WAV가 필요합니다.')
            samples = array.array('h', source.readframes(source.getnframes()))
        if sys.byteorder != 'little':
            samples.byteswap()
        audio = np.asarray(samples, dtype=np.float32) / 32768.0
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=audio)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError('목소리가 너무 짧습니다. 다시 녹음해 주세요.')
        vector = [float(value) for value in extractor.compute(stream)]
        norm = math.sqrt(sum(value * value for value in vector))
        if len(vector) != 192 or not math.isfinite(norm) or norm < 1e-8:
            raise ValueError('목소리 특징을 추출하지 못했습니다.')
        embeddings.append([value / norm for value in vector])
    print(json.dumps({'embeddings': embeddings}))


if __name__ == '__main__':
    main()
