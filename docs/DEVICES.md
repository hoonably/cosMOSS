# Execution devices

cosMOSS defaults to processing recordings on This Mac. The remote connection
sections below are optional; no separate machine is needed for local transcription.
Local setup and app usage are described in the [README](../README.md).

## Data and routing

`DeviceStore` persists targets in `data/devices.sqlite3`. On first launch it creates
only `local` (This Mac). The local target uses `MOSS_MLX_PORT` and the local served
model ID. SSH discovery lists aliases but neither registers nor connects them.

Each new note stores `device_id`, `device_name`, and a private JSON configuration
snapshot. Resolution uses that snapshot, not the current upload form or mutable
global URL. An unavailable/removed device produces an error rather than silently
running on another device. A retry retains the original target.

CUDA devices store a host, remote port, model ID, optional model cache override,
and a generated private API token. GPU UUID/index belong to each job snapshot,
not the registered device. Tokens and snapshots are excluded from API device
lists, public notes, and JSON exports. The data directory remains private and is
excluded from Git. Keep it when backing up the app.

## Discovery and monitoring

The first stage lists registered devices and per-device readiness.
This Mac is marked as the default and shown first.
The bottom connection button opens a dialog; SSH aliases are read only after
opening that flow. The panel header stays in place while the list scrolls
independently. A device's Use action opens a new note with that target selected.
The connection modal contains only SSH registration; registration progress
appears in the panel. Candidate aliases are discovered locally, without probing
or installing on those hosts. There is no global transcription-server error badge.
The panel refreshes approximately every ten seconds while visible. Local/manual
server connection failures are summarized as a neutral preparation state; managed
installation errors remain available under the affected device's details.

`discover_hosts` reads `~/.ssh/config` and recursively expands `Include` patterns.
It enumerates literal Host aliases only; wildcard and negated patterns are not
shown as selectable machines. Listing does not invoke SSH or evaluate Match exec.
Actual connections use OpenSSH, so its identity, proxy, user and port configuration
continue to apply.

Selecting a managed device in the new-note dialog starts read-only `nvidia-smi -q -x`
probes. Registration does not probe or select GPUs. The browser polls again
three seconds after each result and cancels its polling on dialog close or host
change. Parse XML rather than the aligned terminal table. Missing utilization,
memory or process data is unknown, not idle. The idle indication is a heuristic:
utilization <= 5%, >= 80% free VRAM, and no known compute processes. Graphics-only
processes may remain. It is not a scheduler reservation or capacity estimate.

GPU UUID is the routing identity; the numeric index is a display aid. Ports are
independent of GPU indexes. A per-job selection survives refreshes by UUID, but is
cleared if that GPU disappears. Busy GPUs remain selectable, with an app-styled
warning listing process names, PIDs, VRAM usage and the risk of memory exhaustion
or slower processing. Cancel is focused by default. Confirming permits this job
to share the GPU; it never kills existing processes. Local/manual servers show no GPU
picker. Note creation probes again before consuming the upload and pins the UUID
and observed index in the private job snapshot. Busy/unknown states require a
confirmation token bound to the GPU UUID and observed process list. Changed
processes produce another warning before the upload is consumed. The override
is stored only in that job's snapshot, never in device settings. Bootstrap checks
once more at launch and permits occupied GPUs only for a confirmed job.

SSH is noninteractive (`BatchMode=yes`), preserves each alias's host-key and
known-hosts policy from SSH config, disables forwarding for probes, and does not
enable agent forwarding. This includes local ports forwarded to Kubernetes pods.
With OpenSSH's default host-key policy, trust a new host in Terminal first. Unsupported
hosts without nvidia-smi can still be used with a separately configured MLX or
compatible server, but not the managed CUDA mode.

## Managed CUDA workers

The execution-environment control on each managed card (including This Mac)
opens a component list for uv, Python venv and required packages. The read-only
`GET /api/devices/{id}/environment` executes the venv's Python with `-I -B` and
checks isolation, module presence, distribution versions and the runtime stamp.
It does not import models, download packages or initialize CUDA. The matching
POST endpoint repairs only the environment, preserving model-location settings
and avoiding model downloads. Both automatic setup and manual repair detect a
deleted venv or missing packages even when an old version stamp remains.
Active transcription/setup blocks concurrent environment installation.
New remote tools and managed interpreters remain alongside the retained venv;
temporary installer files and job caches are cleaned normally.

Registering a managed SSH device immediately prepares its execution environment
and model without selecting, loading or probing a GPU. Existing installations
are reused; the default model location is the SSH user's Hugging Face cache.
Model settings can later select a different cache or an existing model folder
using an absolute or `~/...` path on that host.
Failed initial setup can be removed from the local device list even while the
host is offline. Once a transcription launch is attempted, a durable cleanup
flag preserves the requirement to stop that device's owned worker before removal.
Only a running/loading GPU or a pending transcription cleanup shows a cleanup
button; setup and connection errors alone never show “GPU 해제”.
Opening the dialog reads paths without installing packages or loading a GPU.
The app sends `runtime_environment.py`, `remote_bootstrap.py`, `model_cache.py`,
and `model_download.py` to the selected host. Python **venvs**
are shared by the SSH account and runtime version, independent of the Mac's device
ID, host alias, model choice or application data. New installations live under
`~/.local/share/cosmoss/runtimes/<runtime-version>/installs/<installation-id>/.venv`.
They remain separate from Conda and do not include system site-packages.
Removing/re-registering a device or reinstalling the Mac app reuses the same
compatible server environment. Removing a server environment causes the next setup
to rebuild it; version stamps alone never count as a working installation.

Valid older installations in `workers/<device-id>/.venv` are discovered and reused
in place without moving their interpreter, changing their ownership/token records,
or touching another Mac's worker files. Missing/broken packages or incompatible
versions are rejected. Repair builds a new installation and publishes its pointer
only after validation, leaving previously usable environments intact for workers
that may still be running. Interrupted, unpublished installations can be resumed.
A per-version OS file lock serializes setup from different Macs; waiting clients
recheck and reuse the completed environment. Installer children retain the lock
if their SSH/bootstrap parent exits, so a retry cannot modify a still-running
installation. Quantized variants share a separate
dependency slot, so adding quantization cannot change a base runtime in use.
Inspection remains read-only and reports the actual selected venv path.

The environment, any managed base Python, and small version/coordination records
are retained for reuse. Worker source, recordings, caches, credentials and process
ownership still belong to the separate `workers/<device-id>` job directories.
An existing uv executable is reused; otherwise uv 0.9.25 is installed from its
official installer into the runtime's `tools/` directory using
`UV_UNMANAGED_INSTALL`, without modifying shell profiles or global tools.
Linux, bootstrap Python >= 3.10, a compatible NVIDIA
driver and network access are required. There is no sudo or system package setup.

The runtime pins torch/torchaudio 2.8.0 with CUDA 12.8 wheels and the MOSS helper
package at OpenMOSS commit `e3bdd93f8ad58418e04659818efd5fd87a0bc486`.
It reuses the runtime on subsequent preparation. Models use the SSH account's
standard Hugging Face cache: `HF_HUB_CACHE`, then `HF_HOME/hub`, then
`XDG_CACHE_HOME/huggingface/hub` or `~/.cache/huggingface/hub`. An optional device
cache override accepts an absolute path or `~/...`. No sudo or mandatory system
directory is involved. A complete cached snapshot is reused offline, including
read-only caches. Missing models are downloaded with public Hugging Face file APIs under a
per-model download lock, preserving the standard snapshot/blob cache layout.
Both Mac and remote preparation emit byte progress once per second, including
cache hits and resume offsets. The UI polls every second and displays percent
and received/total bytes in the dialog and device card. Installation and model
loading remain separate phases; only model download has a byte percentage. Incomplete weights/configs do not count as ready.
The cache layout follows the [Hugging Face cache documentation](https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache).

On execution the worker loads that exact local snapshot with `local_files_only`
and offline Hub access, while serving the original model ID. The worker loads the original
MOSS model using SDPA and BF16 when supported, otherwise FP16. This is distinct
from the Mac MLX weight/KV quantization settings. There is no CPU fallback.

Preparation rechecks the selected UUID and rejects an existing compute process.
The spawned worker receives `CUDA_VISIBLE_DEVICES=<GPU UUID>`; its only visible
CUDA device is `cuda:0`. A per-user GPU file lock prevents another cosMOSS worker
under that same account from loading on the same GPU. This does not lock out other
users/applications. No existing GPU process is killed to make room.

Workers bind only to remote `127.0.0.1`. FastAPI owns independent local SSH tunnels
and verifies the returned device ID, GPU UUID, served model and Bearer token.
Each transcription also carries the expected device ID and UUID; a different
cosMOSS GPU worker rejects it. The app never adopts an unrelated service occupying
the requested port.

The remote worker uses the JSON transcription contract. The queue prepares the
selected GPU automatically for each note, then stops that worker on completion,
failure or cancellation, including cancellation during model loading. Stopping
checks the PID's working directory, environment GPU UUID, device ID and secret
before signalling only that worker. It also works during model loading. A process
that fails to exit after a grace period is killed only after ownership checks.

After the result is stored on the Mac, stopping also removes the worker source,
log, PID/GPU job record, temporary uploads and per-job caches. The retained venv
and model snapshot are reused on the next run. Setup failures clean their temporary
files too; failed model launches terminate the child created for that launch before
cleaning. Upload spooling, Hugging Face dynamic modules, CUDA/Triton/Torch compile
caches and temporary files are directed inside the worker directory. Runtime
installation uses its own temporary/cache paths so job cleanup on another Mac
cannot interrupt setup. UV installs use copy mode so deleting the download cache cannot break
the retained environment. Existing shared package caches elsewhere are not deleted.
The cleanup checks directory ownership, preserves models and refuses overlapping
model/job paths. Cleanup failure is shown on the device and can be retried with
**GPU 해제**. These guarantees apply to the CUDA workers managed by cosMOSS;
user-managed MLX/compatible services retain their own lifecycle and storage rules.

Closing the app attempts to stop the active job's GPU worker before closing SSH
tunnels. A force quit or failed SSH connection can leave a remote process running;
the owned-worker cleanup action is available for recovery. In-progress jobs block
manual setup/stop/removal. Installed environments/model caches remain after
stopping or removing a device. The current app transcription queue is serial, even when
multiple devices are registered; automatic scheduling and parallel dispatch are
future work.

## Local API

- `GET /api/devices`: registered targets; no connection or secrets.
- `GET /api/ssh/hosts`: discovered aliases; no connection.
- `POST /api/ssh/probe`: inspect one selected alias.
- `POST /api/devices`: register a host/port/model/cache and prefetch without GPU.
- `POST /api/devices/{id}/gpus`: inspect only this registered host for a new job.
- `POST /api/devices/{id}/status`: inspect model readiness (CUDA) or a manually served endpoint.
- `POST /api/devices/{id}/worker/install`: prepare runtime/model without a GPU.
- `POST /api/devices/{id}/worker/prepare`: compatibility alias for model installation.
- `POST /api/devices/{id}/worker/stop`: stop only the owned CUDA worker.
- `GET /api/devices/{id}/worker`: local preparation progress.
- `DELETE /api/devices/{id}`: remove an inactive target; local is permanent.
- `POST /api/notes`: managed CUDA requires a currently available `gpu_uuid` in addition to `device_id`.

Management requires loopback client/Host and rejects cross-site browser requests.
Application authentication still applies when configured. Unknown host keys and
passphrase-protected keys require an initial terminal connection/SSH agent setup.

## Validation boundaries

Unit/integration tests exercise parsing, registry persistence, credentials, target
snapshots, GPU identity checks, bootstrap process arguments and stop ownership with
mock GPU/SSH responses. Cache tests cover environment precedence, offline reuse,
prefetch-on-miss and partial snapshots. `tests/ui_smoke.py` checks host-only
registration, model readiness, per-job GPU selection, explicit busy-GPU confirmation,
submitted UUID and polling cleanup in real Chromium/WebKit browsers with
fixture SSH responses. Actual nvidia-smi inventory was also read from a 6x RTX 3090
host. Live package installation, model downloads and GPU inference have not been
validated by these fixtures; those still need a run on the target hardware.


## Model variants and device tuning

The device list occupies the left stage of the three-column workspace. The
middle stage holds note selection, transcript review, speaker naming and TXT
actions; the right stage runs optional Codex or Claude summaries. Each stage
scrolls independently, so devices remain accessible while reviewing a note.

`model_catalog.py` lists compatible MLX 4/8/16-bit snapshots and the original
CUDA model with optional bitsandbytes 8-bit/NF4 decoder quantization. The audio
encoder, adaptor and output head remain unquantized. Quantized CUDA loading uses
a pinned device map and avoids casting quantized weights with `.to(dtype)`.
These CUDA quantization paths are experimental and have not been validated for
transcription accuracy on hardware. The optional runtime dependency is installed
only when that variant is prepared. Other MOSS formats requiring different
engines are listed as unavailable with explanations.

Mac hardware metadata comes from sysctl. Remote setup reads GPU memory with
nvidia-smi without allocating a GPU. Capacity-based profiles are starting values,
not measured peak-memory guarantees. Per-GPU profile buttons fill the form;
GPU selection for execution still happens in the new-note dialog. Existing
choices are never silently replaced by recommendations.

Device options persist in devices.sqlite3 and local-model.json. The per-note
snapshot pins the model, precision, attention, cache setting, bridge length and
output cap; per-note chunk length may override the device default. Changing
settings while notes are queued or processing is rejected. Changes to local KV
or prefill settings restart the owned MLX process; next app launch restores the
chosen model and options without downloading weights again.
