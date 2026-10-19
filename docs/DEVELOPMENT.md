# Development

## Boundaries

The SwiftUI app owns only its local FastAPI process. It does not start an MLX or
CUDA model server. The browser editor talks to FastAPI; FastAPI talks to the
transcription endpoint. Keep device-specific inference code outside the editor
and note store.

The current macOS bundle is a development launcher referencing its checkout via
`CosMOSSProjectRoot` in Info.plist. Python, FFmpeg, models and inference runtimes
are not bundled. Finder launches add Homebrew's executable directories to the
child process PATH so FFmpeg can be found.

The app binds its backend to loopback, uses port 18760 by default, verifies the
`/healthz` application marker, and terminates only a process it started. Use the
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
implementing and verifying the CUDA adapter is a separate milestone.

`MOSS_TRANSCRIPTION_API_KEY`, when set, becomes a Bearer header on health,
transcription, and cancellation requests. It is never included in UI responses.
The application-level `MOSS_AUTH_*` credentials remain separate.

## Validation

Run `python -m unittest discover -s tests -v` inside the project's environment.
The tests use HTTPX's mock transport and temporary audio/database paths. They
require neither model weights nor GPU access and do not establish transcription
accuracy or RTX 3090 compatibility.

Build the Swift application with `./scripts/build-mac-app.sh`. For manual QA:

1. Open `dist/cosMOSS.app` with no inference server: the editor should open and
   show a disconnected transcription service.
2. Start the MLX server separately; check that the status becomes ready.
3. Upload a short recording, edit text and speaker names, then export each format.
4. Close the final app window; the owned FastAPI process should exit.
5. Repeat with a separately started FastAPI server on the desktop port; quitting
   the app must leave that pre-existing server running.

## Initial snapshot

Application source was imported from ginjae/mac-moss commit
`d69b3dbc227e9cee5c9beb8523e5b26b311b2b40`. Its Git metadata, private files,
recordings, model weights, environments, and caches were not copied.
