"""Real owned HTTP streams must finish archive work before writer admission ends."""
import http.client
import json
from pathlib import Path
import sqlite3
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from radioinspector.writer_ownership import archive_writer_owner
from tests.test_platform import session_payload


class HttpEventStreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.servers = []
        self.connections = []
        self.external = []
        self.addCleanup(self.stop)

    def server(self, sessions=None):
        arguments = {'sessions': sessions} if sessions is not None else {
            'session_database': str(self.directory / f'owned-{len(self.servers)}' / 'archive.sqlite')}
        server = create_server(host='127.0.0.1', port=0, **arguments)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.servers.append((server, thread))
        thread.start()
        return server

    def stop(self):
        for connection, response in self.connections:
            response.close(); connection.close()
        for server, thread in reversed(self.servers):
            if thread.is_alive():
                server.shutdown(); thread.join(3)
            server.server_close()
            # Fixture-only ownership also covers cleanup after a failing RED run.
            if server.session_service not in self.external:
                server.session_service.close()
        for service in self.external:
            service.close()

    def connect(self, server, after=0, *, status=200, path=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request('GET', path or f'/api/v1/events?after={after}')
        response = connection.getresponse()
        self.connections.append((connection, response))
        self.assertEqual(response.status, status)
        return connection, response

    def next_event(self, response):
        fields = {}
        while True:
            line = response.readline(2 * 1024 * 1024)
            self.assertTrue(line, 'owned SSE closed before its next event')
            line = line.decode('utf-8').rstrip('\r\n')
            if not line and fields:
                return json.loads(fields['data'])['data']
            if line and not line.startswith(':'):
                key, value = line.split(':', 1)
                fields[key] = value.lstrip(' ')

    def writer_is_busy(self, path):
        try:
            unexpected = archive_writer_owner(path)
        except RuntimeBridgeError as error:
            self.assertEqual(error.code, 'runtime_writer_busy')
        else:
            unexpected.release_after_archive_closed()
            self.fail('a second writer entered before the stream finished archive work')

    def blocked_close(self, phase):
        server = self.server()
        service = server.session_service
        service.create_session(session_payload())
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        if phase == 'reader':
            target, name = service._store, 'next_stream_event'
        else:
            from radioinspector.web import server as server_module
            target, name = server_module, 'json_dumps'
        original = getattr(target, name)

        def blocked(*args, **kwargs):
            if phase == 'encoder' and 'event_type' not in args[0].get('data', {}):
                return original(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic owner did not release archive work')
            return original(*args, **kwargs)

        def close():
            try:
                server.server_close()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        closer = threading.Thread(target=close)
        with patch.object(target, name, side_effect=blocked):
            try:
                _connection, response = self.connect(server)
                self.assertTrue(entered.wait(2))
                notifier = next(iter(service._stream_notifiers))
                closer.start()
                self.assertFalse(finished.wait(.1), 'server_close returned while archive work remained active')
                self.writer_is_busy(service._store.path)
                self.assertEqual(service._store._connection.execute('SELECT 1').fetchone()[0], 1)
                # Closing this server must stop new subscriptions before draining.
                self.assertTrue(notifier.wait(2))
                self.connect(server, status=503)
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(service.event_stream_closed)
        self.assertFalse(service._stream_notifiers)
        self.assertEqual(response.read(1), b'')
        with self.assertRaises(sqlite3.ProgrammingError):
            service._store._connection.execute('SELECT 1')
        owner = archive_writer_owner(service._store.path)
        owner.release_after_archive_closed()

    def test_owned_stream_reader_drains_before_archive_and_writer_release(self):
        self.blocked_close('reader')

    def test_owned_stream_encoder_drains_before_archive_and_writer_release(self):
        self.blocked_close('encoder')

    def test_drain_failure_keeps_archive_open_and_allows_a_proven_retry(self):
        server = self.server()
        service = server.session_service
        service.create_session(session_payload())
        entered, release, unwatched = threading.Event(), threading.Event(), threading.Event()
        read = service._store.next_stream_event
        unwatch = service.unwatch_commits
        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic reader was not released')
            return read(**kwargs)
        def observed(notifier):
            unwatch(notifier); unwatched.set()
        with patch.object(service._store, 'next_stream_event', side_effect=blocked), \
                patch.object(service, 'unwatch_commits', side_effect=observed):
            try:
                self.connect(server)
                self.assertTrue(entered.wait(2))
                with patch('radioinspector.web.event_streams.DRAIN_TIMEOUT_SECONDS', .05):
                    with self.assertRaisesRegex(RuntimeError, 'event streams'):
                        server.server_close()
                self.writer_is_busy(service._store.path)
                self.assertFalse(service.event_stream_closed)
                self.assertEqual(service._store._connection.execute('SELECT 1').fetchone()[0], 1)
            finally:
                release.set()
            self.assertTrue(unwatched.wait(2))
        server.server_close()
        self.assertTrue(service.event_stream_closed)
        owner = archive_writer_owner(service._store.path)
        owner.release_after_archive_closed()

    def test_legacy_stream_uses_the_same_server_shutdown_lifetime(self):
        server = self.server()
        _connection, response = self.connect(server, path='/api/stream')
        self.assertTrue(response.readline().startswith(b'data: '))
        self.assertEqual(response.readline(), b'\n')
        state = server.RequestHandlerClass.state
        server.server_close()
        self.assertEqual(response.read(1), b'')
        self.assertFalse(state._subscribers)

    def test_shutdown_unblocks_a_real_slow_socket_writer_before_archive_close(self):
        server = self.server()
        service = server.session_service
        service.create_session(session_payload())
        writing, exited = threading.Event(), threading.Event()
        original = server.RequestHandlerClass._stream_session_events
        def constrained(handler, after=0):
            handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            write = handler.wfile.write
            def observed(data):
                if len(data) < 500000:
                    return write(data)
                writing.set()
                try:
                    return write(data)
                finally:
                    exited.set()
            handler.wfile.write = observed
            return original(handler, after)
        with patch.object(server.RequestHandlerClass, '_stream_session_events', constrained):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.connect()
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            connection.request('GET', '/api/v1/events?after=1')
            response = connection.getresponse()
            self.connections.append((connection, response))
            self.assertEqual(response.status, 200)
            service.publish('future.synthetic', payload={'synthetic': 'x' * 1000000})
            self.assertTrue(writing.wait(2))
            self.assertFalse(exited.wait(.05), 'synthetic write did not fill its constrained socket')
            server.server_close()
        self.assertTrue(exited.is_set())
        self.assertFalse(service._stream_notifiers)
        self.assertTrue(service.event_stream_closed)
        owner = archive_writer_owner(service._store.path)
        owner.release_after_archive_closed()

    def test_closing_one_server_preserves_injected_service_and_other_streams(self):
        store = SQLiteSessionStore(str(self.directory / 'injected.sqlite'))
        service = SessionService(store=store)
        self.external.append(service)
        session = service.create_session(session_payload())
        first, second = self.server(service), self.server(service)
        _connection, first_response = self.connect(first)
        _connection, second_response = self.connect(second)
        self.assertEqual(self.next_event(first_response)['sequence'], 1)
        self.assertEqual(self.next_event(second_response)['sequence'], 1)
        first.server_close()
        self.assertEqual(first_response.read(1), b'')
        self.assertFalse(service.event_stream_closed)
        self.assertEqual(service.get_session(session.id).id, session.id)
        event = service.publish('future.synthetic', session_id=session.id)
        self.assertEqual(self.next_event(second_response)['sequence'], event.sequence)
        third = self.server(service)
        _connection, reused = self.connect(third, after=1)
        self.assertEqual(self.next_event(reused)['sequence'], event.sequence)

    def test_peer_eof_promptly_releases_subscription_without_closing_service(self):
        server = self.server()
        service = server.session_service
        session = service.create_session(session_payload())
        service.start_session(session.id)
        done = threading.Event()
        unwatch = service.unwatch_commits
        def observed(notifier):
            unwatch(notifier); done.set()
        with patch.object(service, 'unwatch_commits', side_effect=observed):
            connection, response = self.connect(server, after=2)
            response.close(); connection.close()
            self.assertTrue(done.wait(2), 'closed peer retained a stream until the heartbeat')
        self.assertFalse(service.event_stream_closed)
        self.assertEqual(service.get_session(session.id).state.value, 'running')
        self.assertEqual(service.health()['last_event_id'], 2)

    def test_failed_store_close_cannot_turn_empty_exitstack_into_success(self):
        path = str(self.directory / 'failed' / 'archive.sqlite')
        real_close = SQLiteSessionStore.close
        with patch.object(SQLiteSessionStore, 'close', side_effect=OSError('synthetic archive close failure')):
            server = create_server(host='127.0.0.1', port=0, session_database=path)
            try:
                with self.assertRaisesRegex(OSError, 'archive close failure'):
                    server.server_close()
                self.assertEqual(server.session_service._store._connection.execute('SELECT 1').fetchone()[0], 1)
                self.writer_is_busy(path)
                with self.assertRaises((RuntimeError, OSError)):
                    server.server_close()
                with self.assertRaises((RuntimeError, OSError)):
                    server.resource_cleanup.close()
                self.writer_is_busy(path)
            finally:
                real_close(server.session_service._store)
                server.archive_owner.release_after_archive_closed()

    def test_session_constructor_failure_still_closes_the_raw_store(self):
        path = str(self.directory / 'constructor' / 'archive.sqlite')
        stores, owners = [], []
        def store(*args, **kwargs):
            result = SQLiteSessionStore(*args, **kwargs); stores.append(result); return result
        def owner(*args):
            result = archive_writer_owner(*args); owners.append(result); return result
        try:
            with patch('radioinspector.web.server.SQLiteSessionStore', side_effect=store), \
                    patch('radioinspector.web.server.archive_writer_owner', side_effect=owner), \
                    patch('radioinspector.web.server.SessionService', side_effect=ValueError('synthetic constructor failure')):
                with self.assertRaisesRegex(ValueError, 'constructor failure'):
                    create_server(host='127.0.0.1', port=0, session_database=path)
            self.assertEqual(len(stores), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                stores[0]._connection.execute('SELECT 1')
        finally:
            for item in stores:
                item.close()
            for item in owners:
                item.release_after_archive_closed()


if __name__ == '__main__':
    unittest.main()
