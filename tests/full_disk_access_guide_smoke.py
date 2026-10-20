"""Render/test the native guide without opening Settings or changing permissions.

python tests/full_disk_access_guide_smoke.py [APP_PATH] [PNG_PATH]
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    app = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'dist/cosMOSS.app').resolve()
    assert (app / 'Contents/Info.plist').is_file(), app
    with tempfile.TemporaryDirectory(prefix='cosmoss-guide-qa-') as temporary:
        executable = Path(temporary) / 'GuideChecks'
        subprocess.run(['swiftc', '-parse-as-library', '-target', 'arm64-apple-macosx13.0',
                        str(ROOT / 'macos/Sources/CosMOSS/FullDiskAccessAssistant.swift'),
                        str(ROOT / 'tests/FullDiskAccessGuideChecks.swift'), '-o', str(executable)], check=True)
        subprocess.run([str(executable), str(app), *sys.argv[2:]], check=True)


if __name__ == '__main__':
    main()
