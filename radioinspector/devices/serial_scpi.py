"""Управление средствами измерений через COM-порт по протоколу SCPI.

В исходном комплексе по COM-порту управляются, в частности, анализаторы
спектра IFR 2394, 2395 и 2397 (Baudrate 9600–115200, Data Len 8, Stop Bits 1,
Parity none, Protocol none) и HP 8560–8565.
"""

from __future__ import annotations

from typing import Optional
import time

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig
from .scpi_data import MAX_RESPONSE_BYTES, ScpiDataError, parse_ascii, retain_quality_issue, validate_config


class SerialScpiInstrument(Instrument):
    """Средство измерений с управлением через COM-порт по SCPI."""

    device_name = "Serial"

    def __init__(
        self,
        port: str = "COM1",
        baudrate: int = 9600,
        timeout_s: float = 5.0,
        read_termination: str = "\n",
        diagnostic_directory=None,
        max_response_bytes=MAX_RESPONSE_BYTES,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.read_termination = read_termination
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError('Invalid SCPI response limit')
        if not read_termination or not read_termination.isascii() or len(read_termination) > 16:
            raise ValueError('Invalid SCPI termination')
        self.max_response_bytes = max_response_bytes
        self._serial = None
        self._info = InstrumentInfo(interface=f"serial:{port}")
        self.diagnostic_directory = diagnostic_directory
        self.last_quality_issue = None

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

        response = self._read_response(allow_empty=True).strip()
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
        return self._read_response().strip()

    def _read_response(self, *, allow_empty=False):
        peer = self._serial
        previous_timeout = peer.timeout
        deadline = time.monotonic() + self.timeout_s
        termination = self.read_termination.encode('ascii')
        response = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScpiDataError('Serial SCPI response deadline exceeded', response)
                peer.timeout = remaining
                waiting = max(1, peer.in_waiting)
                data = peer.read(min(waiting, 65536, self.max_response_bytes + 1 - len(response)))
                response.extend(data)
                if len(response) > self.max_response_bytes:
                    raise ScpiDataError('Serial SCPI response size limit exceeded', response)
                end = response.find(termination)
                if end >= 0:
                    if end + len(termination) != len(response):
                        raise ScpiDataError('Serial SCPI ambiguous response framing', response)
                    try: return response.decode('ascii', errors='strict')
                    except UnicodeError as exc:
                        raise ScpiDataError('Serial SCPI non-ASCII response', response) from exc
                if not data:
                    if allow_empty and not response: return ''
                    raise ScpiDataError('Serial SCPI truncated response', response)
        except ScpiDataError:
            self.disconnect()
            raise
        except OSError as exc:
            self.disconnect()
            raise ScpiDataError('Serial SCPI response I/O failure', response) from exc
        finally:
            if self._serial is peer: peer.timeout = previous_timeout

    def sweep(self, config: SweepConfig) -> Trace:
        validate_config(config)
        if self._serial is None:
            self.connect()
        try:
            self.query(f"STAR {config.start_hz:.0f}")
            self.query(f"STOP {config.stop_hz:.0f}")
            self.query(f"RESB {config.resolution_bandwidth_hz:.0f}")
            self.query(f"POIN {config.points}")
            payload = self.query("TRAC:DATA? TRACE1")
            levels = parse_ascii(payload, expected_points=config.points, semicolons=True)
        except ScpiDataError as error:
            error.diagnostic['expected_points'] = config.points
            retain_quality_issue(self, error, config)
            raise
        freqs = np.linspace(config.start_hz, config.stop_hz, config.points)
        return Trace(freqs, levels, "dBm")


def _parse_numeric_payload(payload: str) -> np.ndarray:
    """Разбор числового массива из строки ответа прибора."""
    return parse_ascii(payload, semicolons=True)
