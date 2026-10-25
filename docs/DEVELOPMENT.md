# Development

[← cosMOSS](../README.md)

## Website deployment

Cloudflare Pages publishes `https://cosmoss-app.pages.dev/` from this repository.
The `cosmoss-app` project uses `main`, root directory `docs`, build command
`exit 0`, and output directory `.`. Build watch paths include only `docs/*`;
automatic preview branch deployments are disabled. The site uses static files
without Pages Functions, Workers, or database bindings. App downloads remain on
GitHub Releases.

## CUDA speech presets

`speech_pipeline.py` defines the ordered presets and conservative memory plans;
`speech_jobs.py` owns a finite SSH request and numeric-only performance history.
`speech_engine_bootstrap.py` installs separate pinned SGLang, Qwen SDK and
Nemotron environments, acquires one GPU UUID lock and runs stages sequentially.
`speech_engine_worker.py` exits after every model; the bootstrap kills and verifies
the entire process group before acquiring CUDA for the next stage. The stage's
Linux parent-death handler also kills its SGLang descendants on supervisor loss.
The lease remains active through upload, loading, inference and collection.
`speech_engines.py` pins checkpoint revisions and package versions. Its isolated
CUDA13 compiler overlay leaves system CUDA and existing runtimes untouched.

The server menu exposes MOSS SGLang full context (default), Qwen Transformers SDK
3m + forced alignment, and MOSS full → unload → Qwen SDK20m auxiliary → unload →
Nemotron. Alignment runs after Qwen exit, with a PyTorch allocator budget below
its observed process peak and adaptive batches. Raw Qwen token IDs and natural
EOS are checked before accepting outputs. Auxiliary text is private in
`speech-result.json` and fetched only through the per-note auxiliary endpoint;
it never overwrites the MOSS body. Mac MLX uses its native ASR runtime, followed
by the same whole-recording Nemotron attribution policy. The old
`speech_bootstrap.py` / `speech_worker.py` are retained for saved legacy jobs.

## Mac transcription bundle

New local jobs snapshot `options.local_diarization="nemotron-3"`. They transcribe
all MLX chunks, unload the ASR model, run Nemotron once on the full recording,
then associate the original ASR text/timestamps with the speaker timeline. The
server attribution rules are shared: ambiguous/overlapping turns remain
unidentified instead of inheriting the nearest speaker. Nemotron does not repair
ASR text or timing errors. Historical jobs without the marker retain their
MOSS-only behavior and estimates.

The explicit model-download action prepares both the selected MLX checkpoint
and the isolated Nemotron runtime. Selection alone downloads nothing. Readiness
requires both stages, caches are reused, and preparation does not keep a model
resident. The same note dialog supports per-note model choice and estimates on
Mac and server; only GPU and chunk controls differ. Local background jobs hold
one Mac resource through ASR, unload, diarization and cleanup, so multiple
recordings run sequentially and cancellation cannot release it while a child
still owns the device.

The app takes a per-note snapshot of the preset and requested batch. Automatic
batch plans budget for quiet-boundary cuts up to five seconds early, avoiding an
unexpected final GPU wave. H200 reference latency is not used for other GPUs;
successful jobs calibrate the same GPU name, VRAM class, runtime, preset and
effective batch. The history contains no audio, transcript, filename or host.
Per-note `speech-result.json` is private and preserves original ASR chunks,
aligned words, speaker turns and metrics. The normal API exposes concise metrics.

Zero-duration Qwen words are assigned only to a unique active speaker at that
point. Overlapping or missing evidence stays `미확인`; this label is neither a
speaker embedding candidate nor a known speaker identity.
Native ASR text and timing are never rewritten to fabricate a confident match.

Validation: `tests/test_speech_pipeline.py`, `tests/speech_pipeline_ui_smoke.py`,
the existing transcription/progress checks, and the
[actual H200 integration run](performance/2026-10-21-app-pipelines.md).

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
`src/voice_memos.py` reads the Mac Voice Memos SQLite metadata in read-only mode
only when its picker is opened. It falls back to local filenames for unfamiliar
schemas and marks cloud-only recordings unavailable. Recognized deletion dates
(`ZEVICTIONDATE`, the backing field for CloudRecording.deletionDate, or
`ZDELETIONDATE`) retain hidden metadata tombstones so surviving Recently Deleted
files cannot reappear through filename fallback. Import rescans that same state
and rejects a selection deleted since the picker opened; restoring clears the
exclusion. The current Mac's model accessor was inspected read-only to verify
the eviction/deletion alias; no private API is linked or called by cosMOSS. Explicit imports use opaque
IDs, file-version checks and the existing bounded upload/preparation pipeline;
recordings and Apple's database are never edited. Opening the picker first
probes the known Voice Memos directories from the native foreground process.
The settings button opens Privacy_AllFiles alongside `FullDiskAccessAssistant`,
a small floating native panel. Its `NSDraggingItem` writes the actual running
bundle's file URL to the drag pasteboard; the user drops it into Settings and
turns on the switch. It works with bundles outside Applications too. A completed
file drag is never treated as permission approval. Done closes the panel and
returns to the existing picker, which rescans access. No settings UI automation,
TCC writes, resets or automatic grants occur. `tests/FullDiskAccessGuideChecks.swift`
renders the real panel offscreen and checks the real app URL on a private
pasteboard without opening Settings. The browser smoke checks the return event. `tests/test_voice_memos.py`
covers metadata, isolation, stale files, limits and local-only access.
`tests/setup_ui_smoke.py` checks common setup progress, retry, background polling,
remote-only model preparation and the settings dialog at desktop/mobile widths.

Prepare a model only on the device used for transcription. For browser development,
run `./scripts/run-app.sh` instead of opening the Mac bundle.

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node --check static/selects.js
```

Build the downloadable app, DMG installer and updater ZIP with
`./scripts/build-mac-release.sh`. The outputs are
`dist/cosMOSS-v<version>-macos-arm64.dmg` and `.zip`.
Normal development builds (`build-mac-app.sh`) do not generate either archive.
Local signing uses `scripts/sign-mac-app.py`: it automatically uses the one
available Apple Development certificate (or Developer ID Application certificate).
Set `COSMOSS_CODESIGN_IDENTITY` to a certificate name or SHA-1 if several exist.
An explicitly selected certificate that cannot sign fails the build; it never
silently falls back to another identity. Without a certificate, the script warns
and uses ad-hoc signing. Keep the same certificate and bundle ID across rebuilds
so macOS can recognize the app's designated requirement and retain permissions.
After switching from an old ad-hoc build, quit the app, remove its old Full Disk
Access entry and add the current app once, then relaunch. Signing does not grant
or reset permissions. See [Apple TN3127](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements).

Release builds deliberately default to ad-hoc signing rather than distributing
an Apple Development build. `COSMOSS_RELEASE_CODESIGN_IDENTITY` explicitly selects
a distribution identity; Developer ID signing and notarization require separate
release setup. Sparkle's update signature is independent of Apple code signing.
`tests/signing_smoke.py` changes a temporary bundle and verifies that both signed
versions satisfy each other's designated requirements without touching TCC.
To package an existing standalone app without rebuilding it, run
`./scripts/build-mac-dmg.sh /path/to/cosMOSS.app`. Packaging uses pinned dmgbuild
tools in uv's cache, creates the branded Finder layout without UI automation,
and verifies the compressed image before replacing the previous DMG.
The app icon uses the existing green waveform. Run `swift scripts/build-app-icon.swift`
to regenerate `macos/Assets/AppIcon.icns` and its PNG/SVG previews after changing the artwork.
Both development and release bundles include the same icon.

## Publishing updates

The app uses Sparkle 2.9.6 with signed appcasts and Ed25519 archive validation.
Both the app version and monotonic build number come from `pyproject.toml`
(build = major × 1,000,000 + minor × 1,000 + patch). Update the matching project
version in `uv.lock` when bumping the version; keep dependency pins unchanged.

Run `UV_BIN=/path/to/uv ./scripts/prepare-release.sh` to test, build the DMG/ZIP, and
generate `appcast.xml`. This feed is served from the repository's `main` branch.
Publish the DMG and ZIP on the matching GitHub release before pushing
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
The native update-check window exposes Sparkle's persisted automatic-check setting.
`SUEnableAutomaticChecks` enables it on first launch; user choices are never reset.
Manual checks remain available when automatic notifications are off. Run
`python tests/update_preferences_smoke.py` to verify the default, persistence,
manual checks, update availability and signed-feed error handling in isolated apps.
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

Step 3 model discovery lives in `src/summary_models.py`. Codex uses the installed
CLI's app-server `initialize` + `model/list` protocol with pagination. Claude uses
the SDK streaming `initialize` control response's `models` capability list. Neither
probe sends a user message or starts a generation. Probes run in a temporary working
directory, have a timeout, and reap their own process group on cancellation.

Model IDs, display names and supported effort strings come from the CLI rather than
a versioned app list. User selections are stored per provider in
`summary-model-preferences.json`; `summary-model-catalog.json` is a one-hour metadata
cache invalidated by a CLI binary change or explicit refresh. The initial selection
is the CLI default model with Medium when available. A successful refresh can reset
a removed model or unsupported effort; a failed refresh retains the previous choice.
The selected model and effort are validated and copied into each summary job/result.
They are passed as process arguments, without rewriting the user's CLI configuration.

`tests/test_summary_models.py` covers protocol framing, capability parsing, restart
and refresh persistence, unavailable-model fallback, cancellation cleanup, and API-to-
CLI option propagation. `tests/summary_models_ui_smoke.py` covers both provider UIs
using simulated catalogs. Codex also has a synthetic live-run check. Claude's real
account execution still needs verification on a Mac with an installed, logged-in CLI.

The Mac transcription pipeline uses `src/local_diarization.py` and
`src/diarization_worker.py` to run Nemotron FP32 after releasing the ASR model.
The isolated runtime uses MPS when available and CPU otherwise. Its child watches
the backend parent and is reaped on cancellation; tests cover serialization with
MLX, failed setup, and process cleanup. CPU peak RSS is reported as RAM, never as
CUDA VRAM or combined unified-memory peak.

`src/speaker_records.py` retains cleanup of private speaker-analysis records
created by older app versions. They follow the note's seven-day recovery period.
There is no separate speaker-analysis setup, rerun or approval endpoint.

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
The review list selects notes without opening the editor; its separate edit
button opens the note dialog and its Trash icon uses the same version/busy-checked
file action as the context menu. `note_selection_ui_smoke.py` checks direct summary
handoff, per-note drafts, explicit editing, concurrent selections, load errors,
and focus retention during list refreshes in WebKit and Chromium.

`RecoverableNoteCleanup` links deletion to every note and document index entry
owning the exported path. Version/busy checks precede archiving a transcript in
`data/recycle-bin/`. `data/note-retention.sqlite3` records durable wall-clock
deletion timestamps; audio, note rows, summary and diarization state stay hidden
for seven days. Document IDs are reserved so a new file at the old pathname
cannot inherit the deleted file's history. `/api/trash` and its restore route
expose recoverable items; all other note routes reject hidden IDs.

A lifespan task checks missing exports every 30 seconds with a 60-second grace
period. Missing or moved files retain their private data for seven days too.
The original directory and user-selected library directory are checked for
unique file-identity or exact-content matches before scans/purges. Inaccessible
folders, symlinks, active jobs and ambiguous matches defer cleanup. Expiration
removes only the retained transcript and app-owned audio/records, preserving
external source audio, Voice Memos, profiles, caches, separate exports and
anonymous performance samples. `test_note_retention.py` and note-file API tests
cover recovery, expiry, ownership, failure retry and retained IDs.

`job_scheduler.py` dispatches immutable note snapshots by GPU UUID. Current
finite server jobs can run on distinct GPUs while each GPU stays FIFO; Mac
jobs serialize on one local resource. Legacy persistent workers use conservative
shared locks. Cancellation keeps the resource claimed until GPU cleanup ends.
`POST /api/notes/batch` validates selected uploads against one GPU inventory and
queues consumed uploads only after the batch has been checked; partial errors
leave failed uploads available for retry. Consuming an upload is atomic, so a
repeated request cannot duplicate work. The STEP 02 background job list does not
change the selected note or reopen its editor. Library summary markers derive
from persisted summary results/source hashes, including after app restart.
`test_job_scheduler.py`, `test_batch_notes.py`, `test_note_markers.py` and the
background/batch UI smoke tests cover these flows without model inference.

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

## Startup file-access gate

The native `FullDiskAccessGate` runs before `BackendController.start()`, updater
startup and `WorkspaceView` construction. Without FDA only the native permission
screen is presented: the backend cannot index the default Downloads directory
and cause an earlier per-folder prompt. The drag assistant is shared with the
Voice Memos flow. Activation/Done/recheck retries the check; a restart action is
available only before authorization and backend startup, and launches a new app
instance before terminating the old one. It never stops an active transcription.

There is no public FDA status API. The check opens an FDA-protected system or
per-user TCC database as a read-only, no-follow descriptor, verifies a regular
file, then closes it without reading bytes, parsing records or writing anything.
Both known locations are supported because the per-user database can be absent.
Readable Downloads or Voice Memos folders alone are not treated as FDA.
The grant is checked on every native process launch; successful startup is held
for that process so a permission change does not abruptly interrupt an active
job. Source/browser backend runs remain separate and use their caller's access.
`tests/startup_access_smoke.py` covers denied/granted/retry/fresh-launch states,
concurrent checks and no-follow descriptor probes, and renders the onboarding
screen offscreen without changing permissions or launching the backend.

## Voice profile pipeline

`VoiceEngine` explicitly prepares an isolated uv venv (sherpa-onnx 1.13.8,
numpy 2.2.6) and SHA-256-pinned 28MB CAM++ ONNX model under
`.cache/voice-engine/`. The separate worker uses CPU only. Setup checks and
repairs the environment; reads and transcription never initiate downloads.
Recording requires an explicit click, WKWebView microphone permission and
`NSMicrophoneUsageDescription`; camera access stays disabled.

`VoiceProfiles` stores profiles in a private `data/voice-profiles/profiles.sqlite3`.
The versioned `.mossvoice` format is bounded JSON containing a name, 16kHz mono
PCM sample, hash, model ID and normalized 192-dimensional embedding. Imports
validate size, schema, waveform, hash and vectors; they cannot supply executable
code, paths or model URLs. Export includes the sample for portability.

Enrollment normalizes a five-second excerpt with FFmpeg. Matching takes up to
three non-overlapping excerpts per diarized speaker, averages normalized
embeddings and compares cosine similarities by name. The provisional threshold
is 0.75 with a 0.10 lead over the next distinct name; these are not calibrated
probabilities. New transcriptions only store name/merge proposals, leaving names,
speaker IDs and automatic output files unchanged. Optional matching errors
preserve the transcript. Pairwise speaker comparisons can propose merges without
enrolled profiles; overlapping speakers are excluded and inconsistent excerpts
are marked for review. `static/voice-review.js` shows before/after identities and
counts, similarity scores and playable excerpts. Every checkbox starts unchecked.
Approval changes only the speaker-dialog draft; only its Save action updates the
note and output file. Cancel/Escape discard review selections or the dialog draft.
Manual identification returns the same read-only proposal format. Existing names
from older app versions are preserved, not reverted.
Original audio/text is never modified by registration, export, import or preview.
Subprocesses, microphone tracks and temporary excerpts are cleaned up on finish,
cancellation or shutdown. Registered profiles and the optional runtime persist.

`tests/test_voice_profiles.py` covers format boundaries, persistence, ambiguity,
origin checks and fallback on matching failure. `tests/voice_profiles_real_smoke.py`
uses a supplied real recording and actual ONNX runtime in temporary storage to
check registration, portable roundtrip, proposal-only completion and all result formats.
`tests/voice_profiles_ui_smoke.py` checks the same enrollment/import/edit flow in
WebKit with the supplied sample; it does not grant microphone permissions.
`tests/voice_review_ui_smoke.py` uses isolated note/file APIs and fixture scores
to check partial approval, before/after counts, sample playback, cancellation,
explicit saving and speaker merges in Chromium/WebKit at desktop/mobile widths.
Do functional QA with copies of existing Downloads files or the user's recordings;
keep originals unchanged and do not send their contents to AI services.

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
The desktop stages have equal widths and two fixed one-pixel separators. Earlier
saved width preferences are ignored; there are no drag or keyboard resize controls.
Narrow browser layouts hide the vertical separators. `tests/panes_ui_smoke.py`
checks fixed widths, ignored legacy preferences and responsive transitions.
The note list has an app-styled context menu for TXT/SRT/VTT conversion and
recoverable file deletion. `src/note_files.py` validates the indexed source and
an exact string file-version token before acting. macOS Foundation moves the
selected file to Trash via JXA without controlling Finder; managed audio/history
are retained for restoration. Conversion writes a new file using exclusive links,
keeps cue times and speakers, and exports ranged TXT timestamps for round trips.
Plain TXT without end times is rejected instead of inventing timings. Reused
export paths attach to the record matching the current file hash, and missing
exports are never recreated by an editor save. `test_note_files.py` and
`note_context_ui_smoke.py` cover these behaviors and menu interaction.
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
note moves its transcript into the app recycle bin and retains its managed audio
and summary for the seven-day recovery period.

New-note submissions pin `output_format` and `output_directory`. After inference,
the backend atomically writes that file before publishing the completed note;
no browser window or extra save/export action is required. Save failures preserve
the completed transcript and expose a file-only retry. The review list merges
completed jobs and indexed files by path and shows only one result card in the
selected folder, without an audio player or separate export controls. Legacy
manual exports with matching names and bytes are linked without rewriting them.
The editor shows the current transcript only. Its filename input commits on
Enter/blur and cancels on Escape. Only actual input marks a name draft dirty;
modal autofocus and opening another file cannot rename it. `/rename` preserves
the file extension, bytes, metadata and indexed document ID. Exclusive link
creation prevents collisions from overwriting files; matching managed paths are
updated together. The endpoint shares the file-action stale/busy checks and
local/same-origin guard. Text saves finish before renaming so hashes remain current.
`tests/test_automatic_output.py` verifies every format, pinned folders, collisions,
edit synchronization, denied writes, recovery without inference and route origins.
The speaker dialog scrolls its preview to the selected speaker's first utterance;
Up/Down skips other speakers, clamps at the ends and preserves text-field keys.
Clicking a speaker card focuses its inline name input, with Enter returning to
preview navigation. Clicking an already focused input preserves the caret and
preview position. Cards and utterance highlights use the green accent; drag
merging uses the card header so dragging name text does not merge speakers.
`tests/speaker_navigation_ui_smoke.py` exercises long TXT/SRT previews, focus,
scrolling, reduced motion, merge resets and source preservation.

`src/summaries.py` calls authenticated local Codex or Claude CLIs using argv and
stdin, without a shell. The source is a fixed snapshot of the edited TXT. Codex
runs read-only with user config, hooks, and shell tools disabled, and live web
search enabled for official terminology checks. Claude runs in print mode with
only WebSearch/WebFetch allowed, MCP disabled and configuration sources empty. Both use
temporary working directories and disable session persistence. The final result,
source digest, prompt, and status are saved in `data/summaries/<note-id>.json`.
Both providers return a schema-constrained object containing the complete corrected
transcript and Markdown summary. The app validates both fields and publishes one
`<stem>_요약.md`, starting with the summary and ending with a closed `<details>`
transcript toggle. Correction still precedes summarization in the AI request;
the app controls the final document order. The transcript is fenced as literal text with a delimiter longer than any
backtick run in the content. Publication never replaces existing files or symlinks;
collisions advance a numeric suffix, and failed publication removes temporary files.
Original files stay unchanged. Legacy saved summaries remain readable and custom
prompts are preserved when upgrading an unchanged default. A checked-by-default
checkbox includes the saved default request; its help dialog edits it without
requiring a selected note. Preferences are atomically saved in
`data/summary-preferences.json`; restoring the recommendation resets the saved
default. A run snapshots its base prompt before provider checks, so editing the
preference cannot change a running request. The STEP 03 editor holds only additional instructions.
Disabling the checkbox sends only the edited user request, and an empty custom-only
request is rejected. The textarea grows with its content and window width;
scrolling belongs to STEP 03.
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
uv run --no-project --with playwright==1.57.0 python tests/note_selection_ui_smoke.py http://127.0.0.1:8000 webkit /tmp/note-selection.png
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

## Processing estimates

The note dialog has a 45:55 two-column layout: model, processing controls and
estimates on the left, audio/note settings and the start button on the right.
The server adds GPU selection on the left; longer GPU lists scroll internally.
Narrow windows stack the columns.
CUDA chunk controls and estimate requests wait for a GPU number, so an unknown
hardware guess cannot appear as the selected server's expected latency.
The MLX dialog requests the selected model/count estimate after file preparation.
Only a matching measured Mac profile supplies displayed numerical predictions;
unmatched hardware, model folders or advanced settings say `추가 측정 필요`.

For local jobs marked `local_diarization="nemotron-3"`, `processing_estimates.py`
adds the Mac Nemotron reference only on Apple M4 with 16 GiB and a matching MLX
calibration. The single 2026-10-21 MPS observation used 3937.1306875 seconds of
audio: 28.704228708 seconds of inference and 36.901518417 seconds of fresh-process
wall time. The provisional additive estimate is
`8.197289709 + 28.704228708 * duration / 3937.1306875` seconds, once per recording,
regardless of MLX chunk count. Both components include process/model preparation,
but exclude initial downloads and file transfer. A single observation does not
validate linear scaling or the combined pipeline; the API marks its status
provisional and clears the ASR-only validation error for the combined prediction.
Other hardware or unmatched ASR configurations have no combined time estimate.

No combined MPS/unified-memory peak has been measured. Combined estimates return
`memory_expected_gib`, `memory_high_gib`, `memory_low_gib` and `memory_raw_gib` as
null with `memory_source="combined-unified-memory-unmeasured"`. Matched ASR-only
memory remains available under `asr_memory_expected_gib` / `asr_memory_raw_gib`
for diagnostics, never as the total. Nemotron process RSS is not substituted for
MPS allocation. Saved MOSS-only jobs without the marker retain their original
memory estimates.

`src/duration_calibrations.json` contains the public numerical export of the
2026-10-23 duration study, consumed by `src/duration_estimates.py`. It includes
three M4 16-GiB MLX weight variants and three H200 speech presets. Profiles are
scoped by hardware/capacity, runtime, public model/revision where available and
measured settings; H200 profiles cover the effective automatic batch only.
No audio, transcripts, source hashes, hostnames, paths or credentials are included.
The release bundle copies this JSON with the rest of `src`.

Monotone piecewise interpolation uses work-time and memory knots, with median
fresh-process setup added separately. MLX sums equal-length main chunks and
30-second boundary bridge work; memory takes the main-context peak rather than
summing sequential chunks. The displayed budget is `raw_peak*1.12+headroom`,
with 0.3 GiB for Metal allocation or 1 GiB for server process VRAM. These metrics
are not interchangeable with OS-wide memory or host RSS. Server setup includes
all sequential model stages; downloading and transport are excluded.

The 22-minute validation prefix was excluded from fitting. MLX 4-bit time remains
provisional (30.6% validation error); MLX 8-bit has no successful held-out timing.
The UI shows these limits and extrapolation in amber. No valid 4-bit main timing
above 30 minutes exists, so its time stays unknown. Qwen waves above the measured
duration are structural extrapolation. See the [measurement report](performance/2026-10-23-duration-calibration.md)
for failures, scope and validation errors. Existing local server observations can
replace a nearby measured point; a short observation cannot flatten the whole
length curve. Unmatched server hardware still learns from compatible observations.
The new calibration import in the remote attribution catalog is lazy: bootstrap
speaker attribution does not depend on the client's calibration JSON/module.

`tests/test_duration_estimates.py` checks held-out bounds, identity/option scoping,
memory budgets, observation handling and API parity. Run
`python tests/duration_estimate_ui_smoke.py URL webkit PNG` and
`python tests/speech_pipeline_ui_smoke.py URL PNG webkit` against an isolated app
server for Mac and server model/count changes, uncertainty and responsive layout.

The running-job card shows one estimated model progress bar instead of pipeline
steps, token/prefill sub-bars or timestamp/chunk counters. At job start, the
selected GPU name/capacity is captured in the private device snapshot. After
normalization/splitting and model preparation, `estimate_note_inference` freezes
the profile estimate for the actual chunk count in `estimated_inference_seconds`.
The request window is persisted as `inference_started_at`/`inference_finished_at`.
It includes transcription-request transport and waiting, all main chunks and
model-generated boundary bridges, and ends before CPU merge, optional voice
recommendations or file saving. It is a client request timer, not a GPU profiler.
MLX uses the same measured profile as the dialog when available, with a rough
backend fallback for this running-job bar on unmeasured hardware/settings.
The UI advances once per second using the elapsed/estimated ratio, caps it at
99% until the final model response, and then shows 100% even while the note is
being finalized. The calibrated dialog estimate includes preparation, which can
already have finished before the MLX request timer starts; it is an approximation
for progress. Older request-only profiles exclude preparation.
Cancellation/failure freeze the clock. Restart and queue recovery reset its
timestamps and estimate. Unknown estimates retain an indeterminate indicator.
`tests/test_inference_progress.py` checks timing and estimation boundaries;
`tests/transcription_progress_ui_smoke.py` checks live ticks, the 99% plateau,
completion before postprocessing, stop/retry/error states and responsive layout.

`src/processing_estimates.py` returns point estimates: `time_seconds` and
`memory_expected_gib`. GPU names no longer select fixed timing coefficients.
The following request-only estimator remains for legacy CUDA worker jobs.
`src/performance_seeds.json` holds reference observations; the existing four H200
cases are the first dataset, not a separate algorithm. Completed CUDA requests
add local numerical measurements to `data/performance.sqlite3` through
`src/performance_history.py`. No extra inference, background benchmarking or
model installation is started to collect them.

The worker synchronizes CUDA, resets peak counters before each request, measures
inference/feature preparation/decode and returns peaks and generated-token counts.
Model loading, client upload, network/queue time and final speaker merge are
excluded. The first request is marked cold; cold/language/load variation remains
in the measured distribution. Cancelled, failed, empty/unparseable transcripts and
runs hitting the output cap do not become calibration samples. Older workers
without telemetry still transcribe successfully and keep the initial estimate.
Only GPU type/memory class, model, settings, runtime versions, durations, token
counts, cold-start flag and reserved peak are retained; no audio, transcript,
filename, note ID, GPU UUID, host or credentials are saved in this history.

Profiles separate GPU name and 8-GiB VRAM class, model/revision, effective and
requested precision, attention, cache use, output cap and engine/software versions.
Automatic precision only uses observations made in automatic mode. The newest
matching software environment is used; different environments are never pooled.
Each profile retains its latest 200 local observations across app restarts and
device re-registration. Known hardware before selection can also supply a
provisional profile through the API when every server GPU has the same type;
mixed GPU types wait for an explicit UUID. The UI always requires that selection.

For duration D, count N and bridge length B, the common model is
`T(D,N,B) = N*f(D/N) + (N-1)*g(min(B,D/N))`.
Main chunks and boundary bridges have separate points. Minute buckets take
median duration/time and the largest observed reserved footprint; monotone
interpolation retains context-length effects. Below the shortest point, or with
only one main observation, time scales proportionally. Above the last point it
uses the final measured slope. Extrapolation is recorded in the API metadata. The UI displays values and capacity
warnings without the permanent source/scope sentence. A bridge
without direct measurements falls back to the main chunk model. This empirical
model does not infer attention's asymptotic complexity or claim accuracy from a
single recording. Memory follows the main chunk context (bridges are capped to that length),
adds 1 GiB context/execution headroom, and uses `1.25*reserved+1 GiB` internally for
capacity warnings. A lone memory observation supports only coarse extrapolation;
more varied durations are needed to learn context growth. Bridge reserved counters
can retain blocks from preceding long chunks, so they do not inflate the
prediction of a new short job.

The H200 seed preserves all four measured totals by subtracting the mean observed
30-second bridge cost (2.674s) from two-chunk totals before dividing by two. Its
main points are 15, 30, 32.81 and 65.62 minutes. The existing original BF16/auto,
SDPA, KV-enabled, 32768-token settings match that data. Unsupported settings or
new GPUs begin with a generic initial estimate and acquire their
own measurements on ordinary successful transcriptions. No H200 performance is
assigned to an unmeasured different GPU. Changing speech density, prompts, shared
GPU load, power limits or software can still change real latency and memory.

`scripts/benchmark-cuda.py` runs one explicitly UUID-bound case in a prepared
offline runtime. It verifies the observed cache class, excludes load/warmup from
inference latency, records per-chunk first-token/decode timing and CUDA peak
allocated/reserved memory, and keeps transcripts in a private output directory.
`scripts/summarize-cuda-benchmark.py` (optional `rapidfuzz==3.14.3`) applies the
app's parser and boundary-bridge speaker merger and exports only metrics. Relative
character edits and speaker agreement are comparisons to the original-KV result,
not accuracy/CER/DER against human annotations. Never commit the private audio or
transcripts.

- The memory model uses the published MOSS configuration (28 decoder layers,
  8 KV heads, head dimension 128), an approximate 12.5 audio tokens/second,
  output-token allowance, weights, KV precision/cache settings and coarse audio
  activation/runtime allowances. Eager attention adds a quadratic score-matrix
  term. Outside H200 calibration, the displayed point is this rough peak and
  capacity warnings use 1.4 times the peak. Custom models return no estimate.
- Without a matching measured profile, time uses one generic backend ratio, with
  a mix of linear/context-length costs, bridge processing and per-chunk overhead.
  It returns one rounded guess and excludes model preparation and file transfer;
  this fallback does not claim measured hardware performance.
- The capacity comparison uses the selected GPU's server-reported inventory.
  Total-VRAM overflow is red; only free-VRAM overflow is informational. A zero
  free-memory reading is preserved. Free-memory samples older than 10 seconds
  are not used. Warnings do not block job submission.
- Default chunk caps are 30 minutes on Mac and 30/60/90 on CUDA, with boundaries
  at 40/72 GiB to accommodate reserved memory on nominal 48/80 GB GPUs. A server
  with mixed GPUs starts from the smallest known VRAM; unknown inventories are
  not persisted as automatic settings. Initial setup only queries nvidia-smi,
  without selecting a GPU or loading the model onto one.
- `PATCH /api/devices/{id}/chunk-default` saves a cap without preparing a model.
  Automatic initialization preserves already configured caps. The note dialog
  computes `ceil(duration / cap)` and submits only a count for equal splitting.

Verification: `tests/test_processing_estimates.py`, `tests/setup_ui_smoke.py`,
and `tests/chunk_selection_ui_smoke.py` (with an existing recording).

## Product website

`docs/index.html` is the buildless product website. Styles, interactions and the
favicon live in `docs/assets/`; screenshots are shared with the README in
`docs/images/`. All asset paths are relative so the site works under the
`/cosMOSS/` GitHub Pages project path. Existing Markdown guides remain intact.

Preview locally with `python3 -m http.server 18777 --bind 127.0.0.1 --directory docs`,
then open `http://127.0.0.1:18777/`. No package installation or build is required.
`docs/.nojekyll` keeps GitHub Pages from transforming the static files. Version
labels and download links in the HTML should be updated with future releases.
