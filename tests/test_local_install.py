"""Local installation replaces one app and restores it if final verification fails."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('local_install', Path(__file__).resolve().parents[1] / 'scripts/install-local-app.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class LocalInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cosmoss-install-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.app'
        self.target = self.root / 'cosMOSS.app'
        self.source.mkdir()
        self.target.mkdir()
        (self.source / 'payload').write_text('new')
        (self.target / 'payload').write_text('old')

    @staticmethod
    def copy(args, **kwargs):
        shutil.copytree(args[-2], args[-1])

    def test_success_keeps_only_one_installed_app(self):
        with patch.object(installer, 'verify'), patch.object(installer, 'check_not_running'), patch.object(installer.subprocess, 'run', side_effect=self.copy):
            installer.replace_app(self.source, self.target)
        self.assertEqual((self.target / 'payload').read_text(), 'new')
        self.assertEqual(set(self.root.iterdir()), {self.source, self.target})

    def test_failed_final_verification_restores_previous_app(self):
        def verify(path):
            if path == self.target:
                raise RuntimeError('simulated failed verification')
        with patch.object(installer, 'verify', side_effect=verify), patch.object(installer, 'check_not_running'), patch.object(installer.subprocess, 'run', side_effect=self.copy):
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                installer.replace_app(self.source, self.target)
        self.assertEqual((self.target / 'payload').read_text(), 'old')
        self.assertEqual(set(self.root.iterdir()), {self.source, self.target})

    def test_invalid_incoming_app_never_replaces_current_app(self):
        with patch.object(installer, 'verify', side_effect=RuntimeError('invalid signature')), patch.object(installer, 'check_not_running'):
            with self.assertRaisesRegex(RuntimeError, 'signature'):
                installer.replace_app(self.source, self.target)
        self.assertEqual((self.target / 'payload').read_text(), 'old')

    def test_running_app_is_not_replaced_or_terminated(self):
        command = str(self.target / 'Contents/MacOS/CosMOSS')
        with patch.object(installer.subprocess, 'check_output', return_value=command + '\n'), patch.object(installer, 'verify') as verify:
            with self.assertRaisesRegex(RuntimeError, '실행 중'):
                installer.replace_app(self.source, self.target)
            verify.assert_not_called()
        self.assertEqual((self.target / 'payload').read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
