"""Hardware-free Soapy Python API regression tests, not device certification."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from radioinspector.devices.base import InstrumentError, SweepConfig
from radioinspector.devices.soapy import SoapyInstrument

FAKE_RX = 17  # Deliberately not zero: catches hard-coded direction values.
FAKE_CF32 = "CF32"


class _FakeDevice:
    """Stream is an opaque handle; all lifecycle calls belong to Device."""

    def __init__(self):
        self.calls = []
        self.stream = object()
        self.fail = None
        self.activation_status = 0
        self.deactivation_status = 0
        self.gain_mode = True

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail == name:
            raise RuntimeError(name)

    def setSampleRate(self, direction, channel, rate):
        self._call("setSampleRate", direction, channel, rate)

    def setFrequency(self, direction, channel, frequency):
        self._call("setFrequency", direction, channel, frequency)

    def setGain(self, direction, channel, gain):
        self._call("setGain", direction, channel, gain)

    def hasGainMode(self, direction, channel):
        self._call("hasGainMode", direction, channel)
        return self.gain_mode

    def setGainMode(self, direction, channel, mode):
        self._call("setGainMode", direction, channel, mode)

    def setupStream(self, direction, format_, channels):
        self._call("setupStream", direction, format_, channels)
        return self.stream

    def activateStream(self, stream):
        assert stream is self.stream
        self._call("activateStream", stream)
        return self.activation_status

    def deactivateStream(self, stream):
        assert stream is self.stream
        self._call("deactivateStream", stream)
        return self.deactivation_status

    def closeStream(self, stream):
        assert stream is self.stream
        self._call("closeStream", stream)

    def close(self):
        self._call("close")

    def getHardwareInfo(self):
        self._call("getHardwareInfo")
        return {"origin": "test-origin", "label": "fake-sdr"}


class SoapyConnectTests(unittest.TestCase):
    def setUp(self):
        self.device = _FakeDevice()
        module = types.ModuleType("SoapySDR")
        module.SOAPY_SDR_RX = FAKE_RX
        module.SOAPY_SDR_CF32 = FAKE_CF32
        self.factory = mock.Mock(return_value=self.device)
        module.Device = self.factory
        patch = mock.patch.dict(sys.modules, {"SoapySDR": module})
        patch.start()
        self.addCleanup(patch.stop)
        self.inst = SoapyInstrument("driver=fake")
        self.addCleanup(self.inst.disconnect)

    def names(self):
        return [call[0] for call in self.device.calls]

    def test_connect_passes_rx_direction_constant(self):
        info = self.inst.connect()
        self.factory.assert_called_once_with("driver=fake")
        directional = [c for c in self.device.calls if c[0].startswith("set") or c[0] == "hasGainMode"]
        self.assertTrue(directional)
        self.assertTrue(all(c[1] == FAKE_RX for c in directional))
        self.assertIn(("setupStream", FAKE_RX, FAKE_CF32, [0]), self.device.calls)
        self.assertIn(("setGainMode", FAKE_RX, 0, False), self.device.calls)
        self.assertLess(self.names().index("setGainMode"), self.names().index("setGain"))
        self.assertEqual(info.model, "fake-sdr")
        self.assertTrue(self.inst._active)

    def test_connect_and_disconnect_are_idempotent(self):
        self.assertIs(self.inst.connect(), self.inst.connect())
        self.factory.assert_called_once()
        self.inst.disconnect()
        self.inst.disconnect()
        for method in ("activateStream", "deactivateStream", "closeStream", "close"):
            self.assertEqual(self.names().count(method), 1)

    def test_manual_gain_without_agc_support(self):
        self.device.gain_mode = False
        self.inst.connect()
        self.assertNotIn("setGainMode", self.names())
        self.assertIn("setGain", self.names())

    def test_connect_failure_cleans_partial_resources(self):
        for failure in ("setSampleRate", "setFrequency", "hasGainMode", "setGainMode", "setGain", "setupStream", "activateStream", "getHardwareInfo"):
            with self.subTest(failure=failure):
                self.device.calls.clear()
                self.device.fail = failure
                with self.assertRaises(InstrumentError):
                    self.inst.connect()
                self.assertIsNone(self.inst._device)
                self.assertIsNone(self.inst._stream)
                self.assertEqual(self.names().count("close"), 1)
                self.assertEqual("closeStream" in self.names(), failure in ("activateStream", "getHardwareInfo"))
                self.assertEqual("deactivateStream" in self.names(), failure == "getHardwareInfo")

    def test_activation_error_code_closes_stream(self):
        self.device.activation_status = -1
        with self.assertRaisesRegex(InstrumentError, "activateStream.*-1"):
            self.inst.connect()
        self.assertIn("closeStream", self.names())
        self.assertIn("close", self.names())
        self.assertNotIn("deactivateStream", self.names())

    def test_cleanup_continues_after_errors(self):
        for failure in ("deactivateStream", "closeStream", "close"):
            with self.subTest(failure=failure):
                self.device.fail = None
                self.inst.connect()
                self.device.calls.clear()
                self.device.fail = failure
                self.inst.disconnect()
                self.assertEqual(self.names(), ["deactivateStream", "closeStream", "close"])
                self.assertIsNone(self.inst._device)
                self.assertFalse(self.inst._active)

    def test_deactivation_error_code_still_releases_resources(self):
        self.inst.connect()
        self.device.calls.clear()
        self.device.deactivation_status = -1
        self.inst.disconnect()
        self.assertEqual(self.names(), ["deactivateStream", "closeStream", "close"])
        self.assertIsNone(self.inst._device)

    def test_failed_connect_can_retry(self):
        self.device.fail = "setGain"
        with self.assertRaises(InstrumentError):
            self.inst.connect()
        self.device.fail = None
        self.inst.connect()
        self.assertTrue(self.inst._active)

    def test_missing_device_and_stream(self):
        self.factory.return_value = None
        with self.assertRaises(InstrumentError):
            self.inst.connect()
        self.factory.return_value = self.device
        self.device.stream = None
        with self.assertRaises(InstrumentError):
            self.inst.connect()
        self.assertIn("close", self.names())
        self.assertNotIn("activateStream", self.names())

    def test_tune_failure_keeps_last_frequency_and_disconnects(self):
        self.inst.connect()
        original = self.inst.center_frequency_hz
        self.device.fail = "setFrequency"
        with self.assertRaisesRegex(InstrumentError, "перестройки"):
            self.inst.set_center_frequency(123e6)
        self.assertEqual(self.inst.center_frequency_hz, original)
        self.assertIsNone(self.inst._device)
        self.assertIn("close", self.names())

    def test_tune_disconnected_and_connected(self):
        self.inst.set_center_frequency(123e6)
        self.factory.assert_not_called()
        self.inst.connect()
        self.assertIn(("setFrequency", FAKE_RX, 0, 123e6), self.device.calls)
        self.inst.set_center_frequency(124e6)
        self.assertEqual(self.inst.center_frequency_hz, 124e6)
        self.assertIn(("setFrequency", FAKE_RX, 0, 124e6), self.device.calls)

    def test_sweep_fails_before_connect_tune_or_read(self):
        config = SweepConfig(100e6, 101e6, points=2)
        with self.assertRaisesRegex(InstrumentError, "калибровочного"):
            self.inst.sweep(config)
        self.factory.assert_not_called()
        self.inst.connect()
        self.device.calls.clear()
        with self.assertRaisesRegex(InstrumentError, "dBm"):
            self.inst.sweep(config)
        self.assertEqual(self.device.calls, [])

    def test_missing_module_and_constructor_error_are_instrument_errors(self):
        with mock.patch.dict(sys.modules, {"SoapySDR": None}):
            with self.assertRaises(InstrumentError):
                self.inst.connect()
        self.factory.side_effect = RuntimeError("constructor")
        with self.assertRaisesRegex(InstrumentError, "constructor"):
            self.inst.connect()
        self.assertIsNone(self.inst._device)


if __name__ == "__main__":
    unittest.main()
