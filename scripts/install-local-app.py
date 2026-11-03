#!/usr/bin/env python3
"""Build a standalone local app, install one copy, and discard all scratch output."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = Path('/Applications/cosMOSS.app')


def verify(app):
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)


def check_not_running(target):
    executable = str(target / 'Contents/MacOS/CosMOSS')
    commands = subprocess.check_output(['ps', '-ax', '-o', 'command='], text=True).splitlines()
    if any(command == executable or command.startswith(executable + ' ') for command in commands):
        raise RuntimeError('cosMOSS가 실행 중입니다. 작업을 저장하고 앱을 정상 종료한 뒤 다시 설치해 주세요.')


def replace_app(source, target):
    """Verify before and after replacement; restore the installed app on failure."""
    if target.is_symlink():
        raise RuntimeError('설치 경로가 심볼릭 링크입니다. 실제 앱 경로를 확인해 주세요.')
    check_not_running(target)
    verify(source)
    # Stage on the destination filesystem so each rename is atomic.
    stage = Path(tempfile.mkdtemp(prefix='.cosmoss-install-', dir=target.parent))
    incoming = stage / 'cosMOSS.app'
    backup = stage / 'previous.app'
    completed = False
    try:
        subprocess.run(['ditto', '--norsrc', '--noextattr', str(source), str(incoming)], check=True)
        verify(incoming)
        check_not_running(target)
        existed = target.exists()
        if existed:
            target.rename(backup)
        try:
            incoming.rename(target)
            verify(target)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if existed:
                try:
                    backup.rename(target)
                except OSError as error:
                    raise RuntimeError(f'기존 앱을 복구하지 못해 다음 위치에 보존했습니다: {backup}') from error
            raise
        completed = True
    finally:
        # Never discard the only recoverable old app if restoring it failed.
        if completed or not backup.exists():
            shutil.rmtree(stage)
    print(f'설치 완료: {target}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open', action='store_true', help='Open the installed app after successful installation')
    args = parser.parse_args()
    check_not_running(APPLICATION)
    # Keep .app copies, Swift products/checkouts and signing scratch out of Git/Codex.
    with tempfile.TemporaryDirectory(prefix='cosmoss-local-build-') as temporary:
        scratch = Path(temporary)
        environment = dict(os.environ)
        environment.update(COSMOSS_SWIFT_SCRATCH_PATH=str(scratch / 'swift'),
                           COSMOSS_RELEASE_BUILD_DIR=str(scratch / 'package'),
                           COSMOSS_PACKAGE_ONLY='1',
                           COSMOSS_RELEASE_CODESIGN_IDENTITY=os.getenv('COSMOSS_LOCAL_CODESIGN_IDENTITY', 'auto'))
        subprocess.run([str(ROOT / 'scripts/build-mac-release.sh')], env=environment, cwd=ROOT, check=True)
        replace_app(scratch / 'package/cosMOSS.app', APPLICATION)
    if args.open:
        subprocess.run(['open', str(APPLICATION)], check=True)


if __name__ == '__main__':
    main()
