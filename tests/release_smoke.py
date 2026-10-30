"""Validate an extracted, relocated release without the checkout on Python's path.

python tests/release_smoke.py /absolute/path/to/extracted/cosMOSS.app
Models and AI services are not invoked. FFmpeg is a documented prerequisite.
"""
import json
import os
from pathlib import Path
import plistlib
import tomllib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error


def main():
    app = Path(sys.argv[1]).resolve()
    resources = app / 'Contents/Resources'
    metadata = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    assert metadata['CFBundleShortVersionString'] == version
    assert metadata['SUPublicEDKey'] == (root / 'macos/UpdatePublicKey.txt').read_text().strip()
    assert metadata['SUFeedURL'] == 'https://raw.githubusercontent.com/hoonably/cosMOSS/main/appcast.xml'
    assert metadata['SURequireSignedFeed'] and metadata['SUVerifyUpdateBeforeExtraction']
    assert metadata['SUEnableAutomaticChecks']
    assert not metadata['SUAutomaticallyUpdate']
    assert metadata['NSMicrophoneUsageDescription']
    assert metadata['UTExportedTypeDeclarations'][0]['UTTypeTagSpecification']['public.filename-extension'] == ['mossvoice']
    assert (resources / 'app/static/voices.js').is_file()
    assert (resources / 'app/src/voice_embedding_worker.py').is_file()
    assert metadata['CFBundleIconFile'] == 'AppIcon.icns'
    assert (resources / 'AppIcon.icns').read_bytes().startswith(b'icns')
    assert (app / 'Contents/Frameworks/Sparkle.framework').is_dir()
    assert 'CosMOSSProjectRoot' not in metadata
    assert (resources / 'python/licenses/LICENSE.cpython.txt').is_file()
    assert (resources / 'tools/uv-0.9.25.dist-info/licenses/LICENSE-MIT').is_file()
    for path in app.rglob('*'):
        if path.is_symlink():
            assert path.resolve().is_relative_to(app), path
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    with tempfile.TemporaryDirectory(prefix='cosmoss-release-') as temporary:
        root = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        environment = dict(os.environ)
        environment.pop('PYTHONHOME', None)
        environment.update(PYTHONPATH=str(resources / 'app'), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                           MOSS_DATA_DIR=str(root / 'data'), MOSS_RUNTIME_DIR=str(root),
                           MLX_VENV=str(root / 'mlx'), UV_BIN=str(resources / 'tools/bin/uv'))
        base = f'http://127.0.0.1:{port}'
        # Simulate an existing user's edited default before this release starts.
        (root / 'data').mkdir()
        (root / 'data/summary-preferences.json').write_text(json.dumps({'default_prompt': 'old saved default'}))

        def request(path, body=None, method='GET'):
            headers = {'Origin': base}
            if body is not None:
                headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                raise AssertionError(f'{method} {path}: {error.code} {error.read().decode()}') from error

        with (root / 'backend.log').open('wb') as log:
            process = subprocess.Popen([str(resources / 'python/bin/python3.12'), '-m', 'uvicorn', 'src.main:app',
                                        '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=root, env=environment, stdout=log, stderr=log)
            try:
                for _ in range(80):
                    if process.poll() is not None:
                        raise AssertionError((root / 'backend.log').read_text())
                    try:
                        assert request('/healthz')['app'] == 'cosMOSS'
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError('Embedded backend failed to start')
                assert request('/api/notes') == []
                preferences = request('/api/summary/default-prompt')
                assert preferences['default_prompt'] == preferences['recommended_prompt']
                assert preferences['default_prompt'] != 'old saved default'
                assert not preferences['customized']
                custom = 'edited after updating'
                assert request('/api/summary/default-prompt', {'prompt':custom}, 'PUT')['default_prompt'] == custom
                assert request('/api/summary/default-prompt')['default_prompt'] == custom
                devices = request('/api/devices')
                assert len(devices) == 1 and devices[0]['id'] == 'local'
                runtime = request('/api/devices/local/environment')
                assert runtime['uv']['ready'] and runtime['uv']['path'] == environment['UV_BIN']
                assert Path(runtime['venv']['path']).resolve() == Path(environment['MLX_VENV']).resolve()
                folder = root / 'transcripts'
                folder.mkdir()
                source = 'WEBVTT\n\n00:01.000 --> 00:02.000\n<v 지민>첫 기록\n'
                file = folder / 'test.vtt'
                file.write_text(source)
                documents = request('/api/text-library?' + urllib.parse.urlencode({'directory': str(folder)}))
                assert len(documents) == 1
                note_path = '/api/notes/' + documents[0]['id']
                note = request(note_path)
                edited = source.replace('첫 기록', '수정한 기록')
                request(note_path, {'text_body': edited, 'text_hash': note['text_hash']}, 'PATCH')
                assert file.read_text() == edited
                assert (root / 'data').is_dir()
                assert not (resources / 'app/data').exists()
                assert not (resources / 'app/logs').exists()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        subprocess.run([str(resources / 'python/bin/python3.12'), '-c',
                        "from pathlib import Path; from src.summaries import Summaries; "
                        "assert Summaries(Path('data/summaries')).preferences()['default_prompt'] == 'edited after updating'"],
                       cwd=root, env=environment, check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print('PASS: relocated release, signature, embedded Python/uv, isolated data, one-time prompt reset and later edits, VTT reading/editing, unchanged bundle')


if __name__ == '__main__':
    main()
