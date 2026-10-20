"""Verify identity continuity across changed local builds without changing TCC.

python tests/signing_smoke.py /path/to/cosMOSS.app
Uses an existing signing certificate. Temporary copies are never launched.
"""
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIGNER = ROOT / 'scripts/sign-mac-app.py'


def requirement(app):
    result = subprocess.run(['codesign', '-d', '-r-', str(app)], capture_output=True, text=True, check=True)
    match = re.search(r'(?:# )?designated => (.+)', result.stdout + result.stderr)
    assert match and 'cdhash' not in match[1] and 'certificate' in match[1], result.stdout + result.stderr
    return match[1]


def main():
    spec = importlib.util.spec_from_file_location('cosmoss_signing', SIGNER)
    signer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signer)
    identity = signer.signing_identity('auto')
    assert identity != '-', 'Install or explicitly select a signing certificate first'
    with tempfile.TemporaryDirectory(prefix='cosmoss-signing-') as temporary:
        root = Path(temporary)
        first = root / 'first/cosMOSS.app'
        subprocess.run(['ditto', str(Path(sys.argv[1]).resolve()), str(first)], check=True)
        subprocess.run([sys.executable, str(SIGNER), str(first), '--identity', identity], check=True)
        original = requirement(first)
        second = root / 'second/cosMOSS.app'
        subprocess.run(['ditto', str(first), str(second)], check=True)
        (second / 'Contents/Resources/signing-check.txt').write_text('A changed build in another location')
        subprocess.run([sys.executable, str(SIGNER), str(second), '--identity', identity], check=True)
        updated = requirement(second)
        previous_requirement = root / 'previous.req'
        updated_requirement = root / 'updated.req'
        previous_requirement.write_text(original)
        updated_requirement.write_text(updated)
        subprocess.run(['codesign', '--verify', '--strict', '-R', str(previous_requirement), str(second)], check=True)
        subprocess.run(['codesign', '--verify', '--strict', '-R', str(updated_requirement), str(first)], check=True)
        assert original == updated
    print('PASS: changed, relocated builds retain a mutually compatible certificate-backed requirement; no permissions granted/reset')


if __name__ == '__main__':
    main()
