"""Pinned identity guards must fail before importing or signing a different key."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('persistent_signing_test',ROOT/'scripts/persistent_signing.py')
signing=importlib.util.module_from_spec(spec);spec.loader.exec_module(signing)


class PersistentSigningTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='cosmoss-signing-guards-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for relative in ('release.json','macos/UpdatePublicKey.txt','macos/signing/identity.json',
                         'macos/signing/certificate.der','macos/signing/identity.p12.aesgcm','scripts/signing-archive.swift'):
            target=self.root/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/relative,target)

    def test_changed_certificate_or_app_identifier_is_rejected(self):
        self.assertIsNotNone(signing.metadata(self.root))
        cert=self.root/'macos/signing/certificate.der';original=cert.read_bytes()
        cert.write_bytes(original+b'changed')
        with self.assertRaises(ValueError):signing.metadata(self.root)
        cert.write_bytes(original)
        path=self.root/'macos/signing/identity.json';saved=json.loads(path.read_text())
        saved['bundle_identifier']='some.other.app';path.write_text(json.dumps(saved))
        with self.assertRaises(ValueError):signing.metadata(self.root)

    def test_missing_or_wrong_update_key_never_imports_a_key_or_changes_pin(self):
        config=json.loads((self.root/'release.json').read_text())
        before=(self.root/'macos/signing/identity.json').read_bytes()
        wrong=self.root/'wrong-key';wrong.write_text('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
        for path in (self.root/'missing',wrong):
            with self.subTest(path=path.name),patch.dict(os.environ,{config['key_env']:str(path)}),patch.object(signing,'run',wraps=signing.run) as run:
                with self.assertRaises(RuntimeError):
                    with signing.identity(self.root):pass
                self.assertFalse(any(str(call.args[0][0])=='security' for call in run.call_args_list))
                self.assertEqual((self.root/'macos/signing/identity.json').read_bytes(),before)

    def test_corrupted_encrypted_archive_never_reaches_keychain(self):
        path=self.root/'macos/signing/identity.p12.aesgcm';content=bytearray(path.read_bytes());content[-1]^=1;path.write_bytes(content)
        with patch.object(signing,'run',wraps=signing.run) as run:
            with self.assertRaises(RuntimeError):
                with signing.identity(self.root):pass
            self.assertFalse(any(str(call.args[0][0])=='security' for call in run.call_args_list))
