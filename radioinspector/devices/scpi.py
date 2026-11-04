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

import re
import socket
import time
from typing import Optional

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig
from .registry import profile_from_idn

#: Разбор блока данных прибора: ``<prefix> <count> <comma separated values>``.
_CURVE_RE = re.compile(r"([A-Za-z0-9_]+)\s+(\d+)\s*,?\s*(.*)", re.DOTALL)


class ScpiConnection:
    """Транспорт SCPI поверх TCP-сокета."""

    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 10.0,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.read_termination = read_termination
        self.write_termination = write_termination
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
        chunks: list[bytes] = []
        while True:
            try:
                data = self._socket.recv(65536)
            except socket.timeout as exc:
                raise InstrumentError(
                    f"Таймаут чтения ответа от {self.host}:{self.port}"
                ) from exc
            if not data:
                break
            chunks.append(data)
            if self.read_termination.encode() in data:
                break
        return b"".join(chunks).decode("ascii", errors="replace")

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
        profile: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.profile = profile
        self._conn = connection or ScpiConnection(host, port, timeout_s)
        self._info = InstrumentInfo(interface=f"tcp:{host}:{port}")
        self._connected = False

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
        if not self.profile:
            self.profile = profile_from_idn(self._info.idn)
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
        self._conn.read()

        data = self._conn.query("TRAC:DATA? TRACE1")
        levels = self._parse_curve(data)
        if levels.size != config.points and levels.size >= 2:
            freqs = np.linspace(config.start_hz, config.stop_hz, levels.size)
        else:
            freqs = np.linspace(config.start_hz, config.stop_hz, config.points)
        return Trace(freqs, levels[: freqs.size], "dBm")

    @staticmethod
    def _parse_curve(payload: str) -> np.ndarray:
        """Разбор числового массива из ответа прибора."""
        text = payload.strip()
        if not text:
            return np.array([])
        match = _CURVE_RE.match(text)
        if match:
            text = match.group(3)
        text = text.strip().strip(",")
        values: list[float] = []
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                values.append(float(chunk))
            except ValueError:
                continue
        return np.asarray(values, dtype=float)

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

    def __init__(self, host: str, resource: str = "") -> None:
        self.host = host
        self.resource = resource
        self._instr = None
        self._info = InstrumentInfo(interface=f"vxi11:{host}")

    def connect(self) -> InstrumentInfo:
        try:
            import vxi11  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise InstrumentError(
                "Для VXI-11 требуется библиотека python-vxi11 "
                "(pip install python-vxi11)"
            ) from exc
        self._instr = vxi11.Instrument(self.host, self.resource) if self.resource else vxi11.Instrument(self.host)
        response = str(self._instr.ask("*IDN?")).strip()
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

    def sweep(self, config: SweepConfig) -> Trace:
        if self._instr is None:
            self.connect()
        self._instr.write("PRESET")
        self._instr.write(f"STAR {config.start_hz:.0f}")
        self._instr.write(f"STOP {config.stop_hz:.0f}")
        self._instr.write(f"RESB {config.resolution_bandwidth_hz:.0f}")
        self._instr.write(f"POIN {config.points}")
        self._instr.write("INIT;*OPC?")
        self._instr.read()
        data = str(self._instr.query("TRAC:DATA? TRACE1"))
        levels = ScpiInstrument._parse_curve(data)
        freqs = np.linspace(config.start_hz, config.stop_hz, max(2, levels.size))
        return Trace(freqs[: levels.size], levels, "dBm")
