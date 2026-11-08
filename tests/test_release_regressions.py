"""Regression cases found during the release audit; no hardware required."""
import math
import struct
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock

import numpy as np

from radioinspector.devices.base import InstrumentError, SweepConfig
from radioinspector.devices.rtl import RtlTcpInstrument, FFT_SIZE
from radioinspector.devices.soapy import SoapyInstrument
from radioinspector.measurements import (bandwidth_at_level, center_frequency_centroid,
                                        complex_center_frequency)
from radioinspector.occupancy import busy_hour_occupancy, occupancy_from_store
from radioinspector.panorama import PanoramaLibrary, PanoramaStore, ScanSettings
from radioinspector.trace import Trace


class TestStatisticsRegressions(unittest.TestCase):
    def test_hourly_values_are_not_global_mean(self):
        with tempfile.TemporaryDirectory() as root, PanoramaStore(root + '/x.pan') as store:
            for hour, level in [(10, -100), (11, -20)]:
                store.append(Trace([1, 2, 3], [level] * 3),
                             datetime(2026, 1, 1, hour, tzinfo=timezone.utc))
            self.assertEqual(busy_hour_occupancy(store, -80), {10: 0.0, 11: 1.0})

    def test_observation_seconds_use_timestamps(self):
        with tempfile.TemporaryDirectory() as root, PanoramaStore(root + '/x.pan') as store:
            for hour in [10, 11]:
                store.append(Trace([1, 2, 3], [-90] * 3),
                             datetime(2026, 1, 1, hour, tzinfo=timezone.utc))
            self.assertEqual(occupancy_from_store(store, -80).observation_time_s, 3600.0)

    def test_single_scan_has_no_elapsed_time(self):
        with tempfile.TemporaryDirectory() as root, PanoramaStore(root + '/x.pan') as store:
            store.append(Trace([1, 2, 3], [-90] * 3))
            self.assertEqual(occupancy_from_store(store, -80).observation_time_s, 0)


class TestArchiveRegressions(unittest.TestCase):
    def test_colliding_filename_cannot_overwrite_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            library = PanoramaLibrary(root)
            first = ScanSettings(start_hz=100e6, stop_hz=101e6, points=3)
            second = ScanSettings(start_hz=100e6 + 10, stop_hz=101e6 + 10, points=3)
            store, _ = library.open_for(first)
            path = store.path
            store.append(Trace([100e6, 100.5e6, 101e6], [-90]*3))
            store.close()
            # Refuse collision instead of silently mixing incompatible scans.
            with self.assertRaisesRegex(ValueError, 'настрой|settings'):
                other, _ = library.open_for(second)
                other.close()
            with PanoramaStore(path) as check:
                self.assertEqual(check.read_settings().key(), first.key())
                self.assertEqual(check.count, 1)
            same, existed = library.open_for(first)
            self.assertTrue(existed)
            same.close()

    def test_constructor_also_refuses_incompatible_settings(self):
        with tempfile.TemporaryDirectory() as root:
            with PanoramaStore(root + '/x.pan', ScanSettings(start_hz=1)):
                pass
            with self.assertRaises(ValueError):
                with PanoramaStore(root + '/x.pan', ScanSettings(start_hz=2)):
                    pass


class TestFrequencyRegressions(unittest.TestCase):
    def test_symmetric_gaussian_has_no_half_bin_bias(self):
        for step in [100, 1000]:
            with self.subTest(step=step):
                x = np.arange(-200, 201)
                trace = Trace(100e6 + x * step, -100 + 80 * np.exp(-0.5*(x/20)**2))
                self.assertAlmostEqual(center_frequency_centroid(trace).value, 100e6, places=5)

    def test_complex_frequency_respects_level_below_thirty(self):
        x = np.arange(-200, 201)
        trace = Trace(100e6 + x * 1000, -100 + 80 * np.exp(-0.5*(x/20)**2))
        result = complex_center_frequency(trace, n_db=10)
        self.assertAlmostEqual(result.bandwidth_hz, bandwidth_at_level(trace, n_db=10).bandwidth_hz)


class BufferSocket:
    """Fragmented rtl_tcp stream: handshake followed by unsigned interleaved IQ."""
    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = bytearray()
        self.closed = False
    def settimeout(self, _timeout): pass
    def sendall(self, data): self.sent.extend(data)
    def recv(self, size):
        count = min(size, 137, len(self.data))
        result = bytes(self.data[:count])
        del self.data[:count]
        return result
    def close(self): self.closed = True


class TestRtlRegressions(unittest.TestCase):
    def test_commands_are_five_byte_frames(self):
        sock = BufferSocket(bytes([255, 127]) * FFT_SIZE)
        instrument = RtlTcpInstrument(gain_db=-2)
        instrument._socket = sock
        instrument._measure_at(100e6)
        expected = b''.join(struct.pack('>Bi', c, p) for c, p in
                            [(1, 100000000), (2, 2400000), (3, 1), (4, -20)])
        self.assertEqual(bytes(sock.sent), expected)

    def test_handshake_is_consumed_and_iq_is_complex(self):
        header = struct.pack('>4sII', b'RTL0', 5, 29)
        sock = BufferSocket(header + bytes([255, 127]) * FFT_SIZE)
        instrument = RtlTcpInstrument()
        with patch('socket.create_connection', return_value=sock):
            instrument.connect()
        level = instrument._measure_at(100e6)
        expected = 10 * math.log10(1 + (0.5/127.5)**2)
        self.assertAlmostEqual(level, expected, places=5)
        self.assertEqual(sock.data, b'')
        trace_sock = BufferSocket(bytes([255, 127]) * FFT_SIZE * 2)
        instrument._socket = trace_sock
        trace = instrument.sweep(SweepConfig(1e6, 2e6, points=2))
        self.assertEqual(trace.unit, 'dBFS')

    def test_short_or_invalid_handshake_closes_socket(self):
        for data in [b'RTL', struct.pack('>4sII', b'BAD!', 0, 0)]:
            sock = BufferSocket(data)
            instrument = RtlTcpInstrument()
            with patch('socket.create_connection', return_value=sock):
                with self.assertRaises(InstrumentError): instrument.connect()
            self.assertTrue(sock.closed)
            self.assertIsNone(instrument._socket)

    def test_short_iq_block_is_error(self):
        instrument = RtlTcpInstrument()
        instrument._socket = BufferSocket(b'\x7f' * 10)
        with self.assertRaises(InstrumentError): instrument._measure_at(100e6)




if __name__ == '__main__': unittest.main()
