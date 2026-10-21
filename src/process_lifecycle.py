"""Parent ownership for native app backends and managed Linux GPU children."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time


def watch_desktop_parent() -> None:
    """A killed native app cannot leave an orphan backend renewing GPU leases."""
    value = os.environ.get("COSMOSS_DESKTOP_PARENT_PID")
    if not value:
        return  # Terminal/browser launches have their own lifecycle.
    parent = int(value)
    if parent <= 1:
        raise ValueError("Invalid desktop parent PID")

    def watch():
        while os.getppid() == parent:
            time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)
        # Uvicorn gets a chance to cancel jobs and close SSH normally. A stuck
        # shutdown must eventually stop sending heartbeats too.
        time.sleep(15)
        os._exit(1)

    threading.Thread(target=watch, name="cosmoss-desktop-parent", daemon=True).start()


def arm_worker_parent_guard() -> None:
    """The kernel frees the GPU even if its SSH supervisor itself is killed."""
    value = os.environ.get("COSMOSS_WORKER_PARENT_PID")
    if not value:
        return
    parent = int(value)
    if parent <= 1 or sys.platform != "linux":
        raise RuntimeError("Managed GPU workers require a Linux supervisor")
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Could not arm worker parent guard")
    # Close the race where the parent died before prctl was armed.
    if os.getppid() != parent:
        os._exit(1)
