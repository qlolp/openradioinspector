"""Управление средствами измерений через COM-порт по протоколу SCPI.

В исходном комплексе по COM-порту управляются, в частности, анализаторы
спектра IFR 2394, 2395 и 2397 (Baudrate 9600–115200, Data Len 8, Stop Bits 1,
Parity none, Protocol none) и HP 8560–8565.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig


class SerialScpiInstrument(Instrument):
    """Средство измерений с управлением через COM-порт по SCPI."""

    device_name = "Serial"

    def __init__(
        self,
        port: str = "COM1",
        baudrate: int = 9600,
        timeout_s: float = 5.0,
        read_termination: str = "\n",
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.read_termination = read_termination
        self._serial = None
        self._info = InstrumentInfo(interface=f"serial:{port}")

    def connect(self) -> InstrumentInfo:
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise InstrumentError(
                "Для работы с COM-портом требуется пакет pyserial (pip install pyserial)"
            ) from exc
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.timeout_s,
                bytesize=8,
                parity="N",
                stopbits=1,
            )
        except Exception as exc:
            raise InstrumentError(
                f"Не удалось открыть порт {self.port}: {exc}"
            ) from exc

        response = self._serial.readline().decode("ascii", errors="replace").strip()
        if not response:
            response = self.query("*IDN?")
        parts = [p.strip() for p in response.split(",")]
        self._info = InstrumentInfo(
            manufacturer=parts[0] if parts else "",
            model=parts[1] if len(parts) > 1 else "",
            serial=parts[2] if len(parts) > 2 else "",
            firmware=parts[3] if len(parts) > 3 else "",
            interface=f"serial:{self.port}",
        )
        return self._info

    def disconnect(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def identify(self) -> InstrumentInfo:
        return self._info

    def query(self, command: str) -> str:
        if self._serial is None:
            raise InstrumentError("Прибор не подключён")
        self._serial.write((command + self.read_termination).encode("ascii"))
        return self._serial.readline().decode("ascii", errors="replace").strip()

    def sweep(self, config: SweepConfig) -> Trace:
        if self._serial is None:
            self.connect()
        self.query(f"STAR {config.start_hz:.0f}")
        self.query(f"STOP {config.stop_hz:.0f}")
        self.query(f"RESB {config.resolution_bandwidth_hz:.0f}")
        self.query(f"POIN {config.points}")
        payload = self.query("TRAC:DATA? TRACE1")
        levels = _parse_numeric_payload(payload)
        freqs = np.linspace(config.start_hz, config.stop_hz, max(2, levels.size))
        return Trace(freqs[: levels.size], levels, "dBm")


def _parse_numeric_payload(payload: str) -> np.ndarray:
    """Разбор числового массива из строки ответа прибора."""
    text = (payload or "").strip()
    if not text:
        return np.array([])
    parts = text.replace(";", ",").split(",")
    values: list[float] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            continue
    return np.asarray(values, dtype=float)
