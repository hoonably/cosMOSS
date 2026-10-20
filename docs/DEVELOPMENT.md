# Development

[← cosMOSS](../README.md)

## Run from source

Apple Silicon, macOS 13 or later, Xcode Command Line Tools, and uv are
required to build and run the development app.

```bash
brew install uv
git clone https://github.com/hoonably/cosMOSS.git
cd cosMOSS
cp .env.example .env
./scripts/setup-app.sh
./scripts/build-mac-app.sh
open dist/cosMOSS.app
```

Prepare common file tools with **설정 → 준비하기** after launching. This reuses
working `ffmpeg`/`ffprobe` executables or downloads a pinned, SHA-256-verified
macOS arm64 LGPL build into the writable runtime's `.cache/media-tools/`.
The archive retains `bin/`, `lib/`, `LICENSES/` and build metadata together. Setup
is serialized, validates both executables before publishing an atomic pointer,
and preserves the previous installation on failure. API reads do not download.
`GET/POST /api/setup` is loopback/same-site protected and prepares no MLX packages
or model weights. Upload preprocessing waits for any explicit setup and verifies
readiness before using absolute tool paths; upload/transcription routes cannot
start a software download around the protected setup endpoint. Source and release
apps use the same resolver.
`tests/test_media_tools.py` covers setup, offline reuse, recovery and isolation.
`tests/setup_ui_smoke.py` checks common setup progress, retry, background polling,
remote-only model preparation and the settings dialog at desktop/mobile widths.

Prepare a model only on the device used for transcription. For browser development,
run `./scripts/run-app.sh` instead of opening the Mac bundle.

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node --check static/selects.js
node --check static/panes.js
```

Build the downloadable app and ZIP with `./scripts/build-mac-release.sh`.
The output is `dist/cosMOSS-v<version>-macos-arm64.zip`.

## Publishing updates

The app uses Sparkle 2.9.6 with signed appcasts and Ed25519 archive validation.
Both the app version and monotonic build number come from `pyproject.toml`
(build = major × 1,000,000 + minor × 1,000 + patch). Update the matching project
version in `uv.lock` when bumping the version; keep dependency pins unchanged.

Run `UV_BIN=/path/to/uv ./scripts/prepare-release.sh` to test, build the ZIP, and
generate `appcast.xml`. This feed is served from the repository's `main` branch.
Publish the ZIP on the matching GitHub release before pushing
the new feed, so installed apps only see downloadable updates. Publish a feed
change as a separate follow-up commit if necessary. Release notes stay short.

The cosMOSS signing key is kept outside the checkout at
`~/Library/Application Support/cosMOSS Development/sparkle-private-key` (0600).
Back up and transfer this key securely for releases from another Mac. The
repository contains only `macos/UpdatePublicKey.txt`. `SPARKLE_PRIVATE_KEY_FILE`
can select an existing restored key. Never regenerate a key for an existing
installation; `scripts/setup-update-key.swift` checks that both keys match.

`scripts/verify-update.swift` verifies both feed and archive using only the
embedded public key. An install/relaunch waits for transcription, uploads,
model preparation and summaries to finish, and for pending editor saves.
The updater is disabled in checkout-based development launchers.
The original `0.1.0` build had no updater. Its replacement pre-release uses build
1000 and must be installed manually once; subsequent versions update in-app.

## Local configuration and data

The development launcher reads the checkout's `.env`; available settings are
listed in [`.env.example`](../.env.example). Source runs keep notes, recordings,
and device settings in `data/`, logs in `logs/`, and the MLX environment in
`.venv-mlx/`. Quit the app before backing up its data. Release-app storage is
described below.

Hugging Face uses `~/.cache/huggingface/hub` by default and honors `HF_HOME` and
`HF_HUB_CACHE`. Device-specific model options are covered in the
[user guide](USAGE.md); SSH workers are documented in [DEVICES.md](DEVICES.md).

## Boundaries

cosMOSS is a local-first Mac application. Its default device is This Mac; connecting
another device is optional. Keep infrastructure terms out of the default user
interface and remote-only controls behind the other-device connection flow.

The SwiftUI app owns its local FastAPI process. Its local-model manager prepares the MLX environment and starts the selected
model from the standard Hugging Face cache or an existing folder. It stores the
selection in `data/local-model.json` and restores it without downloads on startup.
It stops only the inference process it started when the app exits. The device manager can explicitly prepare and stop remote CUDA workers;
see [device architecture](DEVICES.md). The browser editor talks to FastAPI; FastAPI talks to the
transcription endpoint. Keep device-specific inference code outside the editor
and note store.

Managed CUDA devices reuse an account-scoped, versioned venv, private uv when needed,
managed Python when needed, and model cache. Older per-device environments are
discovered in place. `runtime_environment.SharedRuntime` handles shared setup locks and validated
installation generations; deleting Mac app data or re-registering a device does
not trigger another compatible installation. `remote_bootstrap.cleanup_job_files`
removes only the known transient worker paths after the owned process exits. It
never follows transient symlinks, rejects overlapping model paths, and preserves
unrelated devices and files. Setup/build caches use per-installation paths and UV copy
installs so deleting cache entries does not remove runtime dependencies.
`tests/test_remote_cleanup.py` covers retention, cache isolation, ownership,
symlinks, process termination, startup/setup failures and cleanup failure states.

New managed SSH devices prepare their runtime and model immediately; discovery
alone remains read-only. `runtime_environment.py` is shared by Mac and SSH setup.
Environment inspection and installation are separate from model preparation:
`tests/test_runtime_environment.py` covers deleted-venv repair, package validation,
private uv installation, and GPU/download-free checks. `tests/runtime_ui_smoke.py`
exercises manual installation and recovery on both Mac and SSH cards.
`tests/test_remote_runtime.py` covers old-environment discovery, app/device
reinstallation, deleted/damaged server environments, failed-setup recovery,
quantization isolation, ownership/symlinks and two independent setup processes.

`TextLibrary` indexes selected-folder TXT files in `data/text-library.sqlite3`
without importing them into the audio note store or copying their contents.
The frontend merges that list with app notes and refreshes it every five seconds.
Only registered, regular, non-symlink files are opened, and managed TXT paths are
deduplicated. Writes require the viewed file hash, retain encoding/newlines/mode,
and use atomic replacement. File-backed summaries read the actual source bytes
without invoking the audio-note TXT serializer. `test_text_library.py` and
`library_ui_smoke.py` cover indexing, edits, conflict protection and summary flow.

`scripts/build-mac-app.sh` builds a development launcher referencing its checkout
via `CosMOSSProjectRoot` in Info.plist. `scripts/build-mac-release.sh` builds a
relocatable Apple Silicon ZIP with the app sources, a standalone CPython 3.12.12,
locked web dependencies, and uv 0.9.25. Set `UV_BIN` to the build machine's uv
executable when it is not on PATH. Dependency and interpreter license files remain
inside the bundle. Models, MLX packages, FFmpeg, personal data, and credentials
are excluded. Common file tools are prepared separately through app settings.

The release launcher uses `~/Library/Application Support/cosMOSS/` for writable
data, the MLX venv, caches and logs. `MOSS_RUNTIME_DIR` separates these writable
runtime paths from the read-only bundled source. Finder launches add Homebrew's
executable directories to PATH so FFmpeg and optional AI CLIs can be found.
Release bundles use ad-hoc signatures, not Developer ID signing or notarization.
The build retains the matching python-build-standalone license inventory and
verifies its source archive's SHA-256 before copying the notices.

After extracting the release ZIP into a different directory, validate it with
`python tests/release_smoke.py /path/to/cosMOSS.app`. This checks bundle signatures,
embedded Python/uv, writable runtime separation, and actual VTT reading/editing
without invoking models or AI services.

The app binds its backend to loopback, uses port 18760 by default, verifies the
`/healthz` application marker, and terminates only a process it started. The review-stage footer's save-folder setting is
persisted in macOS UserDefaults and initializes both saved note TXT files and the native TXT export save panel;
it defaults to the user's Downloads folder. Model caches and internal note data
remain separate. Use the
`COSMOSS_DESKTOP_PORT` launch environment variable to override the port. `.env`
continues to configure the Python application and transcription endpoint.

## Transcription server contract

Both modes call the same endpoints:

- `GET /v1/models`: JSON with a `data` array containing model `id` values.
- `POST /v1/audio/transcriptions`: multipart audio upload with `model`, `prompt`,
  `response_format=json`, `temperature=0`, `max_completion_tokens`, and optional
  `language` and `hotwords`.

Return a JSON object with `text`, using `[start][Sxx]text[end]` or
`[start-end][Sxx]text`, or provide a `segments` array. Each segment contains
`start`, `end`, `speaker`, and `text`. Preserve speaker labels within each input.
The application handles chunking and boundary-based speaker reconciliation.

MLX mode defaults to the included custom NDJSON streaming contract. Requests
add `stream=true` and `request_id`. Events have `type` equal to `progress`,
`heartbeat`, `result`, or `error`. The cancellation endpoint is
`POST /v1/audio/transcriptions/{request_id}/cancel`.

Remote mode defaults to non-streaming JSON. It does not send streaming or
cancellation-specific fields. This prevents assuming that external endpoints
implement the included MLX server's custom protocol. Remote inference may
continue on the remote device after a local cancellation. SSE streams are not
supported. An adapter may be needed for an otherwise OpenAI-compatible server;
the included managed CUDA worker implements this JSON contract. Actual CUDA
installation and inference need hardware validation.

`MOSS_TRANSCRIPTION_API_KEY`, when set, becomes a Bearer header on health,
transcription, and cancellation requests. It is never included in UI responses.
The application-level `MOSS_AUTH_*` credentials remain separate.

## Validation

The three-column UI keeps device selection, note review, and summaries visible.
`static/panes.js` gives the stages equal initial widths and two draggable
separators. It saves proportions in localStorage, constrains minimum usable
widths on window resizing, and refits transcript textareas after a split changes.
Double-click or Enter restores equal columns. Arrow keys (Shift for larger
steps), Home/End, pointer capture and Escape cancellation are supported. Narrow
browser layouts hide the splitters. `tests/panes_ui_smoke.py` exercises dragging,
keyboard controls, reflow, persistence, limits and responsive transitions.
`static/selects.js` renders every dropdown as an app-styled combobox and listbox.
Hidden native selects retain form values and existing change handlers. The
component synchronizes dynamic options, direct value assignments and disabled
states; it supports arrows, Home/End, typeahead, Enter, Escape and Tab. Menus are
placed inside their owning dialog's top layer, sized to its bounds and repositioned
on scrolling. `tests/select_ui_smoke.py` checks these behaviors, form reset and
validation, dynamic device cards and control heights in Chromium and WebKit.
`src/text_files.py` tracks each exported TXT's path and SHA-256 digest. Explicit
speaker saves write the TXT before committing names; later note edits update the
same file. External edits cause a conflict rather than an overwrite. Deleting a
note removes its managed audio and summary, but preserves its exported TXT.

`src/summaries.py` calls authenticated local Codex or Claude CLIs using argv and
stdin, without a shell. The source is a fixed snapshot of the edited TXT. Codex
runs read-only with user config, hooks, and shell tools disabled. Claude runs in
print mode with tools and MCP disabled and configuration sources empty. Both use
temporary working directories and disable session persistence. The final result,
source digest, prompt, and status are saved in `data/summaries/<note-id>.json`.
The API protects file-writing and CLI routes with the loopback and same-site
checks used for device management. A run has a 15-minute timeout; cancellation
and shutdown terminate only the process group started for that run. Notes cannot
be deleted during an active summary. Source edits remain allowed and mark the
result stale. Startup reports interrupted runs and permits retrying.

Run `python -m unittest discover -s tests -v` inside the project's environment.
The tests use HTTPX's mock transport and temporary audio/database paths. They
require neither model weights nor GPU access and do not establish transcription
accuracy or RTX 3090 compatibility.

`tests/test_workflow.py` covers file conflicts, write failures, speaker saves,
stdin snapshots, both CLI adapters, cancellation, persistence, and route origin
checks using temporary files and fake CLI executables. The browser smoke tests
use synthetic notes and mock AI responses; they never send real recordings or
transcripts to a service:

```bash
uv run --no-project --with playwright==1.57.0 python tests/ui_smoke.py http://127.0.0.1:8000 /tmp/devices.png webkit
uv run --no-project --with playwright==1.57.0 python tests/workflow_ui_smoke.py http://127.0.0.1:8000 /tmp/workflow.png webkit
uv run --no-project --with playwright==1.57.0 python tests/select_ui_smoke.py http://127.0.0.1:8000 /tmp/selects.png webkit
uv run --no-project --with playwright==1.57.0 python tests/panes_ui_smoke.py http://127.0.0.1:8000 /tmp/panes.png webkit
```

Build the Swift application with `./scripts/build-mac-app.sh`. For manual QA:

1. Open `dist/cosMOSS.app` without the local model running: the app should open,
   show This Mac as default, and keep other-device connections collapsed.
2. Choose This Mac → Model setup, select the default cache or an existing MLX
   model folder, and check that preparation finishes and This Mac becomes ready.
   Reopen the app to verify the saved model starts automatically.
3. Upload a short recording, edit text and speaker names, then export each format.
4. Close the final app window; the owned FastAPI process should exit.
5. Repeat with a separately started FastAPI server on the desktop port; quitting
   the app must leave that pre-existing server running.

## Initial snapshot

The application lineage is JoNebula/moss-note → jiseungjeong/moss-note →
ginjae/mac-moss → hoonably/cosMOSS. Original copyright and MIT license text are
preserved in LICENSE; model/runtime attribution is in THIRD_PARTY_NOTICES.md.

Application source was imported from ginjae/mac-moss commit
`d69b3dbc227e9cee5c9beb8523e5b26b311b2b40`. Its Git metadata, private files,
recordings, model weights, environments, and caches were not copied.

Device/GPU tests and a browser smoke test are documented in [DEVICES.md](DEVICES.md).
