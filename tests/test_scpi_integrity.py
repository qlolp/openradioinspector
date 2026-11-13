"""Malformed analyzer responses never become apparently valid spectra."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from radioinspector.devices.base import InstrumentError, SweepConfig
from radioinspector.devices.scpi import ScpiConnection, ScpiInstrument, Vxi11Instrument
from radioinspector.devices.serial_scpi import _parse_numeric_payload, SerialScpiInstrument


class Transcript:
    def __init__(self, response):
        self.response = response
        self.commands = []
    def write(self, command): self.commands.append(command)
    def read(self): return '1\n'
    def query(self, command): self.commands.append(command); return self.response
    def close(self): pass


class RawSocket:
    def __init__(self, raw=b''): self.raw = raw; self.requests = []; self.timeout = 5; self.closed = False
    def gettimeout(self): return self.timeout
    def settimeout(self, value): self.timeout = value
    def recv(self, count):
        self.requests.append(count); data = self.raw[:count]; self.raw = self.raw[count:]; return data
    def close(self): self.closed = True


class VxiTranscript(Transcript):
    link = 1
    def __init__(self, response):
        super().__init__(response); self.client = self; self.sock = RawSocket(); self.read_requests = []
    def device_read(self, link, count, timeout, lock_timeout, flags, term):
        self.read_requests.append(count)
        payload = self.response if self.commands[-1].startswith('TRAC:DATA?') else '1\n'
        return 0, 4, payload.encode('ascii')


class ScpiIntegrityTests(unittest.TestCase):
    def test_bad_tokens_nonfinite_empty_and_declared_count_are_rejected(self):
        invalid = ('', '-80,, -50', '-80,broken,-50', '-80,nan,-50', '-80,inf,-50',
                   'CURVE 4,-80,-50', 'TRACE 2,-80,-50,-40', '-80,-50,',
                   '1_000,2', '#210-80,-50', 'garbage 2,-80,-50')
        for parser in (ScpiInstrument._parse_curve, _parse_numeric_payload):
            for payload in invalid:
                with self.subTest(parser=parser, payload=payload), self.assertRaises(InstrumentError):
                    parser(payload)

    def test_valid_ascii_formats(self):
        for payload in ('-80, -50, -40\r\n', 'CURVE 3,-80,-50,-40', 'TRACE 3 -80,-50,-40'):
            np.testing.assert_array_equal(ScpiInstrument._parse_curve(payload), [-80, -50, -40])
        np.testing.assert_array_equal(_parse_numeric_payload('-80;-50;-40'), [-80, -50, -40])

    def test_every_transport_rejects_count_mismatch_and_retains_quality_artifact(self):
        config = SweepConfig(100, 400, points=4)
        for kind in ('tcp', 'vxi11', 'serial'):
            with tempfile.TemporaryDirectory() as directory, self.subTest(kind=kind):
                transcript = Transcript('-80,-50,-40')
                if kind == 'tcp':
                    instrument = ScpiInstrument('fixture', connection=transcript)
                    instrument._connected = True
                elif kind == 'vxi11':
                    transcript = VxiTranscript('-80,-50,-40')
                    instrument = Vxi11Instrument('fixture'); instrument._instr = transcript
                else:
                    instrument = SerialScpiInstrument(); instrument._serial = object()
                    instrument.query = transcript.query
                instrument.diagnostic_directory = directory
                with self.assertRaises(InstrumentError): instrument.sweep(config)
                issue = instrument.last_quality_issue
                self.assertEqual(issue['kind'], 'scpi_invalid_response')
                self.assertEqual(issue['expected_points'], 4)
                files = list(Path(directory).glob('*.json'))
                self.assertEqual(len(files), 1)
                stored = json.loads(files[0].read_text())
                self.assertEqual(stored['raw_response'], '-80,-50,-40')
                self.assertEqual(stored['diagnostic']['sha256'], issue['sha256'])

    def test_success_preserves_requested_grid_and_first_last_samples(self):
        instrument = ScpiInstrument('fixture', connection=Transcript('-80,-50,-40,-60'))
        instrument._connected = True
        trace = instrument.sweep(SweepConfig(100, 400, points=4))
        np.testing.assert_array_equal(trace.frequencies, [100, 200, 300, 400])
        np.testing.assert_array_equal(trace.levels, [-80, -50, -40, -60])

    def test_socket_truncated_line_is_rejected(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        connection = ScpiConnection('fixture'); connection._socket = left
        right.sendall(b'-80,-50'); right.shutdown(socket.SHUT_WR)
        with self.assertRaises(InstrumentError): connection.read()

    def test_diagnostic_disk_failure_preserves_original_quality_error(self):
        from radioinspector.devices.scpi_data import ScpiDataError
        instrument = ScpiInstrument('fixture', connection=Transcript('-80,bad,-40'), diagnostic_directory='unused')
        instrument._connected = True
        with patch.object(Path, 'mkdir', side_effect=PermissionError('fixture denial')):
            with self.assertRaises(ScpiDataError): instrument.sweep(SweepConfig(100, 300, points=3))
        self.assertEqual(instrument.last_quality_issue['kind'], 'scpi_invalid_response')
        self.assertEqual(instrument.last_quality_issue['artifact_error']['type'], 'PermissionError')

    def test_diagnostic_artifact_preserves_non_ascii_original_bytes(self):
        from radioinspector.devices.scpi_data import ScpiDataError, retain_quality_issue
        raw = b'literal\\xff,\xff\n'
        with tempfile.TemporaryDirectory() as directory:
            instrument = ScpiInstrument('fixture', connection=Transcript(''), diagnostic_directory=directory)
            retain_quality_issue(instrument, ScpiDataError('non-ASCII fixture', raw), SweepConfig(100, 200))
            record = json.loads(Path(instrument.last_quality_issue['artifact_path']).read_text())
            self.assertEqual((Path(directory) / record['raw_artifact']).read_bytes(), raw)

    def test_serial_partial_size_and_encoding_fail_before_spectrum(self):
        class SerialPeer:
            timeout = 1
            def __init__(self, data): self.data = data; self.closed = False; self.requests = []
            @property
            def in_waiting(self): return len(self.data)
            def write(self, _): pass
            def readline(self, *args): return self.data  # Old unbounded path.
            def read(self, count):
                self.requests.append(count); result = self.data[:count]; self.data = self.data[count:]; return result
            def close(self): self.closed = True
        for raw in (b'-80,-50,-40', b'-80,\xff,-40\n', b'1'*17+b'\n'):
            instrument = SerialScpiInstrument(); peer = SerialPeer(raw); instrument._serial = peer
            instrument.max_response_bytes = 16
            with self.subTest(raw=raw), self.assertRaises(InstrumentError): instrument.query('TRAC:DATA? TRACE1')
            self.assertTrue(peer.closed)
            self.assertTrue(all(count <= 17 for count in peer.requests))

    def test_vxi_limits_receive_requests_and_requires_complete_END(self):
        for raw, reason in ((b'1'*17, 4), (b'-80,-50', 1), (b'-80,\xff', 4)):
            instrument = Vxi11Instrument('fixture', max_response_bytes=16)
            peer = VxiTranscript('unused'); instrument._instr = peer
            sent = []
            def device_read(link, count, timeout, lock_timeout, flags, term):
                sent.append(count)
                return (0, reason, raw) if len(sent) == 1 else (0, 0, b'')
            peer.device_read = device_read
            with self.subTest(raw=raw), self.assertRaises(InstrumentError): instrument._read_response()
            self.assertTrue(all(count <= 17 for count in sent))
            self.assertTrue(peer.sock.closed)

    def test_vxi_rpc_fragment_is_bounded_before_library_allocation(self):
        import time
        from radioinspector.devices.scpi_data import BoundedVxiSocket
        raw = RawSocket(b'x'*100)
        bounded = BoundedVxiSocket(raw, time.monotonic() + 5, 16)
        with self.assertRaises(InstrumentError): bounded.recv(100_000_000)
        self.assertEqual(raw.requests, [17])

    def test_response_limit_is_enforced_without_reading_unbounded_stream(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        connection = ScpiConnection('fixture', max_response_bytes=16); connection._socket = left
        right.sendall(b'1' * 17 + b'\n')
        with self.assertRaises(InstrumentError): connection.read()

    def test_termination_split_across_socket_reads(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        connection = ScpiConnection('fixture', read_termination='\r\n'); connection._socket = left
        right.sendall(b'-80,-50\r\n')
        self.assertEqual(connection.read(), '-80,-50\r\n')
