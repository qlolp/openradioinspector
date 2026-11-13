"""Owned synthetic backend exercising real HTTP, locks, signals and durable writes."""
import os
import sys
import time

_trace_enabled = os.environ.get('ORI_TEST_BACKEND_DIAGNOSTICS') == '1'
_trace_started = time.monotonic()
_trace_seen = set()
_trace_bytes = 0


def milestone(phase, **metadata):
    """Optional synthetic evidence only; never record credentials or requests."""
    global _trace_bytes
    if not _trace_enabled or phase in _trace_seen or len(_trace_seen) >= 12:
        return
    _trace_seen.add(phase)
    elapsed = int((time.monotonic() - _trace_started) * 1_000_000)
    if 'json' in globals():
        record = dict(phase=phase, elapsed_us=elapsed, pid=os.getpid(), **metadata)
        line = (json.dumps(record, separators=(',', ':')) + '\n').encode('ascii')
    else:
        # First beacon precedes the heavier stdlib imports, including json.
        line = ('{"phase":"python_entered","elapsed_us":%d,"pid":%d}\n'
                % (elapsed, os.getpid())).encode('ascii')
    if len(line) > 1024 or _trace_bytes + len(line) > 4096:
        return
    _trace_bytes += len(line)
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_TRUNC if phase == 'python_entered' else os.O_APPEND
        descriptor = os.open(os.path.join(sys.argv[1], 'backend-startup.ndjson'),
                             flags, 0o600)
        os.write(descriptor, line)
    except OSError:
        pass  # Missing evidence must not change backend readiness or cleanup.
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


milestone('python_entered')
import fcntl
import http.server
import json
import pathlib
import signal
import threading

milestone('imports_complete', python_executable=sys.executable[:128],
          python_executable_truncated=len(sys.executable) > 128,
          python_version='.'.join(str(value) for value in sys.version_info[:3]))
root, port, mode = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
lock = open(root / '.ori-runtime' / 'writer.lock', 'a+')
if mode != 'no-writer':
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    milestone('writer_acquired')
state = root / 'committed-data'
with open(state, 'a') as output:
    output.write(mode + '\n')
    output.flush()
    os.fsync(output.fileno())
milestone('committed')
if mode == 'crash':
    sys.exit(7)
armed = mode == 'armed'

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def authorized(self):
        return self.headers.get('Authorization') == 'Bearer ' + os.environ['ORI_RUNTIME_CONTROL_TOKEN']
    def send_json(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        milestone('status_received')
        if not self.authorized():
            self.send_json(401, {})
            return
        milestone('status_authorized')
        self.send_json(200, dict(version=1, instance_id=os.environ['ORI_RUNTIME_INSTANCE_ID'],
            pid=os.getpid(), ready=mode != 'not-ready', preflight_complete=mode != 'not-ready',
            guard_armed=armed, writer_owned=True))
        milestone('status_replied')
    def do_POST(self):
        if not self.authorized():
            self.send_json(401, {})
            return
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if armed and payload['reason'] == 'cutover' and not payload['acknowledge_coverage_gap']:
            self.send_json(409, {})
            return
        # Persist resume intent exactly; shutdown must never be implemented by disarm.
        (root / 'shutdown-intent.json').write_text(json.dumps(payload))
        self.send_json(202, {})
        milestone('shutdown_accepted')
        threading.Thread(target=server.shutdown).start()

server = http.server.HTTPServer(('127.0.0.1', port), Handler)
milestone('listener_bound')
try:
    server.serve_forever(poll_interval=0.01)
finally:
    server.server_close()
    lock.close()
    milestone('closed')
