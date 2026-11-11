"""SSE-only socket mode and exact partial sends on owned real TCP connections."""
import errno
import socket
from socketserver import _SocketWriter
import unittest
from unittest.mock import patch

from radioinspector.web.event_streams import EventStreams


class EventStreamWriterTests(unittest.TestCase):
    def setUp(self):
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.client.close)
        self.client.settimeout(2)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            self.client.connect(listener.getsockname())
            self.connection, _ = listener.accept()
        self.addCleanup(self.connection.close)
        self.connection.settimeout(1.75)
        self.writer = _SocketWriter(self.connection)
        self.addCleanup(self.writer.close)
        self.streams = EventStreams()

    def received(self, size):
        result = bytearray()
        while len(result) < size:
            block = self.client.recv(size - len(result))
            self.assertTrue(block, 'owned TCP connection ended before all original bytes')
            result.extend(block)
        return bytes(result)

    def test_partial_send_offsets_keep_original_bytes_and_restore_ordinary_writer(self):
        payload = bytes(range(256)) * 9
        send = socket.socket.send
        calls = []
        def partial(connection, data, *args, **kwargs):
            if connection is self.connection:
                calls.append(len(data))
                if len(calls) == 3:
                    # A readiness race after real partial progress sends no
                    # bytes. The subsequent real TCP read detects duplication.
                    raise BlockingIOError(errno.EWOULDBLOCK, 'synthetic readiness race')
                data = data[:7]
            return send(connection, data, *args, **kwargs)
        self.assertNotIn('write', vars(self.writer))
        with patch.object(socket.socket, 'send', new=partial):
            with self.streams.register(self.connection, writer=self.writer):
                self.assertEqual(self.connection.gettimeout(), 0.0)
                self.assertEqual(self.writer.write(payload), len(payload))
                self.assertEqual(self.received(len(payload)), payload)
        self.assertGreater(len(calls), 3)
        self.assertEqual(self.connection.gettimeout(), 1.75)
        self.assertNotIn('write', vars(self.writer))
        self.assertEqual(self.streams.active_count, 0)
        # The ordinary unbuffered writer and its original timeout still work.
        self.writer.write(b'ordinary-tail')
        self.assertEqual(self.received(13), b'ordinary-tail')
        self.streams.close()

    def test_encoder_failure_restores_the_exact_existing_instance_override(self):
        write = self.writer.write
        def override(data):
            return write(data)
        self.writer.write = override
        try:
            with self.assertRaisesRegex(ValueError, 'synthetic encoder failure'):
                with self.streams.register(self.connection, writer=self.writer):
                    self.writer.write(b'original-frame')
                    raise ValueError('synthetic encoder failure')
            self.assertIs(self.writer.write, override)
            self.assertEqual(self.connection.gettimeout(), 1.75)
            self.assertEqual(self.received(14), b'original-frame')
            self.assertEqual(self.streams.active_count, 0)
            self.streams.close()
        finally:
            del self.writer.write

    def test_failed_mode_restore_is_visible_and_cannot_become_a_clean_second_close(self):
        settimeout = socket.socket.settimeout
        def failed_restore(connection, timeout):
            if connection is self.connection and timeout == 1.75:
                raise OSError('synthetic socket restore refusal')
            return settimeout(connection, timeout)
        with patch.object(socket.socket, 'settimeout', new=failed_restore):
            with self.assertRaisesRegex(OSError, 'socket restore refusal'):
                with self.streams.register(self.connection, writer=self.writer):
                    self.writer.write(b'original-frame')
        self.assertEqual(self.received(14), b'original-frame')
        self.assertNotIn('write', vars(self.writer))
        self.assertEqual(self.streams.active_count, 0)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'socket restoration failed'):
                self.streams.close()


if __name__ == '__main__':
    unittest.main()
