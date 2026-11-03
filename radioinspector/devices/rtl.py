"""Подключение SDR-приёмников через сервис rtl_tcp.

Сервис rtl_tcp предоставляет доступ к приёмнику RTL-SDR по TCP. Управление
ведётся бинарными командами: установка частоты, частоты дискретизации, режима
усиления и коэффициента усиления, запуск тюнера. Данные возвращаются
8-битными отсчётами без знака.
"""

from __future__ import annotations

import math
import socket
from typing import Optional

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig

#: Размер блока БПФ.
FFT_SIZE = 1024


class RtlTcpInstrument(Instrument):
    """SDR-приёмник, доступный через сервис rtl_tcp."""

    device_name = "RTL-TCP"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1234,
        sample_rate: float = 2.4e6,
        center_frequency_hz: float = 100.0e6,
        gain_db: int = 20,
    ) -> None:
        self.host = host
        self.port = port
        self.sample_rate = sample_rate
        self.center_frequency_hz = center_frequency_hz
        self.gain_db = gain_db
        self._socket: Optional[socket.socket] = None
        self._info = InstrumentInfo(interface=f"rtl_tcp:{host}:{port}")

    def connect(self) -> InstrumentInfo:
        try:
            self._socket = socket.create_connection((self.host, self.port), 10.0)
        except OSError as exc:
            raise InstrumentError(
                f"Не удалось подключиться к rtl_tcp {self.host}:{self.port}: {exc}"
            ) from exc
        self._socket.settimeout(10.0)
        self._info = InstrumentInfo(
            manufacturer="Realtek",
            model="RTL-SDR",
            serial="",
            firmware="rtl_tcp",
            interface=f"rtl_tcp:{self.host}:{self.port}",
        )
        return self._info

    def disconnect(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def identify(self) -> InstrumentInfo:
        return self._info

    def sweep(self, config: SweepConfig) -> Trace:
        """Панорама диапазона частот перестройкой приёмника."""
        if self._socket is None:
            self.connect()
        points = max(2, int(config.points))
        freqs = np.linspace(config.start_hz, config.stop_hz, points)
        levels = np.zeros(points)
        for index, freq in enumerate(freqs):
            levels[index] = self._measure_at(float(freq))
        return Trace(freqs, levels, "dBm")

    def _measure_at(self, frequency_hz: float) -> float:
        assert self._socket is not None
        # set frequency, set sample rate, set gain mode, set gain, start tuner
        self._socket.sendall(
            bytes(
                [
                    0x01,
                    *self._rtl_param(frequency_hz),
                    0x02,
                    *self._rtl_param(self.sample_rate),
                    0x03,
                    0x01,
                    0x04,
                    int(self.gain_db) & 0xFF,
                    0x05,
                ]
            )
        )
        buffer = bytearray()
        needed = FFT_SIZE * 2
        while len(buffer) < needed:
            chunk = self._socket.recv(needed - len(buffer))
            if not chunk:
                break
            buffer.extend(chunk)
        samples = (
            np.frombuffer(bytes(buffer[:needed]), dtype=np.uint8).astype(np.float32) - 127.5
        ) / 127.5
        if samples.size == 0:
            return float("-inf")
        spectrum = np.abs(np.fft.fft(samples)) ** 2
        power = float(np.mean(spectrum))
        return 10.0 * math.log10(max(power, 1e-30)) - 10.0 * math.log10(FFT_SIZE)

    @staticmethod
    def _rtl_param(value: float) -> list[int]:
        return list(int(value).to_bytes(4, "big"))

    def set_center_frequency(self, frequency_hz: float) -> None:
        self.center_frequency_hz = frequency_hz
