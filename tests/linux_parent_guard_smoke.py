"""Run the exact Linux guard with tiny disposable Python processes, never CUDA.

Usage: python tests/linux_parent_guard_smoke.py [SSH_ALIAS]
No remote files, environment changes or model imports are needed.
"""
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
guard = (ROOT/'src/process_lifecycle.py').read_text()
worker = guard + '\narm_worker_parent_guard()\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n'
parent = f'''
import os, subprocess, sys, time
subprocess.Popen([sys.executable,'-u','-c',{worker!r}], env={{**os.environ,'COSMOSS_WORKER_PARENT_PID':str(os.getpid())}})
time.sleep(60)
'''
controller = f'''
import os, select, signal, subprocess, sys
assert sys.platform == 'linux'
parent = subprocess.Popen([sys.executable,'-u','-c',{parent!r}], stdout=subprocess.PIPE)
child = None
finished = False
try:
    assert select.select([parent.stdout], [], [], 5)[0], 'worker startup timeout'
    line = parent.stdout.readline()
    assert line, 'worker did not arm the guard'
    child = int(line)
    parent.kill(); parent.wait(timeout=3)
    assert select.select([parent.stdout], [], [], 3)[0], 'worker survived supervisor death'
    assert parent.stdout.read(1) == b'', 'worker still owns stdout'
    finished = True
    print('PASS: Linux killed the guarded child after supervisor SIGKILL; no GPU used')
finally:
    if parent.poll() is None: parent.kill(); parent.wait()
    if child and not finished:
        try: os.kill(child, signal.SIGKILL)
        except ProcessLookupError: pass
    parent.stdout.close()
'''
if len(sys.argv) > 1:
    sys.path.insert(0, str(ROOT))
    from src.ssh_devices import discover_hosts, ssh_arguments
    alias = sys.argv[1]
    assert alias in discover_hosts(), 'Use a configured SSH alias'
    command = ssh_arguments(alias) + ['-o', 'ClearAllForwardings=yes', '--', alias,
                                      'python3 -u -c '+shlex.quote(controller)]
else:
    command = [sys.executable, '-u', '-c', controller]
result = subprocess.run(command, capture_output=True, text=True, timeout=25, check=True)
assert 'PASS:' in result.stdout, result.stderr
print(result.stdout.strip())
