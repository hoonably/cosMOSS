#!/usr/bin/env python3
"""Bundle the pinned minimal desktop backend. Downloads happen on the build host only."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_URL = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260113/cpython-3.12.12%2B20260113-aarch64-apple-darwin-install_only_stripped.tar.gz'
PYTHON_SHA256 = 'b4fbffc79b72a040ffe3ab4f714864cf9e0166d075576096247dc44fa1883630'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--uv', default='uv')
    args = parser.parse_args()
    resources = args.app / 'Contents/Resources'
    python_root = resources / 'python'
    with tempfile.TemporaryDirectory(prefix='cosmoss-runtime-build-') as tmp:
        archive = Path(tmp) / 'python.tar.gz'
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--connect-timeout', '10',
                        '--max-time', '300', '--retry', '2', PYTHON_URL, '--output', str(archive)], check=True)
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == PYTHON_SHA256, 'Python source archive digest mismatch'
        python_root.mkdir()
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(python_root), '--strip-components', '1'], check=True)
    stdlib = python_root / 'lib/python3.12'
    # The desktop has native SwiftUI/WebKit UI; it does not use Python's GUI,
    # package manager or stdlib's test suite. Keep SSL, SQLite, ctypes, venv,
    # multiprocessing, encodings, headers and all runtime standard modules.
    for name in ['tkinter', 'idlelib', 'turtledemo', 'ensurepip', 'test']:
        shutil.rmtree(stdlib / name, ignore_errors=True)
    for pattern in ['libtcl*', 'libtk*', 'tcl*', 'tk*', 'itcl*']:
        for path in (python_root / 'lib').glob(pattern):
            if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
            else: path.unlink()
    for path in (stdlib / 'lib-dynload').glob('_tkinter*'): path.unlink()
    site = stdlib / 'site-packages'
    for pattern in ['pip', 'pip-*.dist-info', 'setuptools', 'setuptools-*.dist-info', '_distutils_hack', 'distutils-precedence.pth']:
        for path in site.glob(pattern):
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    for path in (python_root / 'bin').iterdir():
        if path.name not in ['python', 'python3', 'python3.12', 'python3.12-config']:
            path.unlink()
    shutil.copytree(ROOT / 'resources/python-notices', python_root / 'licenses')
    python = python_root / 'bin/python3.12'
    subprocess.run([args.uv, 'pip', 'install', '--no-config', '--python', str(python), '--target', str(site),
                    '--require-hashes', '--only-binary', ':all:', '--requirements', str(resources / 'requirements.txt')], check=True)
    for path in python_root.rglob('__pycache__'): shutil.rmtree(path, ignore_errors=True)
    subprocess.run([str(python), '-I', '-B', '-c',
                    'import fastapi, uvicorn, httpx, pydantic, multipart, markdown_it, ssl, sqlite3, ctypes, venv; '
                    'import importlib.util as u; assert all(u.find_spec(m) is None for m in '
                    '["uvloop","httptools","websockets","watchfiles","dotenv","yaml","pip","tkinter"])'], check=True)
    inventory = json.loads(subprocess.check_output([str(python), '-I', '-B', '-c',
        'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]: d.version for d in m.distributions()}))'], text=True))
    record = {'python': '3.12.12', 'python_build': '20260113', 'source_sha256': PYTHON_SHA256,
              'first_launch_downloads': [], 'uv_bundled': False, 'core_packages': inventory, 'server': 'uvicorn + asyncio + h11',
              'python_and_core_bytes': sum(p.stat().st_size for p in python_root.rglob('*') if p.is_file() and not p.is_symlink())}
    (resources / 'desktop-runtime.json').write_text(json.dumps(record, indent=2) + '\n')
    print('Bundled offline desktop runtime:', record)


if __name__ == '__main__': main()
