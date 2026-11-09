"""Pinned Ultra firmware framing, never a physical-device fixture."""
import importlib
import struct
import threading
import unittest
import zlib
from collections import deque

from radioinspector.serial_instruments import _SerialFraming
from radioinspector.device_workers import WorkerError


class Wire(_SerialFraming):
    def __init__(self, chunks):
        self.path = 'synthetic://ultra'
        self._stash = bytearray()
        self._cancelled = threading.Event()
        self.chunks = deque(chunks)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def _read_once(self, timeout, label):
        if not self.chunks:
            raise OSError('synthetic EOF')
        data = self.chunks.popleft()
        self._stash.extend(data)
        return data


class ProtocolTests(unittest.TestCase):
    def test_unbounded_text_reply_cannot_exhaust_transport_memory(self):
        wire = Wire([b'x'*(1024*1024+1) + b'\r\nch> '])
        with self.assertRaises(WorkerError):
            wire.read_until_quiet(.01, 1, 'synthetic flood')

    def module(self):
        try:
            return importlib.import_module('radioinspector.tinysa.protocol')
        except ImportError as exc:
            self.fail(str(exc))

    def test_binary_prompt_bytes_are_pixels_not_a_terminator(self):
        p = self.module()
        wire = Wire([b'capt', b'ure\r\nch>', b' 1234', b'ch', b'> '])
        self.assertEqual(p.read_binary(wire, 'capture', size=8), b'ch> 1234')
        self.assertEqual(wire.sent, [b'capture\r'])
        self.assertFalse(wire._stash)

    def test_sd_length_checked_before_payload_allocation(self):
        p = self.module()
        wire = Wire([b'sd_read A.CSV\r\n' + struct.pack('<I', 9_000_000)])
        with self.assertRaisesRegex(ValueError, 'size|размер'):
            p.read_binary(wire, 'sd_read A.CSV')
        self.assertFalse(wire.chunks)

    def test_sd_bytes_and_echo_are_exact(self):
        p = self.module()
        wire = Wire([b'sd_read A.CSV\r\n\x05\0', b'\0\0a\0ch>ch> '])
        self.assertEqual(p.read_binary(wire, 'sd_read A.CSV'), b'a\0ch>')
        with self.assertRaises(ValueError):
            p.read_binary(Wire([b'other\r\n123ch> ']), 'capture', size=3)

    def test_truncated_frame_or_duplicate_prompt_never_confirms(self):
        p = self.module()
        for data in [b'capture\r\n12', b'capture\r\n123nope', b'capture\r\n123ch> ch> ']:
            with self.subTest(data=data), self.assertRaises((ValueError, OSError)):
                p.read_binary(Wire([data]), 'capture', size=3)

    def test_scanraw_grid_excludes_stop_and_reads_little_endian(self):
        p = self.module()
        raw = b'{x\x80\x0ax\x00\x0bx\x80\x0bx\x00\x0c}'
        hz, dbm = p.decode_scanraw(raw, 100, 111, 4, zero_db=174)
        self.assertEqual(hz, (100, 102, 104, 106))
        self.assertEqual(dbm, (-90.0, -86.0, -82.0, -78.0))

    def test_scanraw_zero_is_part_of_decode_not_an_assumed_constant(self):
        p = self.module()
        self.assertEqual(p.decode_scanraw(b'{x\x00\x10}', 100, 100, 1, zero_db=128), ((100,), (0.0,)))
        for data in [b'{x\0\0}', b'{q\0\0x\0\0}', b'{x\0\0x\0\0}x']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                p.decode_scanraw(data, 100, 200, 2, zero_db=174)

    def test_rgb565_big_endian_capture_produces_exact_png_colours(self):
        p = self.module()
        png = p.capture_png(b'\xf8\0\x07\xe0\0\x1f', width=3, height=1)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        position = 8
        data = b''
        while position < len(png):
            length, = struct.unpack('>I', png[position:position+4])
            tag = png[position+4:position+8]
            payload = png[position+8:position+8+length]
            if tag == b'IDAT':
                data += payload
            position += length+12
        self.assertEqual(zlib.decompress(data), b'\0\xff\0\0\0\xff\0\0\0\xff')

    def test_oversized_capture_and_command_injection_rejected_before_send(self):
        p = self.module()
        wire = Wire([])
        for command, size in [('capture\rreset', 1), ('capture', 9_000_000), ('capture', True)]:
            with self.assertRaises(ValueError):
                p.read_binary(wire, command, size=size)
        self.assertFalse(wire.sent)
        with self.assertRaises(ValueError):
            p.capture_png(b'', width=1000000, height=1000000)
