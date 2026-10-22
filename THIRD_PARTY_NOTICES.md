# Third-party notices

cosMOSS is derived from [ginjae/mac-moss](https://github.com/ginjae/mac-moss)
(snapshot `d69b3dbc227e9cee5c9beb8523e5b26b311b2b40`), which is based on
[jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note), a fork of
[JoNebula/moss-note](https://github.com/JoNebula/moss-note).
The application source is distributed under the [MIT License](LICENSE).

## Application provenance

| Project | Attribution | License evidence |
|---|---|---|
| Original MOSS Note | Jo Seongwoon (`JoNebula`), original application and contributors | [Original LICENSE](https://github.com/JoNebula/moss-note/blob/2fbb57e40d80009e6e6a3195085001ce133a1713/LICENSE) |
| Mac MOSS | `ginjae` and contributors, local execution, memory controls, progress and editing UI | [Source LICENSE](https://github.com/ginjae/mac-moss/blob/d69b3dbc227e9cee5c9beb8523e5b26b311b2b40/LICENSE) |
| Local MOSS Note fork | `jiseungjeong` and contributors, Apple Silicon / MLX adaptation used as the basis for Mac MOSS | [Fork LICENSE](https://github.com/jiseungjeong/moss-note/blob/258be7bd48fc2d75579f2d24a9f64d7543635285/LICENSE) |

Both referenced license files contain `Copyright (c) 2026 Jo Seongwoon` and
the same MIT license text. That file is preserved verbatim in this repository's
[LICENSE](LICENSE). The fork's contributions are credited here and in the
[README](README.md). The full application lineage and initial snapshot are recorded in
[Development](docs/DEVELOPMENT.md).

The pinned links identify the license revisions reviewed for this notice; they
do not assert the exact commits from which all local code was derived.

## Models and inference runtime

Model weights and inference packages are downloaded during setup. They are not
vendored in this source repository. The default MLX model is a community
conversion by `vanch007` of the OpenMOSS model.

| Component | License | Upstream evidence |
|---|---|---|
| MOSS-Transcribe-Diarize 0.9B · OpenMOSS / MOSI.AI | Apache-2.0 | [Model card](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize), [source license](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/blob/main/LICENSE) |
| MLX 8-bit MOSS conversion · vanch007 | Apache-2.0 | [Default model card](https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize-8bit) |
| mlx-audio 0.5.7 · Prince Canuma and contributors | MIT | [License at v0.5.7](https://github.com/Blaizzy/mlx-audio/blob/v0.5.7/LICENSE) |
| mlx-lm 0.31.3 · Apple and contributors | MIT | [License at v0.31.3](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/LICENSE) |
| MLX · Apple and contributors | MIT | [Source license](https://github.com/ml-explore/mlx/blob/main/LICENSE) |

The application MIT license does not replace these licenses. If downloaded
models or inference packages are redistributed with an application bundle,
retain their applicable license and attribution files and follow their
redistribution terms, including Apache-2.0 section 4 where applicable. A custom
`MLX_MODEL_ID` may select a model with different terms.

## Remote CUDA runtime

Managed CUDA preparation installs PyTorch/torchaudio 2.8.0 CUDA 12.8 wheels and
OpenMOSS helper code at commit `e3bdd93f8ad58418e04659818efd5fd87a0bc486` on the
selected remote device. These packages and model weights are downloaded, not
vendored in the application repository. Their shipped licenses continue to apply.

- [PyTorch source and license](https://github.com/pytorch/pytorch)
- [torchaudio source and license](https://github.com/pytorch/audio)
- [Pinned OpenMOSS helper source](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/tree/e3bdd93f8ad58418e04659818efd5fd87a0bc486)

## CUDA speech pipelines

The separately installed speech runtime also downloads
[Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) and
[Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B),
both under Apache-2.0, and the Nemotron model listed below. Revisions are pinned
in `src/speech_engines.py` (legacy presets use `src/speech_pipeline.py`). No weights are included in the Mac application.
The quiet-boundary splitting approach follows the official
[Qwen3-ASR implementation](https://github.com/QwenLM/Qwen3-ASR) (Apache-2.0);
new presets call the pinned SDK splitter directly. Legacy cuts remain in `src/speech_worker.py`.
The MOSS server preset separately installs [SGLang Omni](https://github.com/sgl-project/sglang-omni)
and [SGLang](https://github.com/sgl-project/sglang); their upstream licenses and dependencies remain in that server environment.
Korean forced alignment uses the separately installed
[soynlp](https://github.com/lovit/soynlp) package ([LGPL-3.0](https://github.com/lovit/soynlp/blob/master/LICENSE)); its license is retained with the
server installation. Transformers, Accelerate, PyTorch, torchaudio, librosa,
NumPy, SoundFile and their transitive dependencies retain their own notices.

## Web application dependencies

The following direct runtime dependencies are installed from PyPI rather than
included in this source repository:

| Package | License | Source |
|---|---|---|
| FastAPI | MIT | [Package metadata](https://pypi.org/project/fastapi/) |
| HTTPX | BSD-3-Clause | [Package metadata](https://pypi.org/project/httpx/) |
| python-multipart | Apache-2.0 | [Package metadata](https://pypi.org/project/python-multipart/) |
| Uvicorn | BSD-3-Clause | [Package metadata](https://pypi.org/project/uvicorn/) |

Transitive dependencies and additional MLX runtime packages retain their own
licenses in the installed environments. These tables identify the main
components used by this project; they are not a complete inventory of a
redistributed binary bundle.

## Mac release bundle

The Mac app uses [Sparkle 2.9.6](https://github.com/sparkle-project/Sparkle/tree/2.9.6)
for in-app updates under its permissive license. Its complete upstream license
and third-party notices are included as `Contents/Resources/Sparkle-LICENSE`;
the bundled framework retains its own resources and signatures.

The downloadable Mac app includes a standalone CPython 3.12.12 distribution
provided through python-build-standalone, uv 0.9.25 (MIT or Apache-2.0), and the
web dependencies pinned in `uv.lock`. CPython and its bundled libraries retain
their license files under `Contents/Resources/python/`; uv and Python packages
retain their installed distribution metadata and license files. The app's MIT
license and this notice are also included in `Contents/Resources/`.
Model weights, MLX/CUDA runtimes, and FFmpeg are not included in that ZIP.

## FFmpeg

cosMOSS invokes `ffmpeg` and `ffprobe` as separate executables. Working system
installations are reused. Otherwise app setup downloads the Apple Silicon LGPL
variant of [dfloresc/ffmpeg-macos-builds 9.0.1-1](https://github.com/dfloresc/ffmpeg-macos-builds/releases/tag/v9.0.1-1),
verified against a pinned archive SHA-256 in `src/media_tools.py`.
These binaries are not included in this repository or the cosMOSS release ZIP.
The downloaded installation preserves its `LICENSES/`, `BUILDINFO.txt`, binaries
and companion libraries together. Build sources and checksum-pinned upstream
versions are available from the [build project](https://github.com/dfloresc/ffmpeg-macos-builds/tree/v9.0.1-1).
The selected binaries are LGPL-2.1-or-later; individual bundled components retain
their notices in `LICENSES/`. See also [FFmpeg's licensing information](https://ffmpeg.org/legal.html).


## Optional model variants and retired CUDA quantization

Additional Mac weights use vanch007's
[MLX 4-bit](https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize-4bit) and
[original 16-bit](https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize)
conversions of the same Apache-2.0 MOSS-Transcribe-Diarize model.
Earlier NVIDIA quantization support installed [bitsandbytes](https://github.com/bitsandbytes-foundation/bitsandbytes)
(MIT) and [Accelerate](https://github.com/huggingface/accelerate) (Apache-2.0)
into the selected SSH user's runtime. This support and its installer have been removed;
existing installations retain their package licenses. Historical KV experiments used
[HQQ](https://github.com/mobiusml/hqq) (Apache-2.0) in an isolated dependency overlay.
Model weights are not redistributed in this repository.


## Optional voice profiles

Voice comparison explicitly installs [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
(Apache-2.0) and NumPy (BSD-3-Clause) in a separate local runtime. Their package
license files remain with the installation. The CPU speaker embedding model is
[3D-Speaker CAM++](https://github.com/modelscope/3D-Speaker) (Apache-2.0), downloaded
from sherpa-onnx's [speaker recognition model release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models).
The ONNX model is SHA-256 pinned in `src/voice_engine.py`; neither the weights nor
personal voice profiles are bundled in the application or this repository.

## Optional speaker re-diarization

The separately installed SSH runtime uses NVIDIA's
[Nemotron 3 Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization)
under OpenMDW-1.1 and
[pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
under CC-BY-4.0. Community-1 requires each downloading user's Hugging Face
access agreement. User credentials are not bundled or forwarded by cosMOSS.
Model revisions are pinned in `src/diarization_worker.py`; weights are downloaded
on the selected server and are not redistributed with the app.
PyTorch, torchaudio, torchcodec, Transformers, pyannote.audio, librosa, SoundFile
and their dependencies retain the license files in that runtime.
