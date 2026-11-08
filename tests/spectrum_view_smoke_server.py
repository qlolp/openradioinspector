"""Synthetic spectrum visual fixture, with no HTTP writes or device commands.

Run: python -m tests.spectrum_view_smoke_server
Private stdin commands: advance (both scopes), quit; EOF also stops the server.
Only an ephemeral IPv4 loopback socket and in-memory synthetic sessions are used.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server

BASE = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
FREQUENCIES = [100_000_000, 100_050_000, 100_200_000, 100_900_000, 101_000_000]


def advance(service: SessionService, sequence: int) -> None:
    for suffix in ('one', 'two'):
        service.ingest_sweep({
            'id': f'{suffix}-sweep-{sequence}', 'session_id': f'view-{suffix}',
            'stream_id': f'spectrum-{suffix}',
            'interval': {'started_at': (BASE + timedelta(seconds=sequence * 10)).isoformat(),
                         'ended_at': (BASE + timedelta(seconds=sequence * 10 + 1)).isoformat()},
            'frequencies_hz': FREQUENCIES,
            'levels': [-115 + sequence, -95, -45 - sequence, -100, -85]
                      if suffix == 'one' else [-75, -60, -90 + sequence, -55, -80],
            'level_unit': 'dBm',
            'settings': {'adapter': 'synthetic-fixture', 'simulated': True,
                         'generator_output': 'off', 'sequence': sequence},
        })


def seeded_service() -> SessionService:
    service = SessionService()
    for suffix in ('one', 'two'):
        session = service.create_session({
            'id': f'view-{suffix}', 'name': f'Synthetic spectrum {suffix}',
            'streams': [{'id': f'spectrum-{suffix}', 'device': 'Synthetic tinySA',
                         'role': 'spectrum_analyzer', 'level_unit': 'dBm',
                         'capabilities': ['sweep', 'waterfall']}],
        })
        service.start_session(session.id, at=BASE)
    for sequence in range(1, 5):
        advance(service, sequence)
    return service


def assert_no_workers(server):
    if server.worker_manager.active_count or server.worker_manager.pending_start_count:
        raise AssertionError("Synthetic fixture unexpectedly has device workers")


def create_fixture():
    service = seeded_service()
    server = create_server(host='127.0.0.1', port=0, sessions=service)
    assert_no_workers(server)
    original = server.RequestHandlerClass

    class ReadOnlyHandler(original):
        def setup(self):
            self.request.settimeout(0.5)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                assert_no_workers(server)

        def parse_request(self):
            if not super().parse_request():
                return False
            if self.command != 'GET':
                self.send_error(403, 'Synthetic fixture: HTTP mutations disabled')
                self.close_connection = True
                return False
            return True

        def do_GET(self):
            # Explicit read allowlist excludes SSE and every legacy API route.
            path = self.path.split('?', 1)[0]
            allowed = path in ('/platform', '/platform.html', '/api/v1/health',
                               '/api/v1/capabilities', '/api/v1/sessions',
                               '/api/v1/workers', '/api/v1/events/history', '/api/v1/task-scheduler',
                               '/api/v1/task-scheduler/tasks', '/api/v1/archive/capacity')
            allowed |= path.startswith('/static/')
            allowed |= bool(re.fullmatch(r'/api/v1/sessions/view-(one|two)(/[^.]*)?', path))
            if not allowed or path.endswith('/events'):
                self.send_error(403, 'Synthetic fixture: route disabled')
                return
            super().do_GET()

    server.RequestHandlerClass = ReadOnlyHandler
    server.daemon_threads = False
    return server, service


def read_commands(descriptor, commands):
    """Read a private pipe without holding Python buffered-stream locks at exit."""
    pending = b''
    try:
        while True:
            chunk = os.read(descriptor, 1024)
            if not chunk:
                if pending:
                    commands.put(pending.decode('utf-8', errors='replace'))
                break
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                commands.put(line.decode('utf-8', errors='replace'))
            if len(pending) > 4096:
                raise ValueError('Private stdin command exceeds limit')
    except (OSError, ValueError) as exc:
        commands.put(exc)
    finally:
        commands.put(None)


def main():
    server, service = create_fixture()
    sequence = 4
    server.timeout = 0.05
    commands = queue.Queue()
    reader = threading.Thread(target=read_commands,
                              args=(sys.stdin.fileno(), commands), daemon=True,
                              name='spectrum-fixture-stdin')
    deadline = time.monotonic() + 600
    try:
        reader.start()
        print(json.dumps({'ready': True,
                          'url': f'http://127.0.0.1:{server.server_address[1]}/platform',
                          'scopes': ['view-one/spectrum-one', 'view-two/spectrum-two'],
                          'sequence': sequence, 'frequencies_hz': FREQUENCIES,
                          'level_unit': 'dBm', 'base_utc': BASE.isoformat(),
                          'lifetime_seconds': 600,
                          'stdin': 'advance adds both sweeps (max 32); quit or EOF stops'}), flush=True)
        while time.monotonic() < deadline:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                server.handle_request()
                assert_no_workers(server)
                continue
            if isinstance(command, Exception):
                raise command
            if command is None or command.strip() == 'quit':
                break
            if command.strip() == 'advance' and sequence < 32:
                sequence += 1
                advance(service, sequence)
                assert_no_workers(server)
                print(json.dumps({'advanced': sequence}), flush=True)
            else:
                print(json.dumps({'error': 'Expected advance (up to 32) or quit'}), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
