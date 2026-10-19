"""Persistent execution targets, independent of notes and model runtimes."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import TranscriptionConfig
from src.ssh_devices import SSHTunnels


class DeviceStore:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, config TEXT NOT NULL)")
            local = {
                "id": "local", "name": "이 Mac", "kind": "local", "backend": "mlx",
                "model": os.getenv("MOSS_MODEL_NAME", "moss-mtd"),
                "remote_port": int(os.getenv("MOSS_MLX_PORT", "8001")),
            }
            connection.execute("INSERT INTO devices VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET config=excluded.config", ("local", json.dumps(local)))

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def list(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute("SELECT config FROM devices ORDER BY CASE id WHEN 'local' THEN 0 ELSE 1 END, rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, device_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT config FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        return json.loads(row[0])

    def add_ssh(self, *, name: str, host: str, remote_port: int, model: str, backend: str,
                gpu_uuid: str | None = None, gpu_index: int | None = None) -> dict[str, Any]:
        device = {"id": uuid.uuid4().hex, "kind": "ssh", "name": name, "host": host,
                  "remote_port": remote_port, "model": model, "backend": backend,
                  "gpu_uuid": gpu_uuid, "gpu_index": gpu_index}
        if backend == "cuda":
            device["api_key"] = secrets.token_urlsafe(32)
        with self.lock, self._connect() as connection:
            for row in connection.execute("SELECT config FROM devices"):
                previous = json.loads(row[0])
                if previous.get("host") == host and previous["remote_port"] == remote_port:
                    raise ValueError("이 SSH 호스트와 포트는 이미 등록돼 있습니다.")
                if gpu_uuid and previous.get("host") == host and previous.get("gpu_uuid") == gpu_uuid:
                    raise ValueError("이 호스트의 GPU는 이미 등록돼 있습니다.")
            connection.execute("INSERT INTO devices VALUES (?, ?)", (device["id"], json.dumps(device)))
        return device

    def delete(self, device_id: str) -> None:
        if device_id == "local":
            raise ValueError("기본 로컬 장치는 삭제할 수 없습니다.")
        with self.lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        if not cursor.rowcount:
            raise KeyError(device_id)


@dataclass(frozen=True)
class ResolvedDevice:
    config: TranscriptionConfig
    model: str
    name: str
    device_id: str = ""
    gpu_uuid: str | None = None


def public_device(device: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in device.items() if key != "api_key"}


async def resolve_device(device: dict[str, Any], tunnels: SSHTunnels) -> ResolvedDevice:
    if device["kind"] == "ssh":
        url = await tunnels.endpoint(device["id"], device["host"], device["remote_port"])
    else:
        url = f"http://127.0.0.1:{device['remote_port']}/v1"
    return ResolvedDevice(
        TranscriptionConfig.from_env({"MOSS_TRANSCRIPTION_BACKEND": "remote" if device["backend"] == "cuda" else device["backend"],
                                      "MOSS_TRANSCRIPTION_URL": url,
                                      "MOSS_TRANSCRIPTION_API_KEY": device.get("api_key", "")}),
        device["model"], device["name"], device["id"], device.get("gpu_uuid"),
    )
