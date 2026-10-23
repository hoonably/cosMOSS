# Length-based time and memory calibration · 2026-10-23

The app uses these measurements to estimate its three Mac MLX variants and three
server presets. This is a latency/memory study on one Korean recording, not a
transcription or diarization accuracy ranking. Raw recordings, transcripts and
logs remain outside the repository; only numerical profiles and public model
identity are exported in `src/duration_calibrations.json`.

## Conditions

- Mac: Apple M4, 16 GiB; MOSS MLX 4/8/16-bit weights, KV8, prefill512,
  max32768 tokens, temperature0, Korean. Main chunks run sequentially with a
  30-second bridge at each boundary. Runtime: MLX0.32.4 / MLX Audio0.5.7 /
  MLX LM0.31.3.
- Server: one H200, 143771 MiB. MOSS uses SGLang Omni with the app's capped KV
  configuration; Qwen uses Transformers with automatic 3-minute batching followed
  by forced alignment. The combined preset runs full-context MOSS and then Qwen
  20-minute auxiliary transcription, unloading between stages. All include
  Nemotron. Runtime profile: `speech-engines-sg019-qwen006-nemo5202-v1`.
- Each condition starts a fresh process without an explicit ASR warmup. Import,
  model loading and internal initialization are included. Download, transfer,
  audio normalization and AI summary are excluded. Filesystem/compile caches may
  be warm; these are not measurements after an OS reboot.
- Server training prefixes: 1, 3, 5, 10, 20, 30 and 45 minutes, plus the full
  65:37.13 input. Mac uses 1/5-minute prefixes and the full 60-minute input with
  15/30/60-minute chunks. A 22-minute prefix is held out of fitting on both.
- Main study: 45 conditions, 40 successful, 5 failed output validation. Two extra
  compatibility/diagnostic runs also failed; 47 records total. Failed outputs
  are retained in the private archive and excluded from speed/memory fitting.

## Full-input results

Mac elapsed time includes the sequential boundary bridges. Peak is Metal
allocation, not total system unified-memory usage; host RSS must not be added.

| Mac M4 16GiB · 60min input | 15min chunks | 30min chunks | 60min chunk |
|---|---:|---:|---:|
| MLX 4-bit | timestamp validation failed | 17:30 / 3.86 GiB | output cap reached |
| MLX 8-bit | 10:36 / 2.66 GiB | 17:18 / 4.12 GiB | 36:37 / 7.06 GiB |
| MLX 16-bit | 12:49 / 3.18 GiB | 20:30 / 4.64 GiB | 39:06 / 7.58 GiB |

| H200 · 65:37 input | Total with setup | Model work only | Owned-process VRAM peak |
|---|---:|---:|---:|
| MOSS · SGLang Omni · full | 3:19 | 1:37 | 24.42 GiB |
| Qwen · Transformers · 3min + alignment | 1:27 | 0:42 | 31.34 GiB |
| MOSS + Qwen · sequential | 6:24 | 4:28 | 35.23 GiB |

The combined preset keeps the MOSS body and stores Qwen as an auxiliary
transcript; it does not automatically choose between words from the two models.

## Held-out 22-minute validation

Signed errors below use unrounded predictions. This is one validation prefix of
the same recording, not validation across independent recordings or speakers.

| Profile | Actual total | Predicted total | Time error | Raw peak actual → predicted GiB | Raw peak error | Budget with headroom GiB |
|---|---:|---:|---:|---:|---:|---:|
| Server MOSS SGLang | 2:05 | 2:04 | −0.09% | 20.06 → 20.04 | −0.08% | 23.45 |
| Server Qwen + alignment | 1:09 | 1:06 | −3.55% | 13.34 → 13.57 | +1.70% | 16.20 |
| Server MOSS + Qwen | 4:21 | 3:51 | −11.54% | 20.06 → 20.04 | −0.08% | 23.45 |
| Mac MLX 16-bit | 5:57 | 6:28 | +8.71% | 3.85 → 3.86 | +0.20% | 4.62 |
| Mac MLX 4-bit | 4:40 | 6:05 | +30.56% | 3.12 → 3.19 | +2.10% | 3.87 |

Mac 8-bit failed held-out timestamp validation, so no successful timing error can
be reported. Its estimates remain provisional. The 4-bit sparse long-context
data also remain provisional; no numerical time is shown above 30min per chunk.
Raw peak errors on successful validation cases are 0.08–2.10%. Displayed budgets
are intentionally 16.9–24.0% above those actual peaks after adding headroom;
budget conservatism is not prediction error.

## Estimator

```text
f(t) = y1 + (y2-y1) * (t-x1) / (x2-x1)
Mac time = median setup + N*f(duration/N) + (N-1)*bridge(min(30s,duration/N))
Server time = median setup + f(duration)
Peak budget = interpolated raw peak * 1.12 + fixed headroom
Fixed headroom = 0.3 GiB Mac / 1 GiB server
```

Knots use monotone piecewise interpolation. Beyond the measured range, the nearest
slope is extrapolated and the UI marks the prediction. Below the shortest point,
time tapers to a quarter of the first work cost and memory retains its resident
model floor. Server median setup is 101.4s for MOSS, 41.7s for Qwen, and 116.5s for
the combined preset; Mac setup is roughly 2–3s. Using median setup means an
estimate at a training duration can differ slightly from that individual run.

Qwen adds processing waves above the automatic 24-chunk limit. Those longer
inputs are structural estimates, not validated measurements. Full-context MOSS
retains its 90-minute input limit. Different GPU/Mac types, capacity classes,
runtimes, model folders or effective manual batches do not inherit these timings.
Compatible server completion records can replace a nearby point; unrelated short
measurements do not rescale the whole curve.

## Observed failure limits

- Mac4-bit/15min: generated timestamps reached964.94s for900s of input.
- Mac4-bit/60min: reached32768 output tokens and stopped at1927.02s.
- Mac8-bit/22min: generated timestamps reached1599.98s for1320s of input.
- Qwen standalone and combined at the3min prefix reached the output cap. An
  additional diagnostic reached4096 tokens with an identical64-token pattern
  repeated up to127 times. These outputs are not valid speed samples.
- An initial16-bit loader compatibility attempt failed before the convolution
  layout fix; the completed16-bit study uses the corrected loader.

No failure above establishes a general model failure rate. Changing speech
density, language, hints, shared GPU load, thermal/power limits or software can
change both performance and validity. Additional independent recordings and
successful held-out Mac8-bit runs are needed before stronger accuracy claims.
