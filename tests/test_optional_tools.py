"""Optional uv installation must report progress and preserve retry state."""
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from src import runtime_environment as runtime


class OptionalToolsTests(unittest.TestCase):
    def fixture(self, root, body):
        resources=root/'resources';resources.mkdir()
        (resources/'install-optional-tools.sh').write_text(body)
        return resources

    def test_local_download_progress_and_private_destination(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            resources=self.fixture(root,"printf 'COSMOSS_RUNTIME_PROGRESS:4:10:downloading\\n' >&2\n")
            updates=[]
            environment={'COSMOSS_BUNDLED_RESOURCES':str(resources),'MOSS_RUNTIME_DIR':str(root)}
            with patch.dict(os.environ,environment), patch.object(runtime.os,'uname',return_value=types.SimpleNamespace(sysname='Darwin',machine='arm64')), \
                 patch.object(runtime,'find_uv',side_effect=[{'ready':False},{'ready':True,'path':str(root/'runtime-tools/uv')}]), \
                 patch.object(runtime.urllib.request,'urlopen') as upstream:
                result=runtime.ensure_uv(root/'other-tools',progress=updates.append)
            self.assertEqual(result,str(root/'runtime-tools/uv'))
            self.assertEqual(updates,[{'downloaded_bytes':4,'total_bytes':10,'percent':40.0,'state':'downloading'}])
            upstream.assert_not_called()

    def test_failed_optional_download_does_not_discard_partial_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);resources=self.fixture(root,"echo 'transfer failed' >&2\nexit 1\n")
            cache=root/'.cache/desktop-downloads';cache.mkdir(parents=True)
            part=cache/'pinned.whl.part';part.write_bytes(b'partial transfer')
            with patch.dict(os.environ,{'COSMOSS_BUNDLED_RESOURCES':str(resources),'MOSS_RUNTIME_DIR':str(root)}), \
                 patch.object(runtime.os,'uname',return_value=types.SimpleNamespace(sysname='Darwin',machine='arm64')), \
                 patch.object(runtime,'find_uv',return_value={'ready':False}):
                with self.assertRaisesRegex(RuntimeError,'완료된 다운로드를 재사용'):
                    runtime.ensure_uv(root/'tools')
            self.assertEqual(part.read_bytes(),b'partial transfer')
