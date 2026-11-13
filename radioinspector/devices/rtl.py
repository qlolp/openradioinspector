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
        try:
            header = self._recv_exact(12)
            if header[:4] != b"RTL0":
                raise InstrumentError("Неверный заголовок rtl_tcp")
        except (OSError, InstrumentError) as exc:
            self.disconnect()
            raise InstrumentError(f"Не удалось прочитать заголовок rtl_tcp: {exc}") from exc
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
        return Trace(freqs, levels, "dBFS")

    def _measure_at(self, frequency_hz: float) -> float:
        if self._socket is None:
            raise InstrumentError("Приёмник rtl_tcp не подключён")
        # rtl_tcp commands are one byte plus a big-endian 32-bit argument.
        # Gain is signed and expressed in tenths of a dB; no start command exists.
        commands = ((0x01, int(frequency_hz)), (0x02, int(self.sample_rate)),
                    (0x03, 1), (0x04, int(round(self.gain_db * 10))))
        self._socket.sendall(b"".join(
            bytes([command]) + (value & 0xffffffff).to_bytes(4, "big")
            for command, value in commands
        ))
        raw = self._recv_exact(FFT_SIZE * 2)
        components = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 127.5) / 127.5
        iq = components[0::2] + 1j * components[1::2]
        # Relative to a full-scale complex sinusoid, not calibrated input dBm.
        power = float(np.mean(np.abs(iq) ** 2))
        return 10.0 * math.log10(max(power, 1e-30))

    def _recv_exact(self, length: int) -> bytes:
        if self._socket is None:
            raise InstrumentError("Приёмник rtl_tcp не подключён")
        buffer = bytearray()
        while len(buffer) < length:
            try:
                chunk = self._socket.recv(length - len(buffer))
            except OSError as exc:
                raise InstrumentError(f"Ошибка чтения rtl_tcp: {exc}") from exc
            if not chunk:
                raise InstrumentError("rtl_tcp закрыл соединение до получения полного блока")
            buffer.extend(chunk)
        return bytes(buffer)

    @staticmethod
    def _rtl_param(value: float) -> list[int]:
        return list(int(value).to_bytes(4, "big"))

    def set_center_frequency(self, frequency_hz: float) -> None:
        self.center_frequency_hz = frequency_hz
