"""Remove private storage and isolated tools belonging to retired features."""
from pathlib import Path
import logging
import os
import re
import shutil
import subprocess
import sys


def remove_retired_voice_data(data: Path, runtime: Path) -> None:
    # Never follow a redirected root or remove shared transcription/model caches.
    targets = ((data, 'voice-profiles'), (runtime, '.cache/voice-engine'))
    try:
        commands = subprocess.check_output(['ps', '-ax', '-o', 'command='], text=True)
    except (OSError, subprocess.SubprocessError):
        return
    for root, relative in targets:
        target = root / relative
        if root.is_symlink() or target.is_symlink() or target.parent.is_symlink():
            continue
        if not target.exists() or str(target.resolve()) in commands:
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
        except OSError:
            logging.getLogger(__name__).warning('Retired feature storage could not be removed: %s', target)


def remove_retired_desktop_runtime(runtime: Path) -> None:
    resources = os.getenv('COSMOSS_BUNDLED_RESOURCES')
    if not resources or Path(sys.executable).resolve() != (Path(resources) / 'python/bin/python3.12').resolve():
        return
    target = runtime / 'desktop-runtimes'
    try:
        if target.resolve() != target or not target.is_dir():
            return
        # The old installer used hashed slots, a lock file and scratch downloads.
        if any(not re.fullmatch(r'[0-9a-f]{24}|\.install-lock|\.download\.[A-Za-z0-9]+|\.DS_Store', item.name)
               for item in target.iterdir()):
            return
        commands = subprocess.check_output(['ps', '-ax', '-o', 'command='], text=True, timeout=3)
        if str(target) in commands or any(
                str(runtime) in line and ('install-desktop-runtime.sh' in line or 'install-optional-tools.sh' in line)
                for line in commands.splitlines()):
            return
        shutil.rmtree(target)
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, subprocess.SubprocessError):
        logging.getLogger(__name__).warning('Retired desktop runtime cleanup deferred: %s', target)
