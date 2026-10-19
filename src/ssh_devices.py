"""Discover SSH aliases and inspect GPUs without changing remote machines."""

from __future__ import annotations

import asyncio
import glob
import re
import shlex
import shutil
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
GPU_COMMAND = "LC_ALL=C nvidia-smi -q -x"


class SSHError(RuntimeError):
    pass


def discover_hosts(config: Path | None = None) -> list[str]:
    """Read literal aliases and Include files; never evaluate Match exec or ssh -G."""
    config = config or Path.home() / ".ssh" / "config"
    ssh_dir = config.parent
    visited: set[Path] = set()
    aliases: set[str] = set()

    def read(filename: Path, depth: int = 0) -> None:
        filename = filename.resolve()
        if filename in visited or depth > 16 or len(visited) >= 128:
            return
        visited.add(filename)
        try:
            if filename.stat().st_size > 1024 * 1024:
                return
            lines = filename.read_text().splitlines()
        except (OSError, UnicodeError):
            return
        for line in lines:
            try:
                # OpenSSH accepts both `Host name` and `Host=name`.
                parts = shlex.split(re.sub(r"^(\s*\w+)\s*=\s*", r"\1 ", line), comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            if parts[0].lower() == "host":
                aliases.update(alias for alias in parts[1:] if ALIAS_PATTERN.fullmatch(alias))
            elif parts[0].lower() == "include":
                for pattern in parts[1:]:
                    candidate = Path(pattern).expanduser()
                    if not candidate.is_absolute():
                        candidate = ssh_dir / candidate
                    for included in sorted(glob.glob(str(candidate))):
                        read(Path(included), depth + 1)

    read(config)
    return sorted(aliases, key=str.casefold)


def ssh_arguments(alias: str) -> list[str]:
    if not ALIAS_PATTERN.fullmatch(alias):
        raise SSHError("올바르지 않은 SSH 별칭입니다.")
    executable = shutil.which("ssh")
    if not executable:
        raise SSHError("OpenSSH 클라이언트를 찾을 수 없습니다.")
    return [
        executable, "-T",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
        "-o", "ControlMaster=no", "-o", "ControlPath=none",
        "-o", "ForwardAgent=no", "-o", "PermitLocalCommand=no",
        "-o", "RemoteCommand=none",
        "-o", "LogLevel=ERROR",
    ]


def number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:MiB|%)?\s*", value)
    return float(match[1]) if match else None


def parse_gpu_xml(xml: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    if root.tag != "nvidia_smi_log":
        raise ValueError("올바른 nvidia-smi 응답이 아닙니다.")
    gpus = []
    for index, gpu in enumerate(root.findall("gpu")):
        processes_node = gpu.find("processes")
        processes = []
        if processes_node is not None:
            for process in processes_node.findall("process_info"):
                processes.append({
                    "pid": process.findtext("pid"),
                    "name": process.findtext("process_name", ""),
                    "type": process.findtext("type", ""),
                    "memory_mib": number(process.findtext("used_memory")),
                })
        utilization = number(gpu.findtext("utilization/gpu_util"))
        total = number(gpu.findtext("fb_memory_usage/total"))
        free = number(gpu.findtext("fb_memory_usage/free"))
        process_info_known = processes_node is not None and (processes_node.text or "").strip() in {"", "None"}
        compute_processes = [process for process in processes if "C" in process["type"] or process["type"] not in {"G"}]
        idle = (
            process_info_known and not compute_processes and utilization is not None and utilization <= 5
            and total is not None and total > 0 and free is not None and free / total >= 0.8
        )
        gpus.append({
            "index": index, "uuid": gpu.findtext("uuid", ""),
            "name": gpu.findtext("product_name", "Unknown GPU"),
            "memory_total_mib": total, "memory_used_mib": number(gpu.findtext("fb_memory_usage/used")),
            "memory_free_mib": free, "utilization_percent": utilization,
            "processes": processes, "process_info_known": process_info_known,
            "idle_candidate": idle,
        })
    return gpus


async def probe_host(alias: str, config: Path | None = None) -> dict[str, Any]:
    if alias not in discover_hosts(config):
        raise SSHError("SSH config에 등록된 별칭을 선택해 주세요.")
    arguments = ssh_arguments(alias) + ["-o", "ClearAllForwardings=yes", "--", alias, GPU_COMMAND]
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise SSHError("SSH 연결 시간이 초과되었습니다.") from error
    if process.returncode == 255:
        raise SSHError("SSH 연결 실패: " + stderr.decode(errors="replace").strip()[-800:])
    if process.returncode:
        return {
            "host": alias, "reachable": True, "gpu_supported": False, "gpus": [],
            "message": "SSH는 연결됐지만 nvidia-smi를 실행할 수 없습니다. NVIDIA GPU와 드라이버를 확인해 주세요.",
        }
    if len(stdout) > 2 * 1024 * 1024:
        raise SSHError("GPU 상태 응답이 너무 큽니다.")
    try:
        gpus = parse_gpu_xml(stdout.decode())
    except (ValueError, ET.ParseError, UnicodeError) as error:
        raise SSHError("nvidia-smi의 GPU 상태를 해석하지 못했습니다.") from error
    return {
        "host": alias, "reachable": True, "gpu_supported": True, "gpus": gpus,
        "message": "현재 상태입니다. 여유 표시는 GPU 예약이나 작업 실행 가능 보장이 아닙니다.",
    }


@dataclass
class Tunnel:
    process: asyncio.subprocess.Process
    port: int
    host: str
    remote_port: int


class SSHTunnels:
    """Own independent loopback tunnels; never stop a user's shared SSH master."""

    def __init__(self):
        self.tunnels: dict[str, Tunnel] = {}
        self.lock = asyncio.Lock()

    async def endpoint(self, device_id: str, host: str, remote_port: int) -> str:
        async with self.lock:
            current = self.tunnels.get(device_id)
            if current and current.process.returncode is None and (current.host, current.remote_port) == (host, remote_port):
                return f"http://127.0.0.1:{current.port}/v1"
            if current:
                await self.close(device_id)
            if host not in discover_hosts():
                raise SSHError("등록한 SSH 별칭을 config에서 찾을 수 없습니다.")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            arguments = ssh_arguments(host) + [
                "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3", "-N",
                "-L", f"127.0.0.1:{port}:127.0.0.1:{remote_port}", "--", host,
            ]
            process = await asyncio.create_subprocess_exec(
                *arguments, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            self.tunnels[device_id] = Tunnel(process, port, host, remote_port)
            try:
                for _ in range(100):
                    if process.returncode is not None:
                        detail = (await process.stderr.read()).decode(errors="replace")[-800:]
                        raise SSHError("SSH 터널을 열지 못했습니다: " + detail.strip())
                    try:
                        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=0.15)
                        writer.close()
                        await writer.wait_closed()
                        return f"http://127.0.0.1:{port}/v1"
                    except (OSError, TimeoutError):
                        await asyncio.sleep(0.1)
                raise SSHError("SSH 터널 연결 시간이 초과되었습니다.")
            except BaseException:
                await self.close(device_id)
                raise

    async def close(self, device_id: str) -> None:
        tunnel = self.tunnels.pop(device_id, None)
        if not tunnel:
            return
        if tunnel.process.returncode is None:
            tunnel.process.terminate()
        try:
            await asyncio.wait_for(tunnel.process.communicate(), timeout=3)
        except TimeoutError:
            if tunnel.process.returncode is None:
                tunnel.process.kill()
            await tunnel.process.communicate()

    async def close_all(self) -> None:
        for device_id in list(self.tunnels):
            await self.close(device_id)
