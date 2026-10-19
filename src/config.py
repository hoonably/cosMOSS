"""Device-independent configuration for the transcription client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TranscriptionConfig:
    backend: str
    url: str
    api_key: str
    stream: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TranscriptionConfig:
        values = os.environ if env is None else env
        backend = values.get("MOSS_TRANSCRIPTION_BACKEND", "mlx").strip().lower()
        if backend not in {"mlx", "remote"}:
            raise ValueError("MOSS_TRANSCRIPTION_BACKEND must be mlx or remote")
        url = (
            values.get("MOSS_TRANSCRIPTION_URL")
            or values.get("MOSS_MLX_URL")
            or "http://127.0.0.1:8001/v1"
        ).rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MOSS_TRANSCRIPTION_URL must be an HTTP(S) server URL")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("Use a plain server URL and MOSS_TRANSCRIPTION_API_KEY for authentication")
        stream_value = values.get(
            "MOSS_TRANSCRIPTION_STREAM", "true" if backend == "mlx" else "false"
        ).strip().lower()
        if stream_value not in {"true", "false", "1", "0"}:
            raise ValueError("MOSS_TRANSCRIPTION_STREAM must be true or false")
        return cls(
            backend=backend,
            url=url,
            api_key=values.get("MOSS_TRANSCRIPTION_API_KEY", ""),
            stream=stream_value in {"true", "1"},
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @property
    def label(self) -> str:
        return "MLX · Apple Silicon" if self.backend == "mlx" else "연결된 전사 서버"
