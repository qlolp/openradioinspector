"""Owned synthetic data only: fixture startup, shutdown and Windows-safe cleanup."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from radioinspector.web import server as web_server
from tests import smoke_fixture_support as support


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    'average': 'average-one',
    'sweep_change_detection': 'changes-one',
    'frequency_rounding': 'rounding-one',
    'sweep_context': 'context-one',
    'three_thresholds': 'three-one',
    'spectral_mask': 'mask-one',
    'adaptive_threshold': 'threshold-one',
}


class FixtureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix='ori-fixture-unit-')
        self.addCleanup(self.workspace.cleanup)
        self.stores = []
        self.output = io.StringIO()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        original_directory = tempfile.TemporaryDirectory
        original_store = support.SQLiteSessionStore

        def directory(**kwargs):
            return original_directory(dir=self.workspace.name, **kwargs)

        def store(path):
            result = original_store(path)
            self.stores.append(result)
            return result

        self.stack.enter_context(patch.object(support.tempfile, 'TemporaryDirectory', directory))
        self.stack.enter_context(patch.object(support, 'SQLiteSessionStore', store))
        self.stack.enter_context(patch('sys.stdout', self.output))

    def run_fixture(self, populate=None, argv=None):
        def seed(service):
            service.create_session({'id': 'owned-synthetic', 'name': 'Проверка', 'streams': []})
            return {'session': 'owned-synthetic', 'label': 'Проверка'}

        support.run_fixture(populate or seed, default_port=0, prefix='synthetic-',
                            argv=['--port', '0'] if argv is None else argv)

    def assert_clean(self):
        for store in self.stores:
            with self.assertRaises(sqlite3.ProgrammingError):
                store._connection.execute('SELECT 1')
        self.assertEqual(list(Path(self.workspace.name).iterdir()), [])

    def test_seed_failure_closes_store_and_removes_temporary_files(self):
        def fail(service):
            service.create_session({'id': 'synthetic', 'name': 'synthetic', 'streams': []})
            raise RuntimeError('seed failed')

        with self.assertRaisesRegex(RuntimeError, 'seed failed'):
            self.run_fixture(populate=fail)
        self.assertEqual(self.output.getvalue(), '')
        self.assertEqual(len(self.stores), 1)
        self.assert_clean()

    def test_restore_failure_closes_both_stores(self):
        original = support.SessionService
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise RuntimeError('restore failed')
            return original(**kwargs)

        with patch.object(support, 'SessionService', create):
            with self.assertRaisesRegex(RuntimeError, 'restore failed'):
                self.run_fixture()
        self.assertEqual(len(self.stores), 2)
        self.assert_clean()

    def track_controller_closes(self, stack):
        closed = []
        for name in ('TaskScheduler', 'AutomaticInvestigationCompletion',
                     'AutomaticCandidateDetector', 'AutomaticHandoffScheduler', 'DeviceWorkerManager'):
            cls = getattr(web_server, name)
            original = cls.close

            def close(instance, original=original, name=name):
                closed.append(name)
                original(instance)

            stack.enter_context(patch.object(cls, 'close', close))
        return closed

    def test_bind_failure_closes_controllers_store_and_task_book(self):
        with ExitStack() as stack:
            closed = self.track_controller_closes(stack)
            stack.enter_context(patch.object(web_server, 'ThreadingHTTPServer',
                                             side_effect=OSError('bind failed')))
            with self.assertRaisesRegex(OSError, 'bind failed'):
                self.run_fixture()
        self.assertEqual(len(set(closed)), 5)
        self.assert_clean()

    def test_close_failure_still_closes_every_controller_and_store(self):
        create = support.create_server

        def failing_close(**kwargs):
            server = create(**kwargs)
            close = server.server_close

            def fail():
                close()
                raise RuntimeError('close failed')

            server.server_close = fail
            return server

        with ExitStack() as stack:
            closed = self.track_controller_closes(stack)
            stack.enter_context(patch.object(support, 'create_server', failing_close))
            stack.enter_context(patch.object(support, '_serve'))
            with self.assertRaisesRegex(RuntimeError, 'close failed'):
                self.run_fixture()
        self.assertEqual(len(set(closed)), 5)
        self.assert_clean()

    def test_keyboard_interrupt_preserves_successful_manual_shutdown(self):
        with patch.object(support, '_serve', side_effect=KeyboardInterrupt):
            self.run_fixture()
        before, after = map(json.loads, self.output.getvalue().splitlines())
        self.assertEqual(before['dump_sha256_before'], after['dump_sha256_after'])
        self.assertTrue(after['unchanged'])
        self.assert_clean()

    def test_stdin_read_failure_is_not_reported_as_successful_eof(self):
        with patch('sys.stdin', Mock()), patch.object(support.os, 'read', side_effect=OSError('stdin failed')):
            with self.assertRaisesRegex(OSError, 'stdin failed'):
                support._serve(Mock(), shutdown_stdin=True)

    def test_archive_mutation_reports_false_and_fails(self):
        def mutate(server, shutdown_stdin):
            server.session_service.create_session({'id': 'changed', 'name': 'changed', 'streams': []})

        with patch.object(support, '_serve', mutate):
            with self.assertRaisesRegex(AssertionError, 'archive mutated'):
                self.run_fixture()
        before, after = map(json.loads, self.output.getvalue().splitlines())
        self.assertFalse(after['unchanged'])
        self.assertNotEqual(before['dump_sha256_before'], after['dump_sha256_after'])
        self.assert_clean()

    def test_utf8_output_ignores_redirected_legacy_encoding(self):
        data = io.BytesIO()
        with io.TextIOWrapper(data, encoding='cp1252') as output:
            with patch('sys.stdout', output), patch.object(support, '_serve'):
                self.run_fixture()
            output.flush()
            self.assertIn('Проверка', data.getvalue().decode('utf-8'))
        self.assert_clean()

    def test_cli_rejects_user_archive_device_and_invalid_port(self):
        for argv in (['--archive', 'not-opened'], ['--device', 'not-opened'],
                     ['--port', '-1'], ['--port', '65536']):
            with self.subTest(argv=argv), patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    self.run_fixture(argv=argv)
                self.assertEqual(result.exception.code, 2)
        self.assertEqual(self.stores, [])
        self.assert_clean()

    def test_digest_closes_connection_even_when_dump_fails(self):
        path = Path(self.workspace.name) / 'owned.sqlite'
        with closing(sqlite3.connect(path)) as database:
            database.execute('CREATE TABLE fixture (value TEXT)')
            database.execute('INSERT INTO fixture VALUES (?)', ('Проверка',))
            database.commit()
        connect = sqlite3.connect
        connections = []

        class BrokenDump(sqlite3.Connection):
            def iterdump(self):
                raise RuntimeError('dump failed')

        for fail in (False, True):
            def tracked(target):
                database = connect(target, factory=BrokenDump if fail else sqlite3.Connection)
                connections.append(database)
                return database

            with patch.object(support.sqlite3, 'connect', tracked):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'dump failed'):
                        support.logical_digest(path)
                else:
                    self.assertRegex(support.logical_digest(path), r'^[0-9a-f]{64}$')
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[-1].execute('SELECT 1')

    def test_shutdown_drains_inflight_handler_before_store_close_and_digest(self):
        entered, release, closing_server = (threading.Event() for _ in range(3))
        failures, clients = [], []
        create = support.create_server

        def create_tracked(**kwargs):
            server = create(**kwargs)
            close = server.server_close

            def close_tracked():
                closing_server.set()
                close()

            server.server_close = close_tracked
            return server

        def handler(request):
            entered.set()
            if not release.wait(5):
                failures.append('request was not released')
            request._send_json({'synthetic': True})

        def serve_one(server, shutdown_stdin):
            def client():
                try:
                    with urlopen(f'http://127.0.0.1:{server.server_address[1]}/', timeout=5) as response:
                        response.read()
                except Exception as exc:
                    failures.append(exc)

            client_thread = threading.Thread(target=client)
            clients.append(client_thread)
            client_thread.start()
            server.handle_request()
            if not entered.wait(3):
                raise AssertionError('handler did not start')

        def fixture():
            try:
                self.run_fixture()
            except BaseException as exc:
                failures.append(exc)

        with patch.object(support, 'create_server', create_tracked), \
                patch.object(support, '_serve', serve_one), \
                patch.object(web_server.Handler, 'do_GET', handler):
            thread = threading.Thread(target=fixture)
            thread.start()
            try:
                self.assertTrue(closing_server.wait(5))
                self.assertTrue(thread.is_alive())
                self.assertNotIn('dump_sha256_after', self.output.getvalue())
                self.stores[-1]._connection.execute('SELECT 1')
            finally:
                release.set()
                thread.join(5)
                for client in clients:
                    client.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assert_clean()


class FixtureProcessTests(unittest.TestCase):
    def check_process(self, name, session, early_eof=False):
        with tempfile.TemporaryDirectory(prefix='ori-fixture-process-') as directory:
            root = Path(directory)
            temporary = root / 'owned-temp'
            temporary.mkdir()
            environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
                               TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
            log_path = root / 'stdout.jsonl'
            error_path = root / 'stderr.txt'
            with log_path.open('w', encoding='utf-8') as output, error_path.open('w', encoding='utf-8') as errors:
                process = subprocess.Popen(
                    [sys.executable, '-B', '-m', f'tests.{name}_smoke_server', '--port', '0', '--shutdown-stdin'],
                    cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                )
                try:
                    if early_eof:
                        process.stdin.close()
                    deadline = time.monotonic() + 15
                    while True:
                        lines = log_path.read_text(encoding='utf-8').splitlines()
                        if lines:
                            break
                        self.assertIsNone(process.poll(), error_path.read_text(encoding='utf-8'))
                        self.assertLess(time.monotonic(), deadline, 'fixture readiness timed out')
                        time.sleep(0.02)
                    metadata = json.loads(lines[0])
                    self.assertRegex(metadata['url'], r'^http://127\.0\.0\.1:[1-9][0-9]*/platform$')
                    self.assertEqual(metadata['session'], session)
                    self.assertRegex(metadata['dump_sha256_before'], r'^[0-9a-f]{64}$')
                    if not early_eof:
                        process.stdin.write(b'not a shutdown command\n')
                        process.stdin.flush()
                        with urlopen(metadata['url'], timeout=5) as response:
                            self.assertEqual(response.status, 200)
                            response.read()
                        base = metadata['url'].removesuffix('/platform')
                        with urlopen(f'{base}/api/v1/sessions/{session}/sweep-references?stream_id=spectrum-one',
                                     timeout=5) as response:
                            references = json.load(response)['data']['items']
                        self.assertEqual(references[0]['name'], 'Synthetic retained reference')
                        process.stdin.close()
                    process.wait(timeout=15)
                finally:
                    if not process.stdin.closed:
                        process.stdin.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, error_path.read_text(encoding='utf-8'))
            before, after = map(json.loads, log_path.read_text(encoding='utf-8').splitlines())
            self.assertEqual(before['dump_sha256_before'], after['dump_sha256_after'])
            self.assertIs(after['unchanged'], True)
            self.assertEqual(list(temporary.iterdir()), [], 'fixture left temporary files or task-book locks')

    def test_all_seven_fixtures_bind_ephemeral_ports_and_stop_on_private_stdin_eof(self):
        for name, session in FIXTURES.items():
            with self.subTest(fixture=name):
                self.check_process(name, session)

    def test_stdin_closed_before_startup_is_race_free(self):
        self.check_process('average', 'average-one', early_eof=True)


if __name__ == '__main__':
    unittest.main()
