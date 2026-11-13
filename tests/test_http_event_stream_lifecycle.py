"""Real owned HTTP streams must finish archive work before writer admission ends."""
import http.client
import json
from contextlib import nullcontext
from pathlib import Path
import select
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.event_streams import _ActiveStream, peer_disconnected
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
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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

    def slow_socket_writer(self, send_buffer, *, shutdown_wakes=True):
        server = self.server()
        service = server.session_service
        service.create_session(session_payload())
        writing, exited = threading.Event(), threading.Event()
        outcomes, buffered = [], []
        owned_connections, shutdowns = [], []
        socket_shutdown = socket.socket.shutdown
        def no_write_interrupt(connection, how):
            if how == socket.SHUT_RDWR and any(connection is owned for owned in owned_connections):
                shutdowns.append(how)
                return
            return socket_shutdown(connection, how)
        original = server.RequestHandlerClass._stream_session_events
        def constrained(handler, after=0):
            owned_connections.append(handler.connection)
            handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer)
            return original(handler, after)
        write = _ActiveStream.write
        def observed(stream, data):
            if len(data) < 500000:
                return write(stream, data)
            try:
                # Measure real kernel backpressure, then enter the production
                # cooperative writer. Do not reintroduce a blocking sendall
                # outside the very path whose cancellation this test checks.
                connection = stream.connection
                self.assertEqual(connection.gettimeout(), 0.0)
                total = 0
                deadline = time.monotonic() + 2
                while total < 16 * 1024 * 1024 and time.monotonic() < deadline:
                    try:
                        sent = connection.send(data[:16384])
                    except BlockingIOError:
                        if not select.select([], [connection], [], .05)[1]:
                            break
                    else:
                        if not sent:
                            raise AssertionError('synthetic socket closed before backpressure')
                        total += sent
                else:
                    raise AssertionError(f'synthetic socket never backpressured after {total} bytes')
                buffered.append(total)
                writing.set()
                result = write(stream, data)
                outcomes.append(f'write completed with {result} bytes')
                return result
            except BaseException as error:
                outcomes.append(error)
                raise
            finally:
                exited.set()
        shutdown_model = nullcontext() if shutdown_wakes else patch.object(
            socket.socket, 'shutdown', new=no_write_interrupt)
        with shutdown_model, patch.object(server.RequestHandlerClass, '_stream_session_events', constrained), \
                patch.object(_ActiveStream, 'write', observed):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            self.addCleanup(connection.close)
            # Set the receive window before the TCP handshake, so already
            # advertised Windows receive capacity cannot defeat the constraint.
            connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.sock.settimeout(2)
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            connection.sock.connect(server.server_address)
            connection.request('GET', '/api/v1/events?after=1')
            response = connection.getresponse()
            self.connections.append((connection, response))
            self.assertEqual(response.status, 200)
            service.publish('future.synthetic', payload={'synthetic': 'x' * 1000000})
            self.assertTrue(writing.wait(2), f'synthetic backpressure was not observed: {outcomes!r}')
            self.assertFalse(exited.wait(.05), f'synthetic write exited after buffering {buffered}: {outcomes!r}')
            self.writer_is_busy(service._store.path)
            server.server_close()
        self.assertTrue(exited.is_set())
        if not shutdown_wakes:
            self.assertEqual(shutdowns, [socket.SHUT_RDWR])
        self.assertEqual(len(outcomes), 1, outcomes)
        self.assertIsInstance(outcomes[0], OSError)
        self.assertTrue(peer_disconnected(outcomes[0]), outcomes)
        self.assertFalse(service._stream_notifiers)
        self.assertTrue(service.event_stream_closed)
        owner = archive_writer_owner(service._store.path)
        owner.release_after_archive_closed()

    def test_shutdown_unblocks_a_real_slow_socket_writer_before_archive_close(self):
        self.slow_socket_writer(4096)

    def test_slow_socket_shutdown_also_measures_larger_kernel_send_buffers(self):
        # The original single 1 MB write can complete on this real TCP socket.
        # Exercise the larger-buffer case on every OS, without assuming the OS
        # grants this exact capacity or skipping when its cap is lower.
        self.slow_socket_writer(2 * 1024 * 1024)

    def test_slow_socket_shutdown_drains_when_shutdown_does_not_interrupt_the_write(self):
        # Model the hosted Windows result without replacing the real socket,
        # production stop flag, archive, write, or drain barrier.
        self.slow_socket_writer(4096, shutdown_wakes=False)

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

    def test_sse_socket_mode_does_not_change_an_independent_regular_post(self):
        server = self.server()
        self.connect(server)
        observed = []
        finished = threading.Event()
        post = server.RequestHandlerClass.do_POST
        def regular_post(handler):
            before = (handler.connection.gettimeout(), 'write' in vars(handler.wfile))
            try:
                return post(handler)
            finally:
                observed.append((before, handler.connection.gettimeout(), 'write' in vars(handler.wfile)))
                finished.set()
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            with patch.object(server.RequestHandlerClass, 'do_POST', regular_post):
                connection.request('POST', '/api/v1/sessions', body=json.dumps(session_payload()),
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 201)
                session = json.loads(response.read())['data']
                self.assertEqual(server.session_service.get_session(session['id']).id, session['id'])
                self.assertTrue(finished.wait(2))
            self.assertEqual(observed, [((None, False), None, False)])
        finally:
            connection.close()

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

    def startup_failure(self, phase, *, close_error=False):
        path = str(self.directory / phase / 'archive.sqlite')
        stores, owners = [], []
        real_close = SQLiteSessionStore.close
        def store(*args, **kwargs):
            result = SQLiteSessionStore(*args, **kwargs); stores.append(result); return result
        def owner(*args):
            result = archive_writer_owner(*args); owners.append(result); return result
        target = 'SessionService' if phase == 'constructor' else 'ThreadingHTTPServer'
        failure = ValueError if phase == 'constructor' else OSError
        try:
            with patch('radioinspector.web.server.SQLiteSessionStore', side_effect=store), \
                    patch('radioinspector.web.server.archive_writer_owner', side_effect=owner), \
                    patch('radioinspector.web.server.' + target, side_effect=failure('synthetic startup failure')):
                if close_error:
                    with patch.object(SQLiteSessionStore, 'close', side_effect=OSError('synthetic archive close failure')):
                        with self.assertRaisesRegex(OSError, 'archive close failure'):
                            create_server(host='127.0.0.1', port=0, session_database=path)
                else:
                    with self.assertRaisesRegex(failure, 'startup failure'):
                        create_server(host='127.0.0.1', port=0, session_database=path)
            self.assertEqual(len(stores), 1)
            if close_error:
                self.assertEqual(stores[0]._connection.execute('SELECT 1').fetchone()[0], 1)
                self.writer_is_busy(path)
                with self.assertRaises(RuntimeBridgeError):
                    owners[0].close()
            else:
                with self.assertRaises(sqlite3.ProgrammingError):
                    stores[0]._connection.execute('SELECT 1')
                # POSIX can unlink an open lock file, hiding the Windows cleanup
                # failure. Actual competing admission must also be available.
                replacement = archive_writer_owner(path)
                replacement.release_after_archive_closed()
        finally:
            for item in stores:
                real_close(item)
            for item in owners:
                item.release_after_archive_closed()

    def test_session_constructor_failure_still_closes_the_raw_store(self):
        self.startup_failure('constructor')

    def test_bind_failure_releases_writer_only_after_successful_archive_cleanup(self):
        self.startup_failure('bind')

    def test_startup_archive_cleanup_failure_keeps_the_writer_owned(self):
        self.startup_failure('bind', close_error=True)


if __name__ == '__main__':
    unittest.main()
