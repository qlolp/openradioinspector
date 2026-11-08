"""Portable lifecycle for synthetic-only HTTP smoke fixtures.

Only this module creates archive paths, inside its own temporary directory.
No command-line archive/device input or network shutdown endpoint is exposed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Callable

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.tasks import TaskBook
from radioinspector.web.server import create_server
from tests.loopback_dns import local_loopback_names


def logical_digest(path: Path) -> str:
    # sqlite3's transaction context manager does NOT close the connection.
    with closing(sqlite3.connect(path)) as database:
        return hashlib.sha256('\n'.join(database.iterdump()).encode('utf-8')).hexdigest()


def _serve(server: Any, shutdown_stdin: bool) -> None:
    if not shutdown_stdin:
        server.serve_forever()
        return
    stopped = threading.Event()
    descriptor = sys.stdin.fileno()
    failures = []

    def wait_for_eof() -> None:
        try:
            # Do not hold a BufferedReader lock in a daemon during Ctrl-C exit.
            # Ignore any content: only closing the runner's private pipe stops us.
            while os.read(descriptor, 4096):
                pass
        except OSError as exc:
            failures.append(exc)
        finally:
            stopped.set()

    watcher = threading.Thread(target=wait_for_eof, name='smoke-stdin', daemon=True)
    watcher.start()
    server.timeout = 0.1
    # Unlike shutdown()/serve_forever(), this also handles EOF before startup
    # without a race or a thread waiting for a loop that has not started yet.
    while not stopped.is_set():
        server.handle_request()
    watcher.join()
    if failures:
        raise failures[0]


def run_fixture(
    populate: Callable[[SessionService], dict[str, Any]],
    *,
    default_port: int,
    prefix: str,
    description: str | None = None,
    argv: list[str] | None = None,
) -> None:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--port', type=int, default=default_port)
    parser.add_argument('--shutdown-stdin', action='store_true',
                        help='Stop when the private parent stdin pipe reaches EOF')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    # Windows redirected output need not inherit a UTF-8 locale.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        path = Path(directory) / 'synthetic.sqlite'
        with SQLiteSessionStore(str(path)) as store:
            metadata = populate(SessionService(store=store))
        before = logical_digest(path)
        try:
            with ExitStack() as cleanup:
                store = cleanup.enter_context(SQLiteSessionStore(str(path)))
                service = SessionService(store=store)
                with local_loopback_names():
                    server = create_server(
                        host='127.0.0.1', port=args.port, sessions=service,
                        task_book=TaskBook(Path(directory) / 'tasks.json'),
                    )
                # ExitStack still runs all callbacks if one close fails.
                for name in ('worker_manager', 'automatic_handoff',
                             'automatic_detection', 'automatic_completion', 'control_scheduler'):
                    cleanup.callback(getattr(server, name).close)
                cleanup.callback(server.server_close)
                # Drain accepted HTTP requests before closing their shared store.
                server.daemon_threads = False
                print(json.dumps({
                    **metadata,
                    'url': f'http://127.0.0.1:{server.server_address[1]}/platform',
                    'dump_sha256_before': before,
                }, ensure_ascii=False), flush=True)
                try:
                    _serve(server, args.shutdown_stdin)
                except KeyboardInterrupt:
                    pass
        finally:
            after = logical_digest(path)
            print(json.dumps({'dump_sha256_after': after, 'unchanged': after == before}), flush=True)
            if before != after:
                raise AssertionError('Synthetic archive mutated during read-only smoke')
