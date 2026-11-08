"""Регрессионный тест для драйвера SoapySDR.

В ``connect()`` направление приёма передаётся константой
``SoapySDR.SOAPY_SDR_RX`` через сохранённый модуль ``self._SoapySDR``.
До исправления использовалось голое имя ``SOAPY_SDR_RX``, которого нет
в области видимости метода, — вызов ``connect()`` падал с ``NameError``.
Тест подменяет библиотеку SoapySDR синтетическим модулем и проверяет,
что константа действительно доходит до методов устройства.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from radioinspector.devices.soapy import SoapyInstrument

FAKE_RX = 0
FAKE_CF32 = 8


class _FakeStream:
    def __init__(self) -> None:
        self.activated = False

    def activate(self) -> None:
        self.activated = True

    def deactivate(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeDevice:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    @classmethod
    def make(cls, args: str) -> "_FakeDevice":
        return cls()

    def setSampleRate(self, direction, channel, rate) -> None:
        self.calls.append(("setSampleRate", direction, channel, rate))

    def setFrequency(self, direction, channel, frequency) -> None:
        self.calls.append(("setFrequency", direction, channel, frequency))

    def setGain(self, direction, channel, gain) -> None:
        self.calls.append(("setGain", direction, channel, gain))

    def setGainMode(self, direction, channel, mode) -> None:
        self.calls.append(("setGainMode", direction, channel, mode))

    def setupStream(self, direction, format_, channels):
        self.calls.append(("setupStream", direction, format_, channels))
        return _FakeStream()

    def getHardwareInfo(self) -> dict:
        return {"origin": "test-origin", "label": "fake-sdr"}

    def unmake(self) -> None:
        pass


def _fake_module() -> types.ModuleType:
    mod = types.ModuleType("SoapySDR")
    mod.SOAPY_SDR_RX = FAKE_RX
    mod.SOAPY_SDR_CF32 = FAKE_CF32
    mod.Device = _FakeDevice
    return mod


class SoapyConnectTests(unittest.TestCase):
    def test_connect_passes_rx_direction_constant(self) -> None:
        inst = SoapyInstrument()
        with mock.patch.dict(sys.modules, {"SoapySDR": _fake_module()}):
            info = inst.connect()

        self.assertIsNotNone(inst._device)
        # Второй элемент каждого сохранённого вызова — направление приёма.
        directions = [call[1] for call in inst._device.calls]
        self.assertTrue(directions, "устройство должно получить команды")
        self.assertTrue(all(direction == FAKE_RX for direction in directions))
        self.assertTrue(inst._stream.activated)
        self.assertEqual(info.model, "fake-sdr")


if __name__ == "__main__":
    unittest.main()