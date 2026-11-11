"""Owned synthetic backend exercising real HTTP, locks, signals and durable writes."""
import fcntl
import http.server
import json
import os
import pathlib
import signal
import sys
import threading

root, port, mode = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
lock = open(root / '.ori-runtime' / 'writer.lock', 'a+')
if mode != 'no-writer':
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
state = root / 'committed-data'
with open(state, 'a') as output:
    output.write(mode + '\n')
    output.flush()
    os.fsync(output.fileno())
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
        if not self.authorized():
            self.send_json(401, {})
            return
        self.send_json(200, dict(version=1, instance_id=os.environ['ORI_RUNTIME_INSTANCE_ID'],
            pid=os.getpid(), ready=mode != 'not-ready', preflight_complete=mode != 'not-ready',
            guard_armed=armed, writer_owned=True))
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
        threading.Thread(target=server.shutdown).start()

server = http.server.HTTPServer(('127.0.0.1', port), Handler)
try:
    server.serve_forever(poll_interval=0.01)
finally:
    server.server_close()
    lock.close()
