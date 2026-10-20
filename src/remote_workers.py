"""Prepare and stop only explicitly selected, UUID-bound remote workers."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any, Callable

from src.ssh_devices import SSHError, discover_hosts, ssh_arguments


class RemoteWorkers:
    def __init__(self, on_stopped: Callable[[str], None] | None = None):
        self.on_stopped = on_stopped
        self.tasks: dict[str, asyncio.Task] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def start(self, device: dict[str, Any], action: str = "install") -> dict[str, Any]:
        device_id = device["id"]
        task = self.tasks.get(device_id)
        if task and not task.done():
            return self.states[device_id]
        self.states[device_id] = {**self.states.get(device_id, {}), "phase": "connecting", "action": action}
        self.states[device_id].pop("error", None)
        task = asyncio.create_task(self.run(device, action))
        self.tasks[device_id] = task
        return self.states[device_id]

    async def perform(self, device: dict[str, Any], action: str) -> dict[str, Any]:
        """Serialize setup and per-job worker actions for each registered device."""
        async with self.locks.setdefault(device["id"], asyncio.Lock()):
            current = self.tasks.get(device["id"])
            if current and not current.done():
                await asyncio.shield(current)
            self.start(device, action)
            await self.tasks[device["id"]]
            result = self.states[device["id"]]
            if result["phase"] in {"error", "interrupted"}:
                raise RuntimeError(result.get("error", "원격 작업을 완료하지 못했습니다."))
            return result

    async def execute(self, device, action, on_event):
        process = None
        detail = ""
        remote_error = ""
        try:
            if device["host"] not in discover_hosts():
                raise SSHError("SSH config에서 호스트를 찾을 수 없습니다.")
            root = Path(__file__).parent
            bootstrap = (root / "runtime_environment.py").read_text() + "\n\n" + (root / "remote_bootstrap.py").read_text()
            payload = json.dumps({"device": device, "action": action, "source": (root / "cuda_server.py").read_text(),
                                  "model_source": (root / "model_cache.py").read_text(),
                                  "download_source": (root / "model_download.py").read_text()}).encode()
            arguments = ssh_arguments(device["host"]) + ["-o", "ClearAllForwardings=yes", "--", device["host"],
                                                         "python3 -u -c " + shlex.quote(bootstrap)]
            process = await asyncio.create_subprocess_exec(
                *arguments, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,
            )
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(20 if action == "options" else 60 if action in {"environment", "inspect"} else 1800):
                async for line in process.stdout:
                    text = line.decode(errors="replace").strip()
                    if text.startswith("COSMOSS:"):
                        event = json.loads(text[len("COSMOSS:"):])
                        on_event(event)
                        if event.get("phase") == "error":
                            remote_error = event.get("error", "")
                    elif text:
                        detail = text[-500:]
                code = await process.wait()
            if code:
                raise SSHError(remote_error or detail or "연결한 장치에서 요청을 완료하지 못했습니다.")
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()

    async def options(self, device):
        result = {}
        await self.execute(device, "options", result.update)
        if result.get("phase") == "error":
            raise SSHError(result.get("error", "모델 위치를 확인하지 못했습니다."))
        if not result.get("cache_dir"):
            raise SSHError("장치의 Hugging Face 경로를 확인하지 못했습니다.")
        return {**result, **self.states.get(device["id"], {"phase": "idle"})}

    async def environment(self, device):
        result = {}
        await self.execute(device, "environment", result.update)
        if not result.get("runtime"):
            raise SSHError(result.get("error", "실행 환경을 확인하지 못했습니다."))
        self.states.setdefault(device["id"], {"phase": "unknown"})["runtime"] = result["runtime"]
        return result["runtime"]

    async def run(self, device: dict[str, Any], action: str) -> None:
        device_id = device["id"]
        try:
            await self.execute(device, action, self.states[device_id].update)
            if action == "stop":
                self.states[device_id] = {**self.states[device_id], "phase": "stopped", "active_gpu_uuid": None, "worker_cleanup_pending": False}
            if self.on_stopped and (action == "stop" or (action == "inspect" and self.states[device_id].get("worker_cleanup_pending") is False)):
                self.on_stopped(device_id)
        except asyncio.CancelledError:
            self.states[device_id] = {"phase": "interrupted", "error": "연결이 종료되었습니다. 모델 준비를 다시 확인해 주세요."}
            raise
        except Exception as error:
            previous = self.states[device_id]
            detail = previous.get("error", str(error))
            if action == "stop":
                detail = "이전 전사 작업을 정리하지 못했습니다. 연결을 확인한 뒤 ‘작업 정리’로 다시 시도해 주세요. " + detail
            self.states[device_id] = {**previous, "phase": "error", "error": detail}

    async def close(self) -> None:
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
