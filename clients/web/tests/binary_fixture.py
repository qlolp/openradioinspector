"""Synthetic v1 WS router over the real temporary core archive, test-only.

The production Rust router is validated separately. This independent small encoder
and actual TCP upgrade avoid a per-frame browser-automation IPC roundtrip.
"""
import base64
import hashlib
import json
import select
import socket
import struct
from urllib.parse import parse_qs, urlsplit

from radioinspector.contracts import api_payload

PROTOCOL = 'ori.measurements.v1.protobuf'
MAX_FRAME = 16 * 1024 * 1024 + 64 * 1024


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(tag, value):
    return varint((tag << 3) | 2) + varint(len(value)) + value


def encode(event):
    envelope = api_payload(event)
    value = envelope['data']
    spectrum = b''
    if value['event_type'] == 'sweep.received':
        sweep = value['payload']['sweep']
        if all(0 <= frequency <= 2**64 - 1 for frequency in sweep['frequencies_hz']):
            frequencies = b''.join(varint(frequency) for frequency in sweep['frequencies_hz'])
            levels = b''.join(struct.pack('<d', level) for level in sweep['levels'])
            spectrum = field(4, field(1, frequencies) + field(2, levels))
            del sweep['frequencies_hz'], sweep['levels']
    raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    return b'\x08\x01\x10' + varint(event.sequence) + field(3, raw) + spectrum


def websocket(payload):
    length = len(payload)
    if length < 126:
        header = bytes((0x82, length))
    elif length < 65536:
        header = b'\x82\x7e' + struct.pack('>H', length)
    else:
        header = b'\x82\x7f' + struct.pack('>Q', length)
    return header + payload


def exact(reader, length):
    value = reader.read(length)
    if len(value) != length:
        raise EOFError
    return value


def install(server):
    base = server.RequestHandlerClass

    class SyntheticBinaryHandler(base):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path != '/api/v1/events/ws':
                return super().do_GET()
            query = parse_qs(url.query, keep_blank_values=True)
            after = query.get('after', [''])[0]
            key = self.headers.get('Sec-WebSocket-Key', '')
            if (set(query) != {'after'} or len(query['after']) != 1
                    or not after.isascii() or not after.isdecimal()
                    or str(int(after)) != after or int(after) > 2**64 - 1
                    or self.headers.get('Sec-WebSocket-Protocol') != PROTOCOL
                    or self.headers.get('Origin') != 'http://127.0.0.1:18772'
                    or self.headers.get('Sec-WebSocket-Version') != '13' or not key):
                self.send_error(400)
                return
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.protocol_version = 'HTTP/1.1'
            self.send_response(101)
            for name, value in [('Upgrade', 'websocket'), ('Connection', 'Upgrade'),
                                ('Sec-WebSocket-Accept', accept), ('Sec-WebSocket-Protocol', PROTOCOL)]:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            self.connection.settimeout(30)
            cursor = int(after)
            notifier = server.session_service.watch_commits()
            try:
                while not server.session_service.event_stream_closed:
                    notifier.clear()
                    event = server.session_service.next_stream_event(after=cursor)
                    if event is None:
                        if notifier.is_set():
                            continue
                        # A real browser waits for the close handshake before
                        # reconnecting. Observe idle close/ping, not only commits.
                        readable, _, _ = select.select([self.connection], [], [], .05)
                        if readable:
                            opcode, length = exact(self.rfile, 2)
                            if not length & 128 or length & 127 > 125:
                                return
                            mask = exact(self.rfile, 4)
                            raw = exact(self.rfile, length & 127)
                            control = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
                            if opcode == 0x88:
                                self.wfile.write(bytes((0x88, len(control))) + control)
                                self.wfile.flush()
                                return
                            if opcode == 0x89:
                                self.wfile.write(bytes((0x8a, len(control))) + control)
                                self.wfile.flush()
                            elif opcode != 0x8a:
                                return
                        continue
                    payload = encode(event)
                    assert len(payload) <= MAX_FRAME
                    self.wfile.write(websocket(payload))
                    self.wfile.flush()
                    opcode, length = exact(self.rfile, 2)
                    if opcode == 0x88:  # the reader deliberately closes its owned connection
                        return
                    if opcode != 0x81 or not length & 128 or not 1 <= length & 127 <= 20:
                        return
                    mask = exact(self.rfile, 4)
                    raw = exact(self.rfile, length & 127)
                    acknowledgement = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
                    if acknowledgement != str(event.sequence).encode('ascii'):
                        return
                    cursor = event.sequence
            except (EOFError, OSError, socket.timeout):
                return
            finally:
                server.session_service.unwatch_commits(notifier)

    server.RequestHandlerClass = SyntheticBinaryHandler
