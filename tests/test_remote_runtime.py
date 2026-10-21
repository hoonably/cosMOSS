"""Reinstall, cross-client reuse and repair without downloading wheels or using GPUs."""

import io
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import remote_bootstrap as bootstrap
from src.runtime_environment import SharedRuntime


class SharedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.version = bootstrap.RUNTIME_VERSION
        self.shared = self.client()

    def probe(self, runtime, stamp, expected, tools):
        interpreter = (runtime / 'bin/python').is_file()
        packages = interpreter and not (runtime / 'broken-packages').exists()
        try:
            packages = packages and stamp.read_text() == expected
        except OSError:
            packages = False
        return {'ready': interpreter and packages, 'uv': {'ready': True},
                'venv': {'ready': interpreter, 'path': str(runtime)},
                'packages': {'ready': packages, 'missing': [] if packages else ['torch']}}

    def client(self, version=None):
        return SharedRuntime(version or self.version, probe=self.probe)

    def install(self, root, descriptor=None):
        (root / '.venv/bin').mkdir(parents=True, exist_ok=True)
        (root / '.venv/bin/python').write_text('fixture interpreter')
        (root / 'runtime-version').write_text(self.version)

    def legacy(self, device_id='a' * 32):
        root = self.shared.base / 'workers' / device_id
        bootstrap.claim_root(root, {'id': device_id, 'api_key': 'old-client', 'model': 'example/moss'})
        self.install(root)
        return root

    def test_different_macs_and_deleted_registration_reuse_legacy_environment_in_place(self):
        legacy = self.legacy()
        owner = (legacy / 'owner.json').read_bytes()
        (legacy / 'worker.log').write_text('another Mac owns this log')
        for device_id in ('b' * 32, 'c' * 32):
            device = {'id': device_id, 'api_key': 'new-client-' + device_id, 'model': 'example/moss', 'remote_port': 18400}
            output = io.StringIO()
            with patch.object(bootstrap.sys, 'stdin', io.StringIO(json.dumps({'device': device, 'action': 'install'}))), \
                 patch.object(bootstrap.sys, 'stdout', output), patch.object(bootstrap, 'inspect_environment', side_effect=self.probe), \
                 patch.object(bootstrap, 'prepare_runtime') as installer, \
                 patch.object(bootstrap, 'prepare_model', return_value={'model_path': '/cache/model', 'reused': True}) as model:
                bootstrap.main()
            installer.assert_not_called()
            self.assertEqual(model.call_args.args[1], legacy / '.venv/bin/python')
            self.assertIn('"runtime_reused": true', output.getvalue())
            self.assertNotIn('"phase": "installing"', output.getvalue())
            job = self.shared.base / 'workers' / device_id
            self.assertFalse((job / '.venv').exists())
            shutil.rmtree(job)  # Remove/re-register with a different local device ID.
        self.assertEqual((legacy / 'owner.json').read_bytes(), owner)
        self.assertEqual((legacy / 'worker.log').read_text(), 'another Mac owns this log')
        self.assertFalse((self.shared.base / 'runtimes').exists())

    def test_fresh_install_is_shared_after_job_and_local_registration_are_removed(self):
        installer = Mock(side_effect=self.install)
        python = self.shared.ensure(lambda *a, **k: None, installer)
        self.assertTrue(python.is_relative_to(self.shared.slot / 'installs'))
        device = {'id': 'b' * 32, 'api_key': 'new-client', 'model': 'example/moss'}
        job = self.shared.base / 'workers' / device['id']
        bootstrap.claim_root(job, device)
        bootstrap.temporary_environment(job, device)
        bootstrap.cleanup_job_files(job, device)
        shutil.rmtree(job)
        self.assertEqual(self.client().ensure(lambda *a, **k: None, installer), python)
        installer.assert_called_once()
        self.assertTrue(self.client().inspect()['ready'])

    def test_read_only_inspection_discovers_legacy_without_creating_any_files(self):
        self.assertFalse(self.shared.inspect()['ready'])
        self.assertEqual(list(self.home.iterdir()), [])
        legacy = self.legacy()
        before = sorted(str(p) for p in self.home.rglob('*'))
        checked = self.client().inspect()
        self.assertTrue(checked['ready'])
        self.assertEqual(checked['venv']['path'], str(legacy / '.venv'))
        self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before)

    def test_existing_two_file_ssh_loader_runs_without_local_project_imports(self):
        legacy = self.legacy()
        source_root = Path(bootstrap.__file__).parent
        helper = (source_root / 'runtime_environment.py').read_text()
        helper += '''
Path.home = classmethod(lambda cls: Path(sys.argv[1]))
def inspect_environment(runtime, stamp, expected, tools, **kwargs):
    ready = (runtime/'bin/python').is_file() and stamp.is_file() and stamp.read_text() == expected
    return {'ready':ready, 'uv':{'ready':True}, 'venv':{'ready':ready,'path':str(runtime)}, 'packages':{'ready':ready}}
'''
        # -I excludes this checkout: the remote host only has the two sources
        # embedded by RemoteWorkers, including already-running older launchers.
        script = 'import sys\n' + helper + '\n' + (source_root / 'remote_bootstrap.py').read_text()
        device = {'id': 'b' * 32, 'api_key': 'new-client', 'model': 'example/moss', 'remote_port': 18400}
        model = "def ensure_model(device, python, emit, install=True):\n    return {'model_path':'/cache/model','reused':True}\n"
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(self.home)],
                                input=json.dumps({'device':device, 'action':'install', 'model_source':model}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"runtime_reused": true', result.stdout)
        self.assertIn(str(legacy / '.venv'), result.stdout)
        self.assertIn('"phase": "installed"', result.stdout)

    def test_installer_child_retains_lock_after_bootstrap_closes_its_descriptor(self):
        child = None
        try:
            with self.shared.installation_lock(lambda *a, **k: None) as descriptor:
                child = subprocess.Popen([sys.executable, '-u', '-c',
                                          "import sys; print('RUNNING', flush=True); sys.stdin.read(1)"],
                                         pass_fds=(descriptor,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline().strip(), 'RUNNING')
            check = '''
import fcntl, sys
with open(sys.argv[1], 'r') as lock:
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: sys.exit(2)
'''
            command = [sys.executable, '-c', check, str(self.shared.slot / 'install.lock')]
            self.assertEqual(subprocess.run(command, timeout=5).returncode, 2)
            child.communicate(input='\n', timeout=5)
            self.assertEqual(subprocess.run(command, timeout=5).returncode, 0)
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_deleted_or_damaged_venv_gets_new_generation_without_changing_old_environment(self):
        for damage in ('delete', 'packages'):
            with self.subTest(damage=damage):
                shared = self.client(version=self.version + '-' + damage)
                def install(root, descriptor):
                    self.install(root)
                    (root / 'runtime-version').write_text(shared.version)
                first = shared.ensure(lambda *a, **k: None, install)
                old = first.parents[2]
                if damage == 'delete':
                    shutil.rmtree(old / '.venv')
                else:
                    (old / '.venv/broken-packages').touch()
                self.assertFalse(shared.inspect()['ready'])
                second = shared.ensure(lambda *a, **k: None, install)
                self.assertNotEqual(first, second)
                self.assertTrue(second.is_file())
                self.assertTrue(old.is_dir())
                self.assertEqual((old / 'runtime-version').read_text(), shared.version)

    def test_deleting_all_remote_app_files_can_be_installed_again(self):
        installer = Mock(side_effect=self.install)
        first = self.shared.ensure(lambda *a, **k: None, installer)
        shutil.rmtree(self.shared.base)
        second = self.client().ensure(lambda *a, **k: None, installer)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertNotEqual(first, second)
        self.assertEqual(installer.call_count, 2)

    def test_failed_install_is_not_published_and_next_request_resumes_it(self):
        attempted = []
        def fail(root, descriptor):
            attempted.append(root)
            self.install(root)
            raise RuntimeError('connection interrupted')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.shared.ensure(lambda *a, **k: None, fail)
        self.assertFalse((self.shared.slot / 'current.json').exists())
        self.assertFalse(self.client().inspect()['ready'])
        installer = Mock(side_effect=self.install)
        python = self.client().ensure(lambda *a, **k: None, installer)
        self.assertEqual(installer.call_args.args[0], attempted[0])
        self.assertEqual(python, attempted[0] / '.venv/bin/python')

    def test_incomplete_packages_are_not_published_even_if_the_installer_returns(self):
        def incomplete(root, descriptor):
            self.install(root)
            (root / '.venv/broken-packages').touch()
        with self.assertRaisesRegex(RuntimeError, '검증에 실패'):
            self.shared.ensure(lambda *a, **k: None, incomplete)
        self.assertFalse((self.shared.slot / 'current.json').exists())
        self.assertFalse(self.client().inspect()['ready'])

    def test_installer_caches_and_managed_python_are_independent_of_job_cleanup(self):
        device = {'id': 'b' * 32, 'api_key': 'new-client', 'model': 'example/moss'}
        job = self.shared.base / 'workers' / device['id']
        bootstrap.claim_root(job, device)
        seen = []
        def install(root, emit, environment, pass_fds=()):
            seen.append(root)
            self.assertNotEqual(root, job)
            self.assertEqual(environment['UV_PYTHON_INSTALL_DIR'], str(root / 'python'))
            self.assertTrue(Path(environment['UV_CACHE_DIR']).is_relative_to(root))
            self.assertEqual(len(pass_fds), 1)
            os.fstat(pass_fds[0])
            bootstrap.cleanup_job_files(job, device)
            self.assertTrue(Path(environment['TMPDIR']).is_dir())
            self.install(root)
            (root / 'python/bin').mkdir(parents=True, exist_ok=True)
            (root / 'python/bin/python3.12').touch()
            return root / '.venv/bin/python'
        with patch.object(bootstrap, 'prepare_runtime', side_effect=install):
            python = bootstrap.prepare_shared_runtime(self.shared, device, lambda *a, **k: None)
        self.assertTrue(python.exists())
        self.assertTrue((seen[0] / 'python/bin/python3.12').exists())
        self.assertFalse((seen[0] / 'tmp').exists())
        self.assertFalse((seen[0] / 'cache').exists())

    def test_missing_tools_are_repaired_without_reinstalling_legacy_packages(self):
        legacy = self.legacy()
        def missing_uv(runtime, stamp, expected, tools, **kwargs):
            result = self.probe(runtime, stamp, expected, tools, **kwargs)
            result['uv']['ready'] = (tools / 'uv').exists()
            result['ready'] = result['ready'] and result['uv']['ready']
            return result
        shared = SharedRuntime(self.version, probe=missing_uv)
        def tools_only(tools, **kwargs):
            tools.mkdir(parents=True)
            (tools / 'uv').touch()
        install = Mock()
        with patch('src.runtime_environment.ensure_uv', side_effect=tools_only) as uv:
            python = shared.ensure(lambda *a, **k: None, install)
        self.assertEqual(uv.call_count, 1)
        self.assertEqual(uv.call_args.args[0], shared.slot / 'tools')
        self.assertEqual(len(uv.call_args.kwargs['pass_fds']), 1)
        install.assert_not_called()
        self.assertEqual(python, legacy / '.venv/bin/python')

    def test_legacy_version_mismatch_and_unowned_or_symlinked_environments_are_not_adopted(self):
        legacy = self.legacy()
        (legacy / 'runtime-version').write_text('outdated')
        unowned = self.legacy('b' * 32)
        (unowned / 'owner.json').write_text('{"app":"other app"}')
        symlink = self.shared.base / 'workers' / ('c' * 32)
        symlink.symlink_to(legacy, target_is_directory=True)
        self.assertFalse(self.client().inspect()['ready'])
        python = self.shared.ensure(lambda *a, **k: None, self.install)
        self.assertTrue(python.is_relative_to(self.shared.slot))
        self.assertEqual((legacy / 'runtime-version').read_text(), 'outdated')
        self.assertTrue(symlink.is_symlink())

    def test_symlink_venv_and_escaping_manifest_are_rejected(self):
        legacy = self.legacy()
        outside = self.home / 'unrelated'
        (legacy / '.venv').rename(outside)
        (legacy / '.venv').symlink_to(outside, target_is_directory=True)
        self.shared.slot.mkdir(parents=True)
        (self.shared.slot / 'current.json').write_text(json.dumps({'root': str(outside)}))
        self.assertFalse(self.shared.inspect()['ready'])
        python = self.shared.ensure(lambda *a, **k: None, self.install)
        self.assertTrue(python.is_relative_to(self.shared.slot))
        self.assertEqual((outside / 'bin/python').read_text(), 'fixture interpreter')

    def test_two_independent_processes_install_once_and_reuse_after_waiting(self):
        script = r'''
import sys
from pathlib import Path
from src.runtime_environment import SharedRuntime
Path.home = classmethod(lambda cls: Path(sys.argv[1]))
def probe(runtime, stamp, expected, tools, **kwargs):
    ready = (runtime/'bin/python').is_file() and stamp.is_file() and stamp.read_text() == expected
    return {'ready':ready, 'uv':{'ready':True}, 'venv':{'ready':ready,'path':str(runtime)}, 'packages':{'ready':ready}}
shared = SharedRuntime('fixture-v1', probe=probe)
def install(root, descriptor):
    print('INSTALLING', flush=True)
    sys.stdin.readline()
    (root/'.venv/bin').mkdir(parents=True)
    (root/'.venv/bin/python').touch()
    (root/'runtime-version').write_text('fixture-v1')
def emit(phase, **details):
    if phase == 'waiting_environment': print('WAITING', flush=True)
print('RESULT ' + str(shared.ensure(emit, install)), flush=True)
'''
        children = []
        try:
            def start(home):
                process = subprocess.Popen([sys.executable, '-u', '-c', script, str(home)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(process)
                return process
            def line(process):
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Child did not report lock state')
                return process.stdout.readline().strip()
            for interrupted in (False, True):
                with self.subTest(interrupted=interrupted):
                    home = self.home / str(interrupted)
                    home.mkdir()
                    first = start(home)
                    self.assertEqual(line(first), 'INSTALLING')
                    second = start(home)
                    self.assertEqual(line(second), 'WAITING')
                    if interrupted:
                        first.kill()
                        first.communicate(timeout=5)
                        self.assertEqual(line(second), 'INSTALLING')
                        second.stdin.write('\n')
                        second.stdin.flush()
                    else:
                        first.stdin.write('\n')
                        first.stdin.flush()
                        first_output, first_error = first.communicate(timeout=5)
                        self.assertEqual(first.returncode, 0, first_error)
                    second_output, second_error = second.communicate(timeout=5)
                    self.assertEqual(second.returncode, 0, second_error)
                    self.assertTrue(second_output.startswith('RESULT '), second_output)
                    if not interrupted:
                        self.assertEqual(first_output, second_output)
                    self.assertNotIn('INSTALLING', second_output)
                    installs = home / '.local/share/cosmoss/runtimes/fixture-v1/installs'
                    self.assertEqual(len(list(installs.iterdir())), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
