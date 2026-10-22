"""Normalize MOSS convolution layouts after the pinned mlx-audio loader."""
from __future__ import annotations


def normalize_moss_convolutions(model):
    # mlx-audio 0.5.7 recognizes converted checkpoints by quantization scales.
    # Its sanitizer transposes already-converted, unquantized MLX weights again.
    # Whisper uses a three-sample kernel: MLX expects [out, kernel, channels].
    encoder = getattr(getattr(model, 'model', None), 'whisper_encoder', None)
    if encoder is None:
        return
    for name in ('conv1', 'conv2'):
        convolution = getattr(encoder, name, None)
        weight = getattr(convolution, 'weight', None)
        if weight is not None and len(weight.shape) == 3 and weight.shape[1] != 3 and weight.shape[2] == 3:
            convolution.weight = weight.transpose(0, 2, 1)
