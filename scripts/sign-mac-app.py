"""Keep a certificate-backed identity for local builds; never change TCC grants."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persistent_signing import identity as persistent_identity, metadata


def signing_identity(requested: str) -> str:
    if requested != 'auto':
        if not requested.strip():
            raise ValueError('COSMOSS_CODESIGN_IDENTITY를 비워둘 수 없습니다. auto 또는 인증서를 지정하세요.')
        return requested
    pinned = metadata()
    if pinned is not None:
        return pinned['certificate_sha1']
    raise ValueError('고정 서명 인증서가 없습니다. 저장소의 macos/signing 파일을 복원해 주세요. 임시 서명으로 대체하지 않습니다.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--identity', default=os.getenv('COSMOSS_CODESIGN_IDENTITY', 'auto'))
    parser.add_argument('--embedded', action='store_true', help='Sign bundled Python/uv Mach-O files before sealing the app')
    args = parser.parse_args()
    app = args.app.resolve()
    if not (app / 'Contents/Info.plist').is_file():
        parser.error('완전한 .app 경로를 지정하세요.')
    identity = signing_identity(args.identity)
    with ExitStack() as stack:
        pinned = metadata()
        extra = []
        if pinned is not None and identity == pinned['certificate_sha1']:
            fingerprint, keychain = stack.enter_context(persistent_identity())
            assert fingerprint == identity
            extra = ['--keychain',str(keychain),'--timestamp=none']
        sign(app, identity, args.embedded, extra)
        if pinned is not None and identity == pinned['certificate_sha1']:
            requirement = subprocess.run(['codesign','-d','-r-',str(app)],capture_output=True,text=True,check=True)
            value = requirement.stdout + requirement.stderr
            if 'cdhash' in value or pinned['certificate_sha1'].lower() not in value.lower() or 'certificate' not in value:
                raise ValueError('앱 서명이 고정 인증서의 식별 조건과 다릅니다.')
    print('앱 서명:', '임시 서명' if identity == '-' else identity)


def sign(app, identity, embedded, extra):
    if embedded:
        magic = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
        for path in sorted((app / 'Contents/Resources').rglob('*')):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as content:
                header = content.read(4)
            if header in magic:
                subprocess.run(['codesign', '--force', '--sign', identity, *extra, str(path)], check=True)
    framework = app / 'Contents/Frameworks/Sparkle.framework'
    if framework.exists():
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(framework)], check=True)
    # Let codesign derive a signer-bound requirement. Never substitute a weak
    # identifier-only requirement to try to retain permissions with ad-hoc code.
    subprocess.run(['codesign', '--force', '--sign', identity, *extra, str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)


if __name__ == '__main__':
    main()
