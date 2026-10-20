"""Test/render startup access with injected states, without changing TCC.

python tests/startup_access_smoke.py [APP_PATH] [PNG_PATH]
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    app = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'dist/cosMOSS.app').resolve()
    with tempfile.TemporaryDirectory(prefix='cosmoss-startup-access-') as temporary:
        executable = Path(temporary) / 'StartupAccessChecks'
        subprocess.run(['swiftc', '-parse-as-library', '-target', 'arm64-apple-macosx13.0',
                        str(ROOT / 'macos/Sources/CosMOSS/FullDiskAccessAssistant.swift'),
                        str(ROOT / 'macos/Sources/CosMOSS/FullDiskAccessGate.swift'),
                        str(ROOT / 'tests/StartupAccessChecks.swift'), '-o', str(executable)], check=True)
        subprocess.run([str(executable), str(app), *sys.argv[2:]], check=True)


if __name__ == '__main__':
    main()
