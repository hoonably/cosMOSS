"""Remove private storage and isolated tools belonging to retired features."""
from pathlib import Path
import logging
import shutil
import subprocess


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
