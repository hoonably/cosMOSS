"""Keep a certificate-backed identity for local builds; never change TCC grants."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


def signing_identity(requested: str) -> str:
    if requested != 'auto':
        if not requested.strip():
            raise ValueError('COSMOSS_CODESIGN_IDENTITY를 비워둘 수 없습니다. auto 또는 인증서를 지정하세요.')
        return requested
    result = subprocess.run(['security', 'find-identity', '-v', '-p', 'codesigning'],
                            capture_output=True, text=True, check=True)
    identities = re.findall(r'^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"]+)"', result.stdout, re.MULTILINE)
    # Do not silently choose a different certificate on the next rebuild.
    candidates = [(fingerprint, name) for fingerprint, name in identities if name.startswith('Apple Development:')]
    if not candidates:
        candidates = [(fingerprint, name) for fingerprint, name in identities if name.startswith('Developer ID Application:')]
    if len(candidates) > 1:
        raise ValueError('사용 가능한 서명 인증서가 여러 개입니다. COSMOSS_CODESIGN_IDENTITY에 사용할 인증서 이름 또는 SHA-1을 지정하세요.')
    if candidates:
        return candidates[0][0]
    print('주의: 서명 인증서가 없어 임시 서명합니다. 빌드가 바뀌면 macOS 접근 권한을 다시 등록해야 할 수 있습니다.')
    return '-'


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
    if args.embedded:
        magic = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
        for path in sorted((app / 'Contents/Resources').rglob('*')):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as content:
                header = content.read(4)
            if header in magic:
                subprocess.run(['codesign', '--force', '--sign', identity, str(path)], check=True)
    framework = app / 'Contents/Frameworks/Sparkle.framework'
    if framework.exists():
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(framework)], check=True)
    # Let codesign derive a signer-bound requirement. Never substitute a weak
    # identifier-only requirement to try to retain permissions with ad-hoc code.
    subprocess.run(['codesign', '--force', '--sign', identity, str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print('앱 서명:', '임시 서명' if identity == '-' else identity)


if __name__ == '__main__':
    main()
