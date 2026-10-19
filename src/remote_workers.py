"""Prepare and stop only explicitly selected, UUID-bound remote workers."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any

from src.ssh_devices import SSHError, discover_hosts, ssh_arguments


class RemoteWorkers:
    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self.states: dict[str, dict[str, Any]] = {}

    def start(self, device: dict[str, Any], action: str = "prepare") -> dict[str, Any]:
        device_id = device["id"]
        task = self.tasks.get(device_id)
        if task and not task.done():
            return self.states[device_id]
        self.states[device_id] = {"phase": "connecting"}
        task = asyncio.create_task(self.run(device, action))
        self.tasks[device_id] = task
        return self.states[device_id]

    async def run(self, device: dict[str, Any], action: str) -> None:
        device_id = device["id"]
        process = None
        try:
            if device["host"] not in discover_hosts():
                raise SSHError("SSH config에서 호스트를 찾을 수 없습니다.")
            root = Path(__file__).parent
            bootstrap = (root / "remote_bootstrap.py").read_text()
            payload = json.dumps({"device": device, "action": action, "source": (root / "cuda_server.py").read_text()}).encode()
            arguments = ssh_arguments(device["host"]) + ["-o", "ClearAllForwardings=yes", "--", device["host"],
                                                         "python3 -u -c " + shlex.quote(bootstrap)]
            process = await asyncio.create_subprocess_exec(
                *arguments, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,
            )
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(1800):
                async for line in process.stdout:
                    text = line.decode(errors="replace").strip()
                    if text.startswith("COSMOSS:"):
                        self.states[device_id].update(json.loads(text[len("COSMOSS:"):]))
                    elif text:
                        self.states[device_id]["detail"] = text[-500:]
                code = await process.wait()
            if code and self.states[device_id]["phase"] != "error":
                raise SSHError(self.states[device_id].get("detail", "원격 워커 준비에 실패했습니다."))
            if not code and action == "stop":
                self.states[device_id] = {"phase": "stopped"}
        except asyncio.CancelledError:
            self.states[device_id] = {"phase": "interrupted", "error": "연결이 종료되었습니다. 원격 워커는 서버 확인으로 다시 확인해 주세요."}
            raise
        except Exception as error:
            self.states[device_id] = {"phase": "error", "error": str(error)}
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()

    async def close(self) -> None:
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
