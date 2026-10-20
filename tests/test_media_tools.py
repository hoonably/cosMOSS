"""Common setup uses tiny local tool archives; never downloads models or wheels."""

import asyncio
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import httpx

from src import media_tools


def tool_archive(unsafe=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:xz') as package:
        for name in media_tools.TOOLS:
            body = f"#!/bin/sh\nprintf '%s\\n' '{name} version 9.0.1'\n".encode()
            member = tarfile.TarInfo(f'{media_tools.ARCHIVE_ROOT}/bin/{name}')
            member.mode = 0o755
            member.size = len(body)
            package.addfile(member, io.BytesIO(body))
        for name in ('lib/companion.dylib', 'LICENSES/ffmpeg/COPYING.LGPLv2.1', 'BUILDINFO.txt'):
            member = tarfile.TarInfo(f'{media_tools.ARCHIVE_ROOT}/{name}')
            member.size = 7
            package.addfile(member, io.BytesIO(b'fixture'))
        if unsafe:
            member = tarfile.TarInfo(unsafe)
            member.size = 4
            package.addfile(member, io.BytesIO(b'evil'))
    return output.getvalue()


class MediaToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = media_tools.MediaTools(self.root / 'tools')
        self.addAsyncCleanup(self.manager.close)
        self.payload = tool_archive()
        self.which = self.enterContext(patch('src.media_tools.shutil.which', return_value=None))
        self.enterContext(patch('src.media_tools.platform.system', return_value='Darwin'))
        self.enterContext(patch('src.media_tools.platform.machine', return_value='arm64'))
        self.enterContext(patch('src.media_tools.ARCHIVE_BYTES', len(self.payload)))
        self.enterContext(patch('src.media_tools.ARCHIVE_SHA256', hashlib.sha256(self.payload).hexdigest()))
        self.fetch = self.enterContext(patch('src.media_tools.urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(self.payload)))

    async def test_status_is_read_only_and_setup_deduplicates_concurrent_requests(self):
        self.assertFalse(self.manager.status()['ready'])
        self.assertFalse(self.manager.root.exists())
        self.fetch.assert_not_called()
        await asyncio.gather(self.manager.ensure_ready(), self.manager.ensure_ready())
        self.assertTrue(self.manager.status()['ready'])
        self.fetch.assert_called_once()
        self.assertFalse(self.manager.status()['busy'])
        self.assertTrue((self.manager.managed_folder() / 'LICENSES/ffmpeg/COPYING.LGPLv2.1').is_file())
        self.assertTrue((self.manager.managed_folder() / 'lib/companion.dylib').is_file())
        self.assertFalse(any(self.manager.root.glob('.download-*')))

    async def test_reopening_reuses_download_offline_and_resolves_absolute_paths(self):
        await self.manager.ensure_ready()
        reopened = media_tools.MediaTools(self.manager.root)
        self.addAsyncCleanup(reopened.close)
        self.fetch.side_effect = AssertionError('Cached installation must work offline')
        await reopened.ensure_ready()
        for name in media_tools.TOOLS:
            path = Path(reopened.executable(name))
            self.assertTrue(path.is_absolute())
            self.assertEqual(path, self.manager.managed_folder() / 'bin' / name)
            self.assertEqual(reopened.status()['components'][name]['source'], 'app')

    async def test_existing_system_tools_skip_download_and_mixed_installations_reuse_existing_tool(self):
        system = self.root / 'system'
        system.mkdir()
        for name in media_tools.TOOLS:
            path = system / name
            path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name} version 8.0'\n")
            path.chmod(0o755)
        self.which.side_effect = lambda name: str(system / name)
        await self.manager.ensure_ready()
        self.fetch.assert_not_called()
        self.assertFalse(self.manager.root.exists())
        self.which.side_effect = lambda name: str(system / name) if name == 'ffmpeg' else None
        await self.manager.ensure_ready()
        self.assertEqual(self.manager.executable('ffmpeg'), str(system / 'ffmpeg'))
        self.assertEqual(self.manager.status()['components']['ffprobe']['source'], 'app')
        self.fetch.assert_called_once()

    async def test_failed_repair_keeps_old_files_and_retry_recovers_deleted_tool(self):
        await self.manager.ensure_ready()
        old = self.manager.managed_folder()
        old_pointer = (self.manager.root / 'current.json').read_bytes()
        (old / 'bin/ffprobe').unlink()
        self.assertFalse(self.manager.status()['ready'])
        self.fetch.side_effect = lambda *a, **k: io.BytesIO(self.payload[:-1])
        with self.assertRaisesRegex(RuntimeError, '검증에 실패'):
            await self.manager.ensure_ready()
        self.assertEqual((self.manager.root / 'current.json').read_bytes(), old_pointer)
        self.assertTrue((old / 'bin/ffmpeg').is_file())
        self.assertEqual(len(list((self.manager.root / 'installs').iterdir())), 1)
        self.fetch.side_effect = lambda *a, **k: io.BytesIO(self.payload)
        await self.manager.ensure_ready()
        self.assertTrue(self.manager.status()['ready'])
        self.assertNotEqual(self.manager.managed_folder(), old)
        self.assertTrue((old / 'bin/ffmpeg').is_file())

    async def test_checksum_mismatch_does_not_extract_or_publish(self):
        self.fetch.side_effect = lambda *a, **k: io.BytesIO(bytes(len(self.payload)))
        with self.assertRaisesRegex(RuntimeError, '검증에 실패'):
            await self.manager.ensure_ready()
        self.assertFalse((self.manager.root / 'current.json').exists())
        self.assertEqual(list((self.manager.root / 'installs').iterdir()), [])

    async def test_unsafe_archive_and_unusable_tool_are_not_published(self):
        unsafe = tool_archive(media_tools.ARCHIVE_ROOT + '/../../escape')
        with patch('src.media_tools.ARCHIVE_BYTES', len(unsafe)), \
             patch('src.media_tools.ARCHIVE_SHA256', hashlib.sha256(unsafe).hexdigest()):
            self.fetch.side_effect = lambda *a, **k: io.BytesIO(unsafe)
            with self.assertRaisesRegex(RuntimeError, '올바르지 않은 경로'):
                await self.manager.ensure_ready()
        self.assertFalse((self.root / 'escape').exists())
        self.fetch.side_effect = lambda *a, **k: io.BytesIO(self.payload)
        with patch('src.media_tools.probe_tool', return_value={'ready':False, 'path':'', 'version':''}):
            with self.assertRaisesRegex(RuntimeError, '실행을 확인하지 못했습니다'):
                await self.manager.ensure_ready()
        self.assertFalse((self.manager.root / 'current.json').exists())

    async def test_network_failure_can_be_retried_and_escaping_pointer_is_ignored(self):
        self.fetch.side_effect = urllib.error.URLError('offline')
        with self.assertRaises(RuntimeError):
            await self.manager.ensure_ready()
        self.fetch.side_effect = lambda *a, **k: io.BytesIO(self.payload)
        (self.manager.root / 'current.json').write_text(json.dumps({'folder':'../outside', 'archive_sha256':media_tools.ARCHIVE_SHA256}))
        self.assertIsNone(self.manager.managed_folder())
        await self.manager.ensure_ready()
        self.assertTrue(self.manager.status()['ready'])

    async def test_cancelled_caller_does_not_cancel_another_callers_setup(self):
        started, release = threading.Event(), threading.Event()
        def install():
            started.set()
            if not release.wait(5):
                raise RuntimeError('test setup timed out')
        with patch.object(self.manager, 'install', side_effect=install):
            caller = asyncio.create_task(self.manager.ensure_ready())
            await asyncio.to_thread(started.wait, 3)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(self.manager.task.cancelled())
            release.set()
            await self.manager.task
            self.assertEqual(self.manager.state['phase'], 'ready')

    async def test_common_setup_api_never_prepares_a_mac_model_and_rejects_cross_site_requests(self):
        from src import main
        with patch.object(main, 'MEDIA_TOOLS', self.manager), \
             patch.object(main.LOCAL_MODELS, 'start') as model, \
             patch.object(main.LOCAL_MODELS, 'start_environment') as mlx, \
             patch.object(main.REMOTE_WORKERS, 'start') as remote:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost') as client:
                checked = await client.get('/api/setup')
                self.assertEqual(checked.status_code, 200)
                self.fetch.assert_not_called()
                response = await client.post('/api/setup')
                self.assertEqual(response.status_code, 202, response.text)
                await self.manager.task
                self.assertTrue((await client.get('/api/setup')).json()['ready'])
                denied = await client.post('/api/setup', headers={'Sec-Fetch-Site':'cross-site'})
                self.assertEqual(denied.status_code, 403)
            for address, host in (('10.0.0.2','localhost'), ('127.0.0.1','attacker.example')):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app,client=(address,12345)),base_url='http://'+host) as outsider:
                    self.assertEqual((await outsider.post('/api/setup')).status_code,403)
            model.assert_not_called()
            mlx.assert_not_called()
            remote.assert_not_called()
            self.assertFalse((self.root / '.venv-mlx').exists())

    async def test_audio_commands_use_the_app_installation_without_global_path_changes(self):
        from src import main
        await self.manager.ensure_ready()
        with patch.object(main, 'MEDIA_TOOLS', self.manager), patch.object(main.subprocess, 'run') as run:
            main.normalize_audio(self.root / 'source.mp3', self.root / 'normalized.wav')
        self.assertEqual(run.call_args.args[0][0], str(self.manager.managed_folder() / 'bin/ffmpeg'))

    async def test_upload_cannot_start_a_download_without_protected_setup(self):
        from src import main
        with patch.object(main, 'MEDIA_TOOLS', self.manager):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                response = await client.post('/api/uploads',files={'file':('voice.wav',b'audio','audio/wav')})
                self.assertEqual(response.status_code,503,response.text)
                self.assertIn('상단 설정',response.json()['detail'])
        self.fetch.assert_not_called()
        self.assertIsNone(self.manager.task)
