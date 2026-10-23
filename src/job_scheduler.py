"""Run independent transcription resources while preserving per-resource order."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
import json
import logging
from typing import Any

from src.speech_pipeline import FAST_PIPELINES


_LOGGER = logging.getLogger(__name__)
_EXCLUSIVE = frozenset({'all-resources'})


def transcription_resources(note: dict[str, Any]) -> frozenset[str]:
    """Use the immutable job destination, never the currently selected device.

    GPU UUIDs also identify the same physical GPU through different SSH aliases.
    Legacy CUDA workers share device-level files and HTTP ports; remote MLX
    targets can share a model process and unified memory. Keep those legacy
    groups serial until they have the finite-job isolation of current presets.
    Unknown/old destinations run exclusively instead of guessing their device.
    """
    try:
        device = json.loads(note.get('device_snapshot_json') or '{}')
    except (TypeError, ValueError):
        return _EXCLUSIVE
    if not isinstance(device, dict):
        return _EXCLUSIVE
    if device.get('backend') == 'cuda':
        gpu = device.get('gpu_uuid')
        if not isinstance(gpu, str) or not gpu.startswith('GPU-'):
            return _EXCLUSIVE
        resources = {f'gpu:{gpu.lower()}'}
        options = device.get('options')
        pipeline = options.get('speech_pipeline') if isinstance(options, dict) else None
        if pipeline not in FAST_PIPELINES:
            resources.add('legacy-cuda-workers')
        return frozenset(resources)
    if device.get('backend') == 'mlx':
        if device.get('kind') == 'local' or device.get('id') == 'local':
            return frozenset({'local-mac'})
        if device.get('kind') == 'ssh':
            return frozenset({'remote-mlx-workers'})
    return _EXCLUSIVE


def _conflicts(left: frozenset[str], right: frozenset[str]) -> bool:
    return bool(left & right or left & _EXCLUSIVE or right & _EXCLUSIVE)


class _ResourceSlots:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.waiting: list[tuple[object, frozenset[str]]] = []
        self.active: dict[object, frozenset[str]] = {}

    @asynccontextmanager
    async def claim(self, resources: frozenset[str]):
        token = object()
        async with self.condition:
            self.waiting.append((token, resources))

            def available():
                if any(_conflicts(resources, used) for used in self.active.values()):
                    return False
                for earlier, wanted in self.waiting:
                    if earlier is token:
                        return True
                    if _conflicts(resources, wanted):
                        return False
                return False

            try:
                await self.condition.wait_for(available)
                self.active[token] = resources
            finally:
                self.waiting = [(other, wanted) for other, wanted in self.waiting if other is not token]
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.active.pop(token, None)
                self.condition.notify_all()


async def run_transcription_queue(
    queue: asyncio.Queue[str],
    *,
    get_note: Callable[[str], dict[str, Any]],
    transcribe: Callable[[str], Awaitable[None]],
    tasks: MutableMapping[str, asyncio.Task],
    cancel_requested: set[str],
    parallel_resources: bool = True,
    on_error: Callable[[str, Exception], None] | None = None,
) -> None:
    """Dispatch queued IDs; cancellation continues to use the shared task map.

    Waiting jobs remain ``queued`` in the store. Only ``transcribe`` changes
    their status when execution really starts. Cancelling this dispatcher
    waits for every owned job's cleanup, including resource release. Store
    recovery can therefore requeue interrupted jobs on the next app launch.

    ``on_error`` is for unexpected loader/runner failures: normal inference
    errors should still be recorded by ``transcribe``. It must be synchronous.
    Each consumed queue entry is completed exactly once, including duplicates,
    deleted notes, queued cancellation, and shutdown.
    """
    slots = _ResourceSlots()
    owned: set[asyncio.Task] = set()

    def failed(note_id: str, error: Exception):
        if on_error is not None:
            try:
                on_error(note_id, error)
                return
            except Exception:
                _LOGGER.exception('Could not record transcription failure for %s', note_id)
        _LOGGER.error('Transcription job %s failed: %s', note_id, error,
                      exc_info=(type(error), error, error.__traceback__))

    async def run(note_id: str, resources: frozenset[str]):
        try:
            async with slots.claim(resources):
                try:
                    note = get_note(note_id)
                except KeyError:
                    return
                if note.get('status') == 'queued':
                    await transcribe(note_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failed(note_id, error)

    def finished(note_id: str, task: asyncio.Task):
        # A queued task may be cancelled before its coroutine ever starts, so
        # this cannot live in run()'s finally block.
        if tasks.get(note_id) is task:
            tasks.pop(note_id, None)
            cancel_requested.discard(note_id)
        owned.discard(task)
        queue.task_done()

    try:
        while True:
            note_id = await queue.get()
            try:
                previous = tasks.get(note_id)
                if previous is not None and not previous.done():
                    queue.task_done()
                    continue
                try:
                    note = get_note(note_id)
                except KeyError:
                    queue.task_done()
                    continue
                if note.get('status') != 'queued':
                    queue.task_done()
                    continue
                resources = transcription_resources(note) if parallel_resources else _EXCLUSIVE
                task = asyncio.create_task(run(note_id, resources), name=f'transcription:{note_id}')
                tasks[note_id] = task
                owned.add(task)
                task.add_done_callback(lambda completed, note_id=note_id: finished(note_id, completed))
            except Exception as error:
                queue.task_done()
                failed(note_id, error)
    finally:
        # A second cancel during remote lease cleanup could interrupt its
        # shutdown. Let already-cancelling jobs finish their first cancellation.
        for task in list(owned):
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
