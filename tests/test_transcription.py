"""Exercise the device boundary without downloading a model or using a GPU."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.config import TranscriptionConfig


_storage = tempfile.TemporaryDirectory(prefix="cosmoss-tests-")
with patch.dict(os.environ, {"MOSS_DATA_DIR": _storage.name}):
    from src import main


class ConfigTests(unittest.TestCase):
    def test_local_defaults(self):
        config = TranscriptionConfig.from_env({})
        self.assertEqual(config.url, "http://127.0.0.1:8001/v1")
        self.assertTrue(config.stream)
        self.assertEqual(config.headers, {})

    def test_remote_defaults_and_authentication(self):
        config = TranscriptionConfig.from_env({
            "MOSS_TRANSCRIPTION_BACKEND": "remote",
            "MOSS_TRANSCRIPTION_URL": "https://gpu.example/v1/",
            "MOSS_TRANSCRIPTION_API_KEY": "test-token",
        })
        self.assertFalse(config.stream)
        self.assertEqual(config.url, "https://gpu.example/v1")
        self.assertEqual(config.headers, {"Authorization": "Bearer test-token"})

    def test_legacy_url_and_new_url_precedence(self):
        env = {"MOSS_MLX_URL": "http://legacy.example/v1"}
        self.assertEqual(TranscriptionConfig.from_env(env).url, env["MOSS_MLX_URL"])
        env["MOSS_TRANSCRIPTION_URL"] = "http://gpu.example/v1"
        self.assertEqual(TranscriptionConfig.from_env(env).url, env["MOSS_TRANSCRIPTION_URL"])

    def test_streaming_can_be_overridden(self):
        self.assertFalse(TranscriptionConfig.from_env({"MOSS_TRANSCRIPTION_STREAM": "false"}).stream)
        self.assertTrue(TranscriptionConfig.from_env({
            "MOSS_TRANSCRIPTION_BACKEND": "remote", "MOSS_TRANSCRIPTION_STREAM": "true",
        }).stream)

    def test_invalid_configuration_is_rejected(self):
        for env in [
            {"MOSS_TRANSCRIPTION_BACKEND": "cuda"},
            {"MOSS_TRANSCRIPTION_URL": "file:///etc/passwd"},
            {"MOSS_TRANSCRIPTION_URL": "https://user:secret@example.com/v1"},
            {"MOSS_TRANSCRIPTION_URL": "https://example.com/v1?token=secret"},
            {"MOSS_TRANSCRIPTION_STREAM": "maybe"},
        ]:
            with self.subTest(env=env), self.assertRaises(ValueError):
                TranscriptionConfig.from_env(env)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.audio_dir = tempfile.TemporaryDirectory(prefix="cosmoss-audio-")
        self.addCleanup(self.audio_dir.cleanup)
        self.audio = Path(self.audio_dir.name) / "clip.wav"
        self.audio.write_bytes(b"RIFF-test-audio")
        self.note = {"language": "ko", "hotwords": "cosMOSS"}

    def configure(self, backend="remote", api_key="test-token"):
        config = TranscriptionConfig.from_env({
            "MOSS_TRANSCRIPTION_BACKEND": backend,
            "MOSS_TRANSCRIPTION_URL": "https://gpu.example/v1",
            "MOSS_TRANSCRIPTION_API_KEY": api_key,
        })
        self.enterContext(patch.object(main, "TRANSCRIPTION", config))
        self.enterContext(patch.object(main, "TRANSCRIPTION_URL", config.url))

    async def test_remote_json_parses_speakers_without_custom_stream_fields(self):
        self.configure()
        seen = []

        async def respond(request):
            body = await request.aread()
            seen.append((request, body))
            return httpx.Response(200, json={"text": "[0.0][S01]안녕하세요[1.5]"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            raw, segments = await main.transcribe_chunk(
                client, self.audio, self.note, lambda _: None, "request-1",
            )
        request, body = seen[0]
        self.assertEqual(str(request.url), "https://gpu.example/v1/audio/transcriptions")
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        self.assertNotIn(b'name="stream"', body)
        self.assertNotIn(b'name="request_id"', body)
        self.assertIn(b"cosMOSS", body)
        self.assertEqual(segments[0]["speaker"], "S01")
        self.assertEqual(segments[0]["text"], "안녕하세요")

    async def test_remote_segments_fallback(self):
        self.configure()

        def respond(request):
            return httpx.Response(200, json={"text": "plain transcript", "segments": [
                {"start": 0, "end": 2, "speaker": "S02", "text": " 두 번째 화자 "},
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            _, segments = await main.transcribe_chunk(client, self.audio, self.note)
        self.assertEqual(segments[0], {
            "id": 0, "start": 0.0, "end": 2.0, "speaker": "S02", "text": "두 번째 화자",
        })

    async def test_mlx_ndjson_progress_and_result(self):
        self.configure("mlx")
        progress = []

        async def respond(request):
            body = await request.aread()
            self.assertIn(b'name="stream"', body)
            self.assertIn(b'name="request_id"', body)
            events = [
                {"type": "progress", "stage": "prefill", "prompt_processed": 50},
                {"type": "heartbeat"},
                {"type": "result", "text": "[0-1][S01]첫 발언[1-2][S02]다음 발언"},
            ]
            return httpx.Response(200, text="\n".join(map(json.dumps, events)),
                                  headers={"content-type": "application/x-ndjson"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            _, segments = await main.transcribe_chunk(
                client, self.audio, self.note, progress.append, "request-1",
            )
        self.assertEqual(progress, [{"stage": "prefill", "prompt_processed": 50}])
        self.assertEqual([item["speaker"] for item in segments], ["S01", "S02"])

    async def test_server_error_is_not_treated_as_a_transcript(self):
        self.configure()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "unauthorized"}),
        )) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await main.transcribe_chunk(client, self.audio, self.note)

    async def test_health_authentication_and_no_secret_in_ui_response(self):
        self.configure()

        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(200, json={"data": [{"id": "moss-mtd"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            health = await main.model_health()
        self.assertTrue(health["moss"]["ok"])
        self.assertEqual(health["moss"]["backend"], "remote")
        self.assertNotIn("test-token", json.dumps(health))

    async def test_app_opens_without_an_inference_runtime(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://cosmoss.test",
        ) as client:
            response = await client.get("/healthz")
            self.assertEqual(response.json(), {"ok": True, "app": "cosMOSS"})
            response = await client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("<title>cosMOSS</title>", response.text)

    async def test_invalid_model_list_does_not_break_the_app(self):
        self.configure()
        for response in [
            httpx.Response(200, text="This is not a model server"),
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": [{"id": "a-different-model"}]}),
        ]:
            with self.subTest(response=response):
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
                with patch.object(main.httpx, "AsyncClient", return_value=client):
                    health = await main.model_health()
                self.assertFalse(health["moss"]["ok"])
                self.assertIn("error", health["moss"])


if __name__ == "__main__":
    unittest.main()
