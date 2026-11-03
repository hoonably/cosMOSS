"""Exercise the optional-tool transfer policy without external network access."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = bytes(range(256)) * 2048


@unittest.skipUnless(sys.platform == 'darwin', 'Uses the macOS downloader tools')
class DesktopDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cosmoss-download-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.received = []
        records = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                records.append((self.path, self.headers.get('Range')))
                if self.path == '/fail':
                    self.send_error(503); return
                start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
                data = PAYLOAD if self.path != '/corrupt' else b'x' * len(PAYLOAD)
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(data) - start))
                if start: self.send_header('Content-Range', f'bytes {start}-{len(data)-1}/{len(data)}')
                self.end_headers()
                if self.path == '/stall': time.sleep(2.5); return
                if self.path == '/cut':
                    self.wfile.write(data[start:start+65536]); self.wfile.flush(); self.close_connection=True; return
                try:
                    for at in range(start, len(data), 16384):
                        self.wfile.write(data[at:at+16384]); self.wfile.flush()
                        time.sleep(.12 if self.path == '/slow' else .001)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        # HTTP is permitted only in this copied test fixture. Production stays HTTPS-only.
        helper = (ROOT/'scripts/desktop-download.sh').read_text().replace("--proto '=https'", "--proto '=http,https'")
        helper = helper.replace('--speed-time 25', '--speed-time 1').replace('--max-time 600', '--max-time 20').replace('"$idle" -lt 15', '"$idle" -lt 1')
        (self.root/'helper.sh').write_text(helper)
        self.target = self.root/'verified.bin'

    def command(self, paths):
        body = 'set -euo pipefail\nsource '+shlex.quote(str(self.root/'helper.sh'))+'\ndownload_pid=""\n'
        body += 'trap \'[[ -z "$download_pid" ]] || kill "$download_pid" 2>/dev/null || true\' EXIT\ntrap "exit 130" TERM INT\n'
        args=[str(self.target),hashlib.sha256(PAYLOAD).hexdigest(),str(len(PAYLOAD)),*[self.url+p for p in paths]]
        body+='fetch_verified '+' '.join(shlex.quote(x) for x in args)+'\n'
        script=self.root/'run.sh';script.write_text(body)
        return ['/bin/bash',str(script)]

    def run_transfer(self, paths):
        result=subprocess.run(self.command(paths),capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.target.read_bytes(),PAYLOAD)
        return result.stderr

    def test_failover_then_verified_cache_needs_no_request(self):
        log=self.run_transfer(['/fail','/file'])
        self.assertIn(':retry',log)
        requests=len(self.received)
        self.assertIn(':reused',self.run_transfer(['/fail']))
        self.assertEqual(len(self.received),requests)

    def test_interrupted_connection_resumes_identical_bytes(self):
        self.run_transfer(['/cut','/file'])
        self.assertTrue(any(path=='/file' and value=='bytes=65536-' for path,value in self.received),self.received)

    def test_corruption_is_rejected_and_clean_source_used(self):
        self.target.write_bytes(b'bad cache')
        log=self.run_transfer(['/corrupt','/file'])
        self.assertIn('COSMOSS_RUNTIME_ERROR:integrity',log)
        self.assertIn(('/file',None),self.received)

    def test_stalled_source_fails_over(self):
        log=self.run_transfer(['/stall','/file'])
        self.assertIn(':retry',log)
        self.assertTrue(any(path=='/file' for path,_ in self.received))

    def test_app_interruption_keeps_partial_for_next_attempt(self):
        with (self.root/'first.log').open('w') as log:
            process=subprocess.Popen(self.command(['/slow']),stdout=log,stderr=log)
            part=Path(str(self.target)+'.part')
            try:
                deadline=time.monotonic()+5
                while (not part.exists() or part.stat().st_size<32768) and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(part.exists())
            finally:
                process.terminate();process.wait(timeout=10)
        self.assertGreater(part.stat().st_size,0)
        self.assertLess(part.stat().st_size,len(PAYLOAD))
        self.run_transfer(['/file'])
        self.assertTrue(any(path=='/file' and value is not None for path,value in self.received))

    def test_slow_transfer_reports_real_byte_counts(self):
        log=self.run_transfer(['/slow'])
        values=[line.split(':') for line in log.splitlines() if line.startswith('COSMOSS_RUNTIME_PROGRESS:')]
        self.assertTrue(any(0<int(parts[1])<len(PAYLOAD) for parts in values),values)
