"""Owned local-core lifecycle for installed clients and system services.

Starting the core never starts devices or enables acquisition policies. A data
lease prevents concurrent writers using this launcher; remote mode owns no core.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import logging
from logging.handlers import RotatingFileHandler
import json
from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import signal
import sys
import threading
import time

from .tasks import TaskBook
from .web.server import create_server


DEFAULT_ARCHIVE_BYTES = 1024 ** 3


def default_data_directory(platform=None, environment=None, home=None) -> Path:
    platform = platform or sys.platform
    environment = os.environ if environment is None else environment
    home = Path.home() if home is None else Path(home)
    if platform == 'win32':
        return Path(environment.get('LOCALAPPDATA', str(home/'AppData/Local')))/'OpenRadioInspector'
    if platform == 'darwin':
        return home/'Library/Application Support/OpenRadioInspector'
    xdg = environment.get('XDG_DATA_HOME', '')
    return (Path(xdg) if Path(xdg).is_absolute() else home/'.local/share')/'openradioinspector'


class _DiagnosticStream:
    def __init__(self, logger, level, original):
        self.logger, self.level, self.original = logger, level, original

    def write(self, text):
        if text.strip():
            self.logger.log(self.level, text[:65536].rstrip())
        if self.original is not None:
            self.original.write(text)
        return len(text)

    def flush(self):
        if self.original is not None:
            self.original.flush()


class _DiagnosticHandler(RotatingFileHandler):
    def handleError(self, record):
        # Disk errors must not recurse through redirected stderr or stop I/O.
        if sys.__stderr__ is not None:
            sys.__stderr__.write("OpenRadioInspector: diagnostic log is unavailable\n")


@contextmanager
def _diagnostics(directory, name):
    logs = Path(directory).expanduser().resolve()/'logs'
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    handler = _DiagnosticHandler(logs/(name+'.log'), maxBytes=1024*1024,
                                 backupCount=3, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s')
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger = logging.Logger('openradioinspector-'+name, level=logging.INFO)
    logger.addHandler(handler)
    try:
        with redirect_stdout(_DiagnosticStream(logger, logging.INFO, sys.stdout)), \
             redirect_stderr(_DiagnosticStream(logger, logging.ERROR, sys.stderr)):
            try:
                yield
            except Exception:
                logger.exception("Application failed")
                raise
    finally:
        handler.close()


def _core_ready(host, port, timeout=5):
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request('GET', '/api/v1/health')
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            return False
        return json.loads(body).get('data', {}).get('status') == 'ok'
    finally:
        connection.close()


class LocalCore:
    def __init__(self, directory: Path, *, host='127.0.0.1', port=0,
                 archive_max_bytes=DEFAULT_ARCHIVE_BYTES):
        self.directory = Path(directory).expanduser().resolve()
        self.host, self.port, self.archive_max_bytes = host, port, archive_max_bytes
        self.server = self.thread = self._lease = None
        self.url = None

    def _acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self.directory/'core.lock', flags, 0o600)
        lease = os.fdopen(descriptor, 'r+b', buffering=0)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    lease.write(b'\0')
                lease.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lease.close()
            raise RuntimeError('Каталог данных уже используется другим ядром') from exc
        self._lease = lease

    def start(self) -> str:
        if self.server is not None:
            return self.url
        self._acquire()
        try:
            self.server = create_server(
                host=self.host, port=self.port,
                session_database=str(self.directory/'sessions.sqlite3'),
                task_book=TaskBook(self.directory/'tasks.json'),
                archive_max_bytes=self.archive_max_bytes,
            )
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           name='openradioinspector-http', daemon=True)
            self.thread.start()
            host = '127.0.0.1' if self.host == '0.0.0.0' else self.host
            self.url = f'http://{host}:{self.server.server_address[1]}'
            if not _core_ready(host, self.server.server_address[1]):
                raise RuntimeError('Локальное ядро не подтвердило готовность')
            return self.url
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.server is not None:
            server = self.server
            if self.thread is not None and self.thread.is_alive():
                server.shutdown()
                self.thread.join(timeout=10)
                if self.thread.is_alive():
                    raise RuntimeError('HTTP ядра не остановлен; данные не закрыты')
            for resource in (server.automatic_completion, server.automatic_detection,
                             server.automatic_handoff, server.control_scheduler):
                resource.close()
            server.worker_manager.close()
            if server.worker_manager.active_count or server.worker_manager.pending_start_count:
                raise RuntimeError('Приборный I/O ещё выполняется; архив и блокировка сохранены')
            state = server.RequestHandlerClass.state
            state.close()
            server.server_close()
            server.session_service.close()
            server.resource_cleanup.close()
            self.server = self.thread = None
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()


def _desktop_parser():
    parser = argparse.ArgumentParser(description='OpenRadioInspector desktop')
    parser.add_argument('--server', help='Удалённое ядро; локальное не запускается')
    parser.add_argument('--data-dir', type=Path, default=default_data_directory())
    parser.add_argument('--self-check', action='store_true', help='Проверить локальное ядро без GUI и приборов')
    return parser


def main(argv=None) -> int:
    return _desktop_run(_desktop_parser().parse_args(argv))


def installed_main(argv=None) -> int:
    args = _desktop_parser().parse_args(argv)
    try:
        with _diagnostics(args.data_dir, 'desktop'):
            return _desktop_run(args)
    except Exception as error:
        try:
            from tkinter.messagebox import showerror
            if not args.self_check:
                showerror('OpenRadioInspector', f'Не удалось запустить приложение: {error}\nЖурнал: {args.data_dir}/logs/desktop.log')
        except Exception:
            pass  # The private log is still available when a display is absent.
        return 1


def _desktop_run(args):
    if args.self_check:
        if args.server:
            _desktop_parser().error('--self-check требует локальное ядро')
        with LocalCore(args.data_dir):
            return 0
    from .windows.scheduler_app import main as gui
    if args.server:
        return gui(['--server', args.server])
    with LocalCore(args.data_dir) as core:
        return gui(['--server', core.url])


def core_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='OpenRadioInspector owned core')
    parser.add_argument('--data-dir', type=Path, default=default_data_directory())
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--archive-max-bytes', type=int, default=DEFAULT_ARCHIVE_BYTES)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--check-ready', action='store_true', help='Проверить уже запущенное ядро без открытия архива')
    parser.add_argument('--stdin-lifeline', action='store_true', help='EOF родителя останавливает только это ядро')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or args.archive_max_bytes <= 0:
        parser.error('Некорректный порт или предел архива')
    if args.check_ready:
        host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if _core_ready(host, args.port, timeout=1):
                    return 0
            except (OSError, ValueError, HTTPException):
                pass
            time.sleep(.1)
        return 1
    with _diagnostics(args.data_dir, 'core'):
        return _serve_core(args)


def _serve_core(args):
    stop = threading.Event()
    handlers = {}
    ready_owned = False
    def request_stop(*_args):
        stop.set()
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGINT', 'SIGTERM'):
            signum = getattr(signal, name)
            handlers[signum] = signal.signal(signum, request_stop)
    def watch_parent():
        try:
            while sys.stdin.buffer.read(1):
                pass
        finally:
            stop.set()
    try:
        with LocalCore(args.data_dir, host=args.host, port=args.port,
                       archive_max_bytes=args.archive_max_bytes) as core:
            if args.ready_file:
                descriptor = os.open(args.ready_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w', encoding='ascii') as out:
                    out.write(core.url+'\n')
                ready_owned = True
            print('OpenRadioInspector ready: '+core.url, flush=True)
            if args.stdin_lifeline:
                threading.Thread(target=watch_parent, name='core-parent-lifeline', daemon=True).start()
            stop.wait()
        return 0
    finally:
        if ready_owned:
            args.ready_file.unlink(missing_ok=True)
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


if __name__ == '__main__':
    raise SystemExit(core_main())
