# Execution devices

## Data and routing

`DeviceStore` persists targets in `data/devices.sqlite3`. On first launch it creates
only `local` (This Mac). The local target uses `MOSS_MLX_PORT` and the local served
model ID. SSH discovery lists aliases but neither registers nor connects them.

Each new note stores `device_id`, `device_name`, and a private JSON configuration
snapshot. Resolution uses that snapshot, not the current upload form or mutable
global URL. An unavailable/removed device produces an error rather than silently
running on another device. A retry retains the original target.

CUDA targets include a GPU UUID, its displayed index, a remote port, model ID, and
a generated private API token. Tokens and snapshots are excluded from API device
lists, public notes, and JSON exports. The data directory remains private and is
excluded from Git. Keep it when backing up the app.

## Discovery and monitoring

`discover_hosts` reads `~/.ssh/config` and recursively expands `Include` patterns.
It enumerates literal Host aliases only; wildcard and negated patterns are not
shown as selectable machines. Listing does not invoke SSH or evaluate Match exec.
Actual connections use OpenSSH, so its identity, proxy, user and port configuration
continue to apply.

Selecting a host starts read-only `nvidia-smi -q -x` probes. The browser polls again
three seconds after each result and cancels its polling on dialog close or host
change. Parse XML rather than the aligned terminal table. Missing utilization,
memory or process data is unknown, not idle. The idle indication is a heuristic:
utilization <= 5%, >= 80% free VRAM, and no known compute processes. Graphics-only
processes may remain. It is not a scheduler reservation or capacity estimate.

GPU UUID is the routing identity; the numeric index is a display aid and default
port offset. A process-local selection survives monitoring refreshes by UUID.
Selecting a host or radio does not install software or run inference.

SSH is noninteractive (`BatchMode=yes`), requires an already trusted host key,
disables forwarding for probes, and does not enable agent forwarding. Unsupported
hosts without nvidia-smi can still be used with a separately configured MLX or
compatible server, but not the managed CUDA mode.

## Managed CUDA workers

The explicit Prepare action sends `remote_bootstrap.py` and `cuda_server.py` to the
selected host. It uses only the remote user's `~/.local/share/cosmoss/` directory.
Linux, bootstrap Python >= 3.10, a working venv module or uv, a compatible NVIDIA
driver and network access are required. There is no sudo or system package setup.

The runtime pins torch/torchaudio 2.8.0 with CUDA 12.8 wheels and the MOSS helper
package at OpenMOSS commit `e3bdd93f8ad58418e04659818efd5fd87a0bc486`.
It reuses the runtime on subsequent preparation. The worker loads the original
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

The remote worker uses the JSON transcription contract. A local note cancellation
closes its request but does not interrupt an already running CUDA generation;
its GPU lock remains held until inference finishes. Stop Worker is explicit and
checks the PID's working directory, environment GPU UUID, device ID and secret
before signalling only that worker. It also works during model loading. A process
that fails to exit after a grace period is killed only after ownership checks.

Remote workers intentionally survive closing the Mac app; use Stop Worker to
release GPU memory. Removing a managed target requires stopping its worker first.
In-progress note jobs block stop/removal. Installed environments/model caches
remain after stopping. The current app transcription queue is serial, even when
multiple devices are registered; automatic scheduling and parallel dispatch are
future work.

## Local API

- `GET /api/devices`: registered targets; no connection or secrets.
- `GET /api/ssh/hosts`: discovered aliases; no connection.
- `POST /api/ssh/probe`: inspect one selected alias.
- `POST /api/devices`: register a host/port/model and optional CUDA UUID/index.
- `POST /api/devices/{id}/status`: open/reuse its tunnel and check the worker.
- `POST /api/devices/{id}/worker/prepare`: prepare/start the selected CUDA worker.
- `POST /api/devices/{id}/worker/stop`: stop only the owned CUDA worker.
- `GET /api/devices/{id}/worker`: local preparation progress.
- `DELETE /api/devices/{id}`: remove a stopped target; local is permanent.

Management requires loopback client/Host and rejects cross-site browser requests.
Application authentication still applies when configured. Unknown host keys and
passphrase-protected keys require an initial terminal connection/SSH agent setup.

## Validation boundaries

Unit/integration tests exercise parsing, registry persistence, credentials, target
snapshots, GPU identity checks, bootstrap process arguments and stop ownership with
mock GPU/SSH responses. `tests/ui_smoke.py` checks host selection, automatic refresh,
GPU selection persistence, default port and polling cleanup in a real browser with
fixture SSH responses. Actual nvidia-smi inventory was also read from a 6x RTX 3090
host. Remote package installation and model inference still require a selected
GPU and an actual hardware run; mock tests do not establish that compatibility.
