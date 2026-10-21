"""Real local subprocesses stand in for SSH and CUDA; no GPU or personal data."""
import asyncio
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src import process_lifecycle
from src.remote_workers import RemoteWorkers

ROOT = Path(__file__).resolve().parents[1]

# Exercise the actual bootstrap/lease/termination/cleanup loop with an ordinary
# HTTP child in place of the model server. Keep the production child arguments
# covered separately by BootstrapTests.
SUPERVISOR = r'''
import json, os, signal, subprocess, sys
from pathlib import Path
from src import remote_bootstrap as b
b.Path.home = classmethod(lambda cls: Path(sys.argv[1]))
b.LEASE_SECONDS = 1.2
b.WORKER_STOP_GRACE_SECONDS = .2
b.prepare_shared_runtime = lambda *a: Path(sys.executable)
b.prepare_model = lambda *a, **kw: {'model_path':sys.argv[1]+'/model', 'model_cache':sys.argv[1]+'/hf'}
b.subprocess.check_output = lambda *a, **kw: b'<nvidia_smi_log><gpu><uuid>GPU-test</uuid><processes/></gpu></nvidia_smi_log>'
spawn = subprocess.Popen
child_source = """
import http.server, json, os, signal, sys, time
if sys.argv[2] == 'stubborn': signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[2] == 'loading': time.sleep(60)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'app':'cosMOSS CUDA worker', 'device_id':os.environ['COSMOSS_DEVICE_ID'], 'gpu_uuid':'GPU-test', 'ok':True}).encode())
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""
def worker(command, **kw):
    return spawn([sys.executable, '-u', '-c', child_source, sys.argv[2], sys.argv[3]], **kw)
b.subprocess.Popen = worker
try: b.main()
except Exception as error:
    print('ERROR:'+str(error), flush=True)
    sys.exit(1)
'''


class SupervisorTests(unittest.TestCase):
    def start_session(self, mode='ready'):
        temp = tempfile.TemporaryDirectory(prefix='cosmoss-lease-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home/'model').mkdir()
        (home/'model/weights').write_bytes(b'keep model')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        device = dict(id='a'*32, api_key='test-token', gpu_uuid='GPU-test', model='test/moss', remote_port=port)
        root = home/'.local/share/cosmoss/workers'/device['id']
        process = subprocess.Popen([sys.executable, '-u', '-c', SUPERVISOR, str(home), str(port), mode],
                                   cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.stdin.close()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(dict(device=device, action='prepare', lease_protocol=1,
                                            source='# stand-in', lifecycle_source='# stand-in')).encode()+b'\n')
        process.stdin.flush()
        return process, root, home

    def until_phase(self, process, phase, timeout=8):
        output = b''
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), output.decode(errors='replace'))
            process.stdin.write(b'PING\n'); process.stdin.flush()
            if select.select([process.stdout], [], [], .1)[0]:
                # Read unbuffered chunks: multiple status lines can arrive in one
                # packet, so select + buffered readline would miss queued lines.
                output += os.read(process.stdout.fileno(), 65536)
                if f'"phase": "{phase}"'.encode() in output:
                    return
        self.fail('Missing phase '+phase+': '+output.decode(errors='replace'))

    def assert_released(self, process, root, home, pid):
        self.assertEqual(process.wait(timeout=6), 1)
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        for name in ('src', 'tmp', 'cache', 'worker.pid', 'worker.json', 'worker.log'):
            self.assertFalse((root/name).exists(), name)
        self.assertEqual((home/'model/weights').read_bytes(), b'keep model')

    def test_connected_session_runs_past_lease_then_silent_link_releases_stubborn_child(self):
        process, root, home = self.start_session('stubborn')
        self.until_phase(process, 'ready')
        pid = int((root/'worker.pid').read_text())
        deadline = time.monotonic()+1.6
        while time.monotonic() < deadline:
            process.stdin.write(b'PING\n'); process.stdin.flush()
            time.sleep(.15)
        self.assertIsNone(process.poll())
        # Leave the channel open but stop PINGs: simulates a black-holed network.
        self.assert_released(process, root, home, pid)

    def test_eof_during_model_loading_stops_child_and_removes_private_artifacts(self):
        process, root, home = self.start_session('loading')
        self.until_phase(process, 'loading')
        deadline = time.monotonic()+2
        while not (root/'worker.pid').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pid = int((root/'worker.pid').read_text())
        process.stdin.close()
        self.assert_released(process, root, home, pid)


class ClientLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_returns_while_ssh_stays_alive_and_stop_closes_stream(self):
        workers = RemoteWorkers()
        device = dict(id='a'*32, host='test-host')
        spawn = asyncio.create_subprocess_exec
        child = r'''
import json, sys
request = json.loads(sys.stdin.readline())
if request['action'] == 'prepare':
    print('COSMOSS:{"phase":"ready"}', flush=True)
    for line in sys.stdin:
        if line.strip() != 'PING': raise RuntimeError('bad heartbeat')
else: print('COSMOSS:{"phase":"stopped"}', flush=True)
'''
        processes = []
        async def local_ssh(*args, **kwargs):
            process = await spawn(sys.executable, '-u', '-c', child, **kwargs)
            processes.append(process)
            return process
        with patch('src.remote_workers.discover_hosts', return_value=['test-host']), \
             patch('src.remote_workers.asyncio.create_subprocess_exec', new=local_ssh), \
             patch('src.remote_workers.HEARTBEAT_SECONDS', .02):
            try:
                result = await asyncio.wait_for(workers.perform(device, 'prepare'), 3)
                self.assertEqual(result['phase'], 'ready')
                self.assertFalse(workers.tasks[device['id']].done())
                await asyncio.sleep(.07)
                self.assertIsNone(processes[0].returncode)
                self.assertEqual((await asyncio.wait_for(workers.perform(device, 'stop'), 3))['phase'], 'stopped')
                self.assertEqual(processes[0].returncode, 0)
            finally:
                await workers.close()

    async def test_startup_failure_reaches_caller_without_waiting_forever_for_ready(self):
        workers = RemoteWorkers()
        with patch.object(workers, 'execute', new=AsyncMock(side_effect=RuntimeError('offline'))):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                await asyncio.wait_for(workers.perform({'id':'a'*32}, 'prepare'), 1)


class ParentGuardTests(unittest.TestCase):
    def test_force_killed_native_parent_does_not_leave_backend_sending_heartbeats(self):
        # Parent forks an actual child backend; child observes its real PPID.
        script = r'''
import os, subprocess, sys, time
source = "import os,time; from src.process_lifecycle import watch_desktop_parent; watch_desktop_parent(); print(os.getpid(),flush=True); time.sleep(60)"
child = subprocess.Popen([sys.executable,'-u','-c',source], env={**os.environ,'COSMOSS_DESKTOP_PARENT_PID':str(os.getpid())})
time.sleep(60)
'''
        parent = subprocess.Popen([sys.executable, '-u', '-c', script], cwd=ROOT, stdout=subprocess.PIPE)
        child = None
        try:
            self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
            child = int(parent.stdout.readline())
            parent.kill(); parent.wait(timeout=3)
            # EOF arrives when the orphan exits and closes its inherited stdout.
            self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
            self.assertEqual(parent.stdout.read(1), b'')
        finally:
            if parent.poll() is None: parent.kill(); parent.wait()
            if child:
                try: os.kill(child, signal.SIGKILL)
                except ProcessLookupError: pass
            parent.stdout.close()

    def test_linux_worker_arms_kernel_guard_and_checks_startup_parent_race(self):
        libc = Mock()
        libc.prctl.return_value = 0
        with patch.dict(os.environ, {'COSMOSS_WORKER_PARENT_PID':'123'}), \
             patch.object(process_lifecycle.sys, 'platform', 'linux'), \
             patch('ctypes.CDLL', return_value=libc), \
             patch.object(process_lifecycle.os, 'getppid', return_value=456), \
             patch.object(process_lifecycle.os, '_exit') as exit:
            process_lifecycle.arm_worker_parent_guard()
        libc.prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)
        exit.assert_called_once_with(1)
