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
from src.model_catalog import has_model_selection


def original_cuda_configuration(device: dict[str, Any]) -> dict[str, Any]:
    """Retire saved server quantization choices without changing Mac options."""
    if device.get('backend') != 'cuda':
        return device
    device = device.copy()
    options = dict(device.get('options', {}))
    options.pop('cuda_kv_bits', None)
    if options.get('precision') in {'int4', 'int8'}:
        options['precision'] = 'auto'
    if 'options' in device:
        device['options'] = options
    if device.get('model_key') in {'cuda-4bit', 'cuda-8bit'}:
        device.update(model_key='cuda-original', model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
    return device


class DeviceStore:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, config TEXT NOT NULL)")
            for device_id, raw in connection.execute('SELECT id, config FROM devices').fetchall():
                saved = json.loads(raw)
                migrated = original_cuda_configuration(saved)
                if migrated != saved:
                    connection.execute('UPDATE devices SET config=? WHERE id=?', (json.dumps(migrated), device_id))
            local = {
                "id": "local", "name": "이 Mac", "kind": "local", "backend": "mlx",
                "model": os.getenv("MOSS_MODEL_NAME", "moss-mtd"),
                "remote_port": int(os.getenv("MOSS_MLX_PORT", "8001")),
            }
            previous = connection.execute("SELECT config FROM devices WHERE id = 'local'").fetchone()
            if previous:
                local = {**json.loads(previous[0]), **local}
            connection.execute("INSERT INTO devices VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET config=excluded.config", ("local", json.dumps(local)))

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def list(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute("SELECT config FROM devices ORDER BY CASE id WHEN 'local' THEN 0 ELSE 1 END, rowid").fetchall()
        return [original_cuda_configuration(json.loads(row[0])) for row in rows]

    def get(self, device_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT config FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        return original_cuda_configuration(json.loads(row[0]))

    def add_ssh(self, *, name: str, host: str, remote_port: int, model: str, backend: str,
                model_cache: str = "") -> dict[str, Any]:
        device = {"id": uuid.uuid4().hex, "kind": "ssh", "name": name, "host": host,
                  "remote_port": remote_port, "model": model, "backend": backend,
                  "model_cache": model_cache, "model_prepared": False}
        if backend == "cuda":
            device['model_selected'] = False
            device["api_key"] = secrets.token_urlsafe(32)
            device["worker_cleanup_pending"] = False
        with self.lock, self._connect() as connection:
            for row in connection.execute("SELECT config FROM devices"):
                previous = json.loads(row[0])
                if previous.get("host") == host and (previous["remote_port"] == remote_port or
                                                     backend == previous.get("backend") == "cuda"):
                    raise ValueError("이 SSH 호스트와 포트는 이미 등록돼 있습니다.")
            connection.execute("INSERT INTO devices VALUES (?, ?)", (device["id"], json.dumps(device)))
        return device

    def set_model(self, device_id: str, source: str, path: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            device = self.get(device_id)
            device.update(model_source=source, model_path=path if source == "folder" else "", model_prepared=True)
            connection.execute("UPDATE devices SET config = ? WHERE id = ?", (json.dumps(device), device_id))
        return device

    def configure(self, device_id: str, **values) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            device = self.get(device_id)
            device.update(values)
            device = original_cuda_configuration(device)
            connection.execute("UPDATE devices SET config = ? WHERE id = ?", (json.dumps(device), device_id))
        return device

    def initialize_chunk_default(self, device_id: str, minutes: int, hardware: dict | None = None) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            device = self.get(device_id)
            # A model/settings save may race the first hardware report.
            device['options'] = {'chunk_minutes':minutes, **device.get('options', {})}
            if hardware:
                device['hardware'] = hardware
            connection.execute('UPDATE devices SET config=? WHERE id=?', (json.dumps(device), device_id))
        return device

    def set_chunk_default(self, device_id: str, minutes: int) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            device = self.get(device_id)
            device['options'] = {**device.get('options', {}), 'chunk_minutes':minutes}
            connection.execute('UPDATE devices SET config=? WHERE id=?', (json.dumps(device), device_id))
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
    return {**{key: value for key, value in device.items() if key not in {"api_key", "gpu_uuid", "gpu_index"}},
            'model_selected': has_model_selection(device)}


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
