"""SCPI-драйверы для анализаторов спектра и измерительных приёмников.

Поддерживаются транспорты, описанные в руководстве по подключению приборов:

* **SCPI поверх TCP-сокета** (порт 5025) — основной транспорт сетевых
  анализаторов Agilent/Keysight, R&S, Tektronix, SignalHound;
* **VXI-11 / LXI** — управление приборами по сети средствами внешней
  библиотеки ``python-vxi11`` (устанавливается отдельно);
* **COM-порт** — приборы с последовательным интерфейсом (IFR 2394/2395/2397,
  HP 8560–8565 и ряд других).

Приём данных выполняется в формате ASCii с разбором блока ``CURVE`` либо
``TRACE``; для приборов, отдающих данные блоками ``BEGIN``/``END``, разбираются
оба варианта.
"""

from __future__ import annotations

import socket
import time
from typing import Optional

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig
from .scpi_data import BoundedVxiSocket, MAX_RESPONSE_BYTES, ScpiDataError, parse_ascii, retain_quality_issue, validate_config


class ScpiConnection:
    """Транспорт SCPI поверх TCP-сокета."""

    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 10.0,
        read_termination: str = "\n",
        write_termination: str = "\n",
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.read_termination = read_termination
        self.write_termination = write_termination
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError('Invalid SCPI response limit')
        if not read_termination or not read_termination.isascii() or len(read_termination) > 16:
            raise ValueError('Invalid SCPI termination')
        self.max_response_bytes = max_response_bytes
        self._socket: Optional[socket.socket] = None

    def open(self) -> None:
        try:
            self._socket = socket.create_connection((self.host, self.port), self.timeout_s)
        except OSError as exc:
            raise InstrumentError(
                f"Не удалось подключиться к {self.host}:{self.port}: {exc}"
            ) from exc
        self._socket.settimeout(self.timeout_s)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def write(self, command: str) -> None:
        if self._socket is None:
            raise InstrumentError("Соединение не установлено")
        self._socket.sendall(command.encode("ascii") + self.write_termination.encode())

    def read(self) -> str:
        if self._socket is None:
            raise InstrumentError("Соединение не установлено")
        response = bytearray()
        deadline = time.monotonic() + self.timeout_s
        termination = self.read_termination.encode('ascii')
        peer = self._socket
        previous_timeout = peer.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScpiDataError('SCPI response deadline exceeded', response)
                peer.settimeout(remaining)
                data = peer.recv(min(65536, self.max_response_bytes + 1 - len(response)))
                response.extend(data)
                if len(response) > self.max_response_bytes:
                    raise ScpiDataError('SCPI response size limit exceeded', response)
                end = response.find(termination)
                if end >= 0:
                    if end + len(termination) != len(response):
                        raise ScpiDataError('SCPI ambiguous response framing', response)
                    try:
                        return response.decode('ascii', errors='strict')
                    except UnicodeError as exc:
                        raise ScpiDataError('SCPI non-ASCII response', response) from exc
                if not data:
                    raise ScpiDataError('SCPI truncated response', response)
        except (socket.timeout, OSError) as exc:
            self.close()
            raise ScpiDataError('SCPI response I/O failure', response) from exc
        except ScpiDataError:
            self.close()
            raise
        finally:
            if self._socket is peer:
                peer.settimeout(previous_timeout)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()


class ScpiInstrument(Instrument):
    """Анализатор спектра или приёмник с управлением по SCPI."""

    device_name = "SCPI"

    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 10.0,
        connection: Optional[ScpiConnection] = None,
        diagnostic_directory=None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._conn = connection or ScpiConnection(host, port, timeout_s)
        self._info = InstrumentInfo(interface=f"tcp:{host}:{port}")
        self._connected = False
        self.diagnostic_directory = diagnostic_directory
        self.last_quality_issue = None

    # -- подключение -------------------------------------------------------

    def connect(self) -> InstrumentInfo:
        self._conn.open()
        self._connected = True
        response = self._conn.query("*IDN?").strip()
        parts = [p.strip() for p in response.split(",")]
        self._info = InstrumentInfo(
            manufacturer=parts[0] if parts else "",
            model=parts[1] if len(parts) > 1 else "",
            serial=parts[2] if len(parts) > 2 else "",
            firmware=parts[3] if len(parts) > 3 else "",
            interface=f"tcp:{self.host}:{self.port}",
        )
        return self._info

    def disconnect(self) -> None:
        self._conn.close()
        self._connected = False

    def identify(self) -> InstrumentInfo:
        return self._info

    def _require(self) -> None:
        if not self._connected:
            raise InstrumentError("Прибор не подключён")

    # -- измерение ---------------------------------------------------------

    def sweep(self, config: SweepConfig) -> Trace:
        """Выполняет измерение панорамы диапазона частот."""
        self._require()
        validate_config(config)
        self._conn.write("PRESET")
        self._conn.write(f"STAR {config.start_hz:.0f}")
        self._conn.write(f"STOP {config.stop_hz:.0f}")
        self._conn.write(f"RESB {config.resolution_bandwidth_hz:.0f}")
        self._conn.write(f"VBW {config.video_bandwidth_hz:.0f}")
        self._conn.write(f"POIN {config.points}")
        self._conn.write(f"REFL {config.reference_level_db:.1f}")
        self._conn.write(f"ATT {config.attenuation_db:.1f}")
        if config.sweep_time_s > 0:
            self._conn.write(f"SWE {config.sweep_time_s:.3f}")
        self._conn.write("INIT 1")
        self._conn.write("TRIG:SOUR AUTO")
        self._conn.write("INIT;*OPC?")
        try:
            self._conn.read()
            data = self._conn.query("TRAC:DATA? TRACE1")
            levels = parse_ascii(data, expected_points=config.points)
        except ScpiDataError as error:
            error.diagnostic['expected_points'] = config.points
            retain_quality_issue(self, error, config)
            raise
        freqs = np.linspace(config.start_hz, config.stop_hz, config.points)
        return Trace(freqs, levels, "dBm")

    @staticmethod
    def _parse_curve(payload: str) -> np.ndarray:
        """Разбор числового массива из ответа прибора."""
        return parse_ascii(payload)

    # -- управление --------------------------------------------------------

    def set_center_frequency(self, frequency_hz: float) -> None:
        self._require()
        self._conn.write(f"CENT {frequency_hz:.0f}")

    def set_squelch(self, level: int) -> None:
        self._require()
        self._conn.write(f"SQU:LEV {int(level)}")

    def set_volume(self, level: int) -> None:
        self._require()
        self._conn.write(f"VOL {int(level)}")


class Vxi11Instrument(Instrument):
    """Прибор по протоколу VXI-11 (LXI).

    Требуется внешняя библиотека ``python-vxi11``:
    ``pip install python-vxi11``.
    """

    device_name = "VXI-11"

    def __init__(self, host: str, resource: str = "", diagnostic_directory=None,
                 timeout_s=10.0, max_response_bytes=MAX_RESPONSE_BYTES) -> None:
        self.host = host
        self.resource = resource
        self._instr = None
        self._info = InstrumentInfo(interface=f"vxi11:{host}")
        self.diagnostic_directory = diagnostic_directory
        self.last_quality_issue = None
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError('Invalid SCPI response limit')
        self.max_response_bytes = max_response_bytes
        self.timeout_s = timeout_s

    def connect(self) -> InstrumentInfo:
        try:
            import vxi11  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise InstrumentError(
                "Для VXI-11 требуется библиотека python-vxi11 "
                "(pip install python-vxi11)"
            ) from exc
        self._instr = vxi11.Instrument(self.host, self.resource) if self.resource else vxi11.Instrument(self.host)
        self._instr.write('*IDN?')
        response = self._read_response().strip()
        parts = [p.strip() for p in response.split(",")]
        self._info = InstrumentInfo(
            manufacturer=parts[0] if parts else "",
            model=parts[1] if len(parts) > 1 else "",
            serial=parts[2] if len(parts) > 2 else "",
            firmware=parts[3] if len(parts) > 3 else "",
            interface=f"vxi11:{self.host}",
        )
        return self._info

    def disconnect(self) -> None:
        if self._instr is not None:
            try:
                self._instr.close()
            except Exception:  # pragma: no cover - зависит от прибора
                pass
            self._instr = None

    def identify(self) -> InstrumentInfo:
        return self._info

    def _read_response(self):
        instrument = self._instr
        if instrument.link is None: instrument.open()
        peer = instrument.client.sock
        client = instrument.client
        previous_timeout = peer.gettimeout()
        deadline = time.monotonic() + self.timeout_s
        instrument.client.sock = BoundedVxiSocket(peer, deadline, self.max_response_bytes + 65536)
        response = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScpiDataError('VXI-11 response deadline exceeded', response)
                count = min(65536, self.max_response_bytes + 1 - len(response))
                milliseconds = max(1, int(remaining * 1000))
                error, reason, data = client.device_read(
                    instrument.link, count, milliseconds, milliseconds, 0, 0)
                if error or reason & ~7 or not isinstance(data, bytes) or len(data) > count:
                    raise ScpiDataError('VXI-11 invalid RPC response', response)
                response.extend(data)
                if len(response) > self.max_response_bytes:
                    raise ScpiDataError('VXI-11 response size limit exceeded', response)
                # With TERMCHAR disabled, only IEEE-488 END confirms completion.
                if reason & 4:
                    try: return response.decode('ascii', errors='strict')
                    except UnicodeError as exc:
                        raise ScpiDataError('VXI-11 non-ASCII response', response) from exc
                if not data:
                    raise ScpiDataError('VXI-11 response made no progress before END', response)
        except ScpiDataError:
            self.disconnect()
            raise
        except (OSError, RuntimeError) as exc:
            self.disconnect()
            raise ScpiDataError('VXI-11 response I/O failure', response) from exc
        finally:
            client.sock = peer
            if self._instr is instrument: peer.settimeout(previous_timeout)
            else: peer.close()

    def sweep(self, config: SweepConfig) -> Trace:
        validate_config(config)
        if self._instr is None:
            self.connect()
        self._instr.write("PRESET")
        self._instr.write(f"STAR {config.start_hz:.0f}")
        self._instr.write(f"STOP {config.stop_hz:.0f}")
        self._instr.write(f"RESB {config.resolution_bandwidth_hz:.0f}")
        self._instr.write(f"POIN {config.points}")
        self._instr.write("INIT;*OPC?")
        try:
            self._read_response()
            self._instr.write('TRAC:DATA? TRACE1')
            data = self._read_response()
            levels = parse_ascii(data, expected_points=config.points)
        except ScpiDataError as error:
            error.diagnostic['expected_points'] = config.points
            retain_quality_issue(self, error, config)
            raise
        freqs = np.linspace(config.start_hz, config.stop_hz, config.points)
        return Trace(freqs, levels, "dBm")
