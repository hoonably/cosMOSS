"""Native close/reopen behavior with injected work states; never quits the real app."""
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix='cosmoss-window-lifecycle-') as temporary:
    executable = Path(temporary)/'WindowLifecycleChecks'
    subprocess.run(['swiftc', '-parse-as-library', '-target', 'arm64-apple-macosx13.0',
                    str(ROOT/'macos/Sources/CosMOSS/WindowLifecycle.swift'),
                    str(ROOT/'tests/WindowLifecycleChecks.swift'), '-o', str(executable)], check=True)
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=15)
    assert 'PASS:' in result.stdout, result.stdout + result.stderr
    print(result.stdout.strip())
