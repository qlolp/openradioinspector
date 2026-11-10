"""Явные последовательные адаптеры tinySA Ultra и AOR AR-DV1.

Порты открываются только при явном ``settings.adapter`` значении
``tinysa-serial`` или ``ardv1-serial``. Каждый worker получает отдельный
transport. Проверка на физическом стенде здесь не заявляется.
"""

from __future__ import annotations

import math
import os
import re
import select
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    DeviceRole,
    DeviceStream,
    ReceiverObservation,
    Sweep,
    TimeInterval,
    format_time,
    utc_now,
)
from .device_workers import SimulatedAdapter, WorkerError, create_simulated_adapter
from .tinysa_profile import PROFILE_ID, matches_identity, profile_description, validate_receive_settings


HARDWARE_VALIDATION_NOT_RUN = "not_run"


class _SerialFraming:
    """Common reply framing; backends only move raw bytes with a timeout."""

    def cancel(self) -> None:
        """Interrupt only this transport; a cancelled connection cannot be reused."""
        self._cancelled.set()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise WorkerError(f"Операция на {self.path} остановлена; требуется новое соединение")
        check = getattr(self, '_ownership_check', None)
        if check is not None:
            check()

    def _require_clean_input(self) -> None:
        # There are no transaction IDs: pending bytes belong to an earlier
        # exchange, never to the command we are about to send. Do not flush
        # them and continue, because that would hide a rejection or late ACK.
        if self._stash or self._read_once(0.0, "проверка перед командой"):
            raise WorkerError(f"Непрочитанный ответ перед новой командой на {self.path}")

    def read_line(self, timeout: float, label: str) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            if len(self._stash) > 1024 * 1024:
                raise WorkerError(f"Превышен размер текстового ответа: {label}")
            newline = self._stash.find(b"\n")
            if newline >= 0:
                raw = bytes(self._stash[:newline]).rstrip(b"\r")
                del self._stash[: newline + 1]
                return raw.decode("utf-8", errors="replace")
            self._read_once(max(0.01, deadline - time.monotonic()), label)
        raise WorkerError(f"Таймаут ответа {label} на {self.path}")

    def read_until_quiet(self, quiet: float, maximum: float, label: str) -> str:
        # Keep the public method name for transport compatibility. A quiet gap
        # is only a polling interval, never a shell reply terminator: tinySA
        # echoes scan immediately, then can measure silently for many seconds.
        # The same applies to gaps between rows and fragments of the prompt.
        deadline = time.monotonic() + maximum
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            chunk = self._read_once(min(quiet, max(0.0, remaining)), label)
            if chunk:
                if len(self._stash) > 1024 * 1024:
                    raise WorkerError(f"Превышен размер текстового ответа: {label}")
                text = self._stash.decode("utf-8", errors="replace")
                if text.strip().endswith("ch>"):
                    self._stash.clear()
                    return text
        if not self._stash:
            raise WorkerError(f"Пустой ответ {label} на {self.path}")
        # Do not clear, resynchronize, retry or send another command. The
        # adapter taints this connection, so a late prompt cannot ACK setup.
        raise WorkerError(f"Незавершённый ответ {label} на {self.path}: таймаут завершающего ch>")


class PosixSerialTransport(_SerialFraming):
    """Маленький blocking-by-timeout transport без внешних зависимостей."""

    def __init__(self, path: str, baud: int = 9600) -> None:
        import termios

        if not isinstance(path, str) or not path.strip():
            raise WorkerError("Последовательный порт не должен быть пустым")
        speeds = {
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
        }
        if baud not in speeds:
            raise WorkerError(f"Неподдерживаемая скорость порта: {baud}")
        self.path = path
        self._stash = bytearray()
        self._cancelled = threading.Event()
        self._fd = -1
        self._original_settings: Optional[list[Any]] = None
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            # Cooperating core processes must not configure/read the same tty.
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._original_settings = termios.tcgetattr(self._fd)
            settings = list(self._original_settings)
            settings[6] = list(self._original_settings[6])
            # Python 3.10+ exposes speeds as tcgetattr/tcsetattr fields, not
            # the C cfset*speed/cfmakeraw functions. Keep the saved cc list intact.
            settings[0] &= ~(
                termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                | termios.INLCR | termios.IGNCR | termios.ICRNL
                | termios.IXON | termios.IXOFF | termios.IXANY
            )
            settings[1] &= ~termios.OPOST
            settings[2] &= ~(
                termios.CSIZE | termios.CSTOPB | termios.PARENB
                | getattr(termios, "CRTSCTS", 0)
            )
            settings[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
            settings[3] &= ~(
                termios.ECHO | termios.ECHONL | termios.ICANON
                | termios.ISIG | termios.IEXTEN
            )
            settings[4] = settings[5] = speeds[baud]
            settings[6][termios.VMIN] = 1
            settings[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, settings)
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except Exception as exc:
            try:
                self.close()
            except Exception:
                pass  # Preserve the opening error; close still releases the fd.
            raise WorkerError(f"Не удалось открыть порт {path}: {exc}") from exc

    def send(self, data: bytes) -> None:
        self._require_clean_input()
        view = memoryview(data)
        sent = 0
        deadline = time.monotonic() + 1.0
        while sent < len(view):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError(f"Таймаут записи в {self.path}")
                _, ready, _ = select.select([], [self._fd], [], remaining)
                if not ready:
                    raise WorkerError(f"Таймаут записи в {self.path}")
                try:
                    count = os.write(self._fd, view[sent:])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise OSError("запись вернула ноль")
                sent += count
            except WorkerError:
                raise
            except Exception as exc:
                raise WorkerError(f"Ошибка записи в {self.path}: {exc}") from exc

    def _read_once(self, timeout: float, label: str) -> bytes:
        try:
            self._check_cancelled()
            ready, _, _ = select.select([self._fd], [], [], min(0.1, max(0.0, timeout)))
            self._check_cancelled()
            if not ready:
                return b""
            chunk = os.read(self._fd, 4096)
            if not chunk:
                raise WorkerError(f"Порт {self.path} закрыт во время {label}")
            self._stash.extend(chunk)
            return chunk
        except WorkerError:
            raise
        except Exception as exc:
            raise WorkerError(f"Ошибка чтения {label} на {self.path}: {exc}") from exc

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        self._fd = -1
        if fd >= 0:
            try:
                try:
                    if self._original_settings is not None:
                        import termios

                        termios.tcsetattr(fd, termios.TCSANOW, self._original_settings)
                finally:
                    os.close(fd)
            except Exception as exc:
                raise WorkerError(f"Ошибка закрытия порта {self.path}: {exc}") from exc
            finally:
                self._original_settings = None
                self._stash.clear()


class WindowsSerialTransport(_SerialFraming):
    """Bounded COM I/O through optional pySerial, owned only by the core worker."""

    def __init__(self, path: str, baud: int = 9600) -> None:
        # Accept only native COM names, never pySerial URL handlers or arbitrary files.
        if not isinstance(path, str):
            raise WorkerError("Windows требует имя COM-порта, например COM3")
        name = path[4:] if path.startswith("\\\\.\\") else path
        if re.fullmatch(r"COM[1-9][0-9]*", name, re.IGNORECASE | re.ASCII) is None:
            raise WorkerError("Windows требует имя COM-порта, например COM3")
        if type(baud) is not int or baud not in (9600, 19200, 38400, 57600, 115200):
            raise WorkerError(f"Неподдерживаемая скорость порта: {baud}")
        self.path = name.upper()
        self._stash = bytearray()
        self._cancelled = threading.Event()
        self._serial = None
        try:
            import serial
        except ImportError as exc:
            raise WorkerError(
                'Для COM-портов установите на компьютере ядра: pip install "openradioinspector[serial]"'
            ) from exc
        try:
            self._serial = serial.Serial(
                port=None, baudrate=baud, bytesize=8, parity="N", stopbits=1,
                timeout=0, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False,
            )
            # Set control lines before opening, without enabling hardware flow control.
            self._serial.dtr = False
            self._serial.rts = False
            self._serial.port = self.path
            self._serial.open()
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception as exc:
            try:
                self.close()
            except Exception:
                pass  # Preserve the opening error after attempting to release the handle.
            raise WorkerError(f"Не удалось открыть порт {self.path}: {exc}") from exc

    def _opened(self):
        self._check_cancelled()
        if self._serial is None or not self._serial.is_open:
            raise WorkerError(f"Порт {self.path} закрыт")
        return self._serial

    def send(self, data: bytes) -> None:
        try:
            port = self._opened()
            self._require_clean_input()
            view = memoryview(data)
            sent = 0
            deadline = time.monotonic() + 1.0
            while sent < len(view):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError(f"Таймаут записи в {self.path}")
                port.write_timeout = remaining
                count = port.write(view[sent:])
                self._opened()  # A cancelled Windows write can return a byte count.
                if not isinstance(count, int) or not 0 < count <= len(view) - sent:
                    raise WorkerError(f"Неполная запись в {self.path}")
                sent += count
        except WorkerError:
            raise
        except Exception as exc:
            raise WorkerError(f"Ошибка записи в {self.path}: {exc}") from exc

    def _read_once(self, timeout: float, label: str) -> bytes:
        try:
            port = self._opened()
            port.timeout = min(0.1, max(0.0, timeout))
            # Wait for one byte, then drain only bytes already available. A 4096-byte
            # blocking read would delay a complete short reply until the timeout.
            chunk = port.read(1)
            self._opened()  # Cancellation/close may return partial bytes, not an error.
            if chunk:
                port.timeout = 0
                available = min(port.in_waiting, 4095)
                if available:
                    chunk += port.read(available)
                    self._opened()
                self._stash.extend(chunk)
            return chunk
        except WorkerError:
            raise
        except Exception as exc:
            raise WorkerError(f"Ошибка чтения {label} на {self.path}: {exc}") from exc

    def close(self) -> None:
        port = self._serial
        self._serial = None
        self._stash.clear()
        if port is not None:
            try:
                port.close()
            except Exception as exc:
                raise WorkerError(f"Ошибка закрытия порта {self.path}: {exc}") from exc


def create_serial_transport(path: str, baud: int = 9600):
    """Select by the core OS, not the OS of an HTTP client."""
    if os.name == "nt":
        return WindowsSerialTransport(path, baud)
    return PosixSerialTransport(path, baud)


def _command(text: str) -> bytes:
    if not text or any(character in text for character in "\r\n"):
        raise WorkerError("Некорректная команда прибора")
    return text.encode("ascii") + b"\r"


def _frequency_token(value: int) -> str:
    for scale, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if value % scale == 0:
            return f"{value // scale}{suffix}"
    return str(value)


def _parse_frequency(token: str) -> int:
    try:
        value = int(token)
    except ValueError:
        scale = {"k": 1_000, "M": 1_000_000, "G": 1_000_000_000}.get(token[-1:])
        if scale is None:
            raise WorkerError(f"Некорректная частота tinySA: {token!r}")
        try:
            value = round(float(token[:-1].replace(",", ".")) * scale)
        except ValueError as exc:
            raise WorkerError(f"Некорректная частота tinySA: {token!r}") from exc
    if value <= 0:
        raise WorkerError(f"Частота tinySA должна быть положительной: {token!r}")
    return value


def parse_tinysa_scan(text: str, command: str, points: int) -> tuple[tuple[int, ...], tuple[float, ...]]:
    frequencies: list[int] = []
    levels: list[float] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == "ch>" or line == command:
            continue
        tokens = line.replace(",", " ").split()
        if len(tokens) < 2:
            raise WorkerError(f"Неразобранная строка scan: {line!r}")
        frequencies.append(_parse_frequency(tokens[0]))
        try:
            level = float(tokens[1].removesuffix("dBm"))
        except ValueError as exc:
            raise WorkerError(f"Некорректный уровень tinySA: {tokens[1]!r}") from exc
        if not math.isfinite(level):
            raise WorkerError(f"Уровень tinySA не конечен: {tokens[1]!r}")
        levels.append(level)
    if len(frequencies) != points:
        raise WorkerError(f"Ожидалось {points} точек scan, получено {len(frequencies)}")
    if any(right < left for left, right in zip(frequencies, frequencies[1:])):
        raise WorkerError("Частоты scan не монотонны")
    return tuple(frequencies), tuple(levels)


def _ardv1_body(raw: str) -> str:
    text = raw.strip()
    if not text.isascii():
        raise WorkerError(f"Некорректный ответ AR-DV1: {raw!r}")
    if len(text) >= 2 and text[0].isdigit() and text[1] in "01":
        kind = int(text[0])
        rest = text[2:]
        plausible = not rest or rest[0] in "? " or rest[0].isalpha()
        if 1 <= kind <= 6 and plausible:
            if kind >= 3:
                raise WorkerError(f"AR-DV1 отклонил команду: {text}")
            if text[1] == "1":
                raise WorkerError(f"Продолженный ответ AR-DV1 не поддерживается: {text}")
            return rest.strip()
    if text == "?":
        raise WorkerError("AR-DV1 отклонил команду: ?")
    return text


def _ardv1_level(raw: str) -> tuple[int, int]:
    body = _ardv1_body(raw)
    if not body.startswith("LM") or len(body) <= 3:
        raise WorkerError(f"Некорректный ответ LM: {raw!r}")
    payload = body[2:]
    if not payload.isascii() or not payload.isdigit():
        raise WorkerError(f"Некорректный ответ LM: {raw!r}")
    try:
        squelch = int(payload[-1])
        level = int(payload[:-1])
    except ValueError as exc:
        raise WorkerError(f"Некорректный ответ LM: {raw!r}") from exc
    if squelch not in (0, 1, 2, 3) or not 1 <= len(payload[:-1]) <= 6:
        raise WorkerError(f"Некорректный ответ LM: {raw!r}")
    return level, squelch


def _rf_command(frequency_hz: int) -> str:
    if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
        raise WorkerError("frequency_hz должно быть целым числом")
    if not 100_000 <= frequency_hz <= 1_300_000_000:
        raise WorkerError("Частота AR-DV1 вне диапазона 0,1–1300 МГц")
    units = frequency_hz // 10
    if frequency_hz % 10:
        raise WorkerError("Частота AR-DV1 должна быть кратна 10 Гц")
    return f"RF{units // 100_000:04d}.{units % 100_000:05d}"


class TinySASerialAdapter(SimulatedAdapter):
    adapter_name = "tinysa-serial"

    def __init__(
        self,
        stream: DeviceStream,
        *,
        clock: Callable[[], datetime] = utc_now,
        transport: Any = None,
        transport_factory: Callable[..., Any] = create_serial_transport,
    ) -> None:
        super().__init__(stream)
        if stream.role is not DeviceRole.SPECTRUM_ANALYZER:
            raise ValueError("tinysa-serial требует роль spectrum_analyzer")
        if stream.level_unit != "dBm":
            raise ValueError("tinysa-serial выдаёт только исходную единицу dBm")
        self._clock = clock
        requested = validate_receive_settings(self.settings)
        self.start_hz = requested["start_hz"]
        self.stop_hz = requested["stop_hz"]
        self.points = requested["points"]
        self.rbw_hz = requested["rbw_hz"]
        self._identity: dict[str, str] = {}
        self._instrument = None
        if transport is None:
            if not stream.port or stream.port.startswith("sim://"):
                raise WorkerError("tinysa-serial требует физический последовательный порт")
            transport = transport_factory(stream.port, int(self.settings.get("baud", 9600)))
        self.transport = transport
        self._ready = False
        self._closed = False
        self._sequence = 0
        self._restart_required = False
        self._generator_output_source = "unconfirmed"

    def _ensure_usable(self) -> None:
        if self._restart_required or self._closed:
            raise WorkerError("tinySA: требуется перезапуск потока (stop/start) с новым соединением")

    def _taint_connection(self, error: Exception) -> WorkerError:
        # Shell replies have no transaction IDs. An incomplete/late prompt
        # must never acknowledge a later setup command or an automatic retry.
        self._ready = False
        self._restart_required = True
        self._generator_output_source = "unknown"
        return WorkerError(
            f"{error}; tinySA: требуется перезапуск потока (stop/start) с новым соединением"
        )

    def _transact(self, text: str, maximum: float, *, allow_leading_prompt: bool = False) -> str:
        self.transport.send(_command(text))
        raw = self.transport.read_until_quiet(quiet=0.25, maximum=maximum, label=text)
        # A quiet interval alone is not a completed command. Accept only the
        # supported shell frame: optional exact echo, body, one terminal ch>.
        # Apply framing to identity/scan too, so their partial responses cannot
        # leave a prompt to be consumed as the next setup acknowledgement.
        if not raw.isascii() or any(
            (ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in raw
        ):
            raise WorkerError(f"Некорректный ответ tinySA на {text}: {raw!r}")
        lines = [line.strip(" \t") for line in raw.splitlines() if line.strip(" \t")]
        # The legacy console scan sample includes an opening prompt. It is
        # harmless only for data replies with a separately checked point count;
        # never permit it to stand in for a setup acknowledgement.
        if allow_leading_prompt and len(lines) > 1 and lines[0] == "ch>":
            lines = lines[1:]
        if not lines or lines[-1] != "ch>" or any("ch>" in line for line in lines[:-1]):
            raise WorkerError(f"Незавершённый или неоднозначный ответ tinySA на {text}: {raw!r}")
        body = lines[:-1]
        if body and body[0] == text:
            body = body[1:]
        return "\n".join(body)

    def _acknowledge(self, text: str) -> None:
        body = self._transact(text, 3.0)
        # The supported setup commands return no body on success. Reject all
        # other content, including errors/help/unknown status, rather than
        # trying to enumerate firmware-specific rejection strings.
        if body:
            raise WorkerError(f"tinySA не подтвердил команду {text}: {body!r}")

    def _prepare(self) -> None:
        if self._ready:
            return
        info = self._transact("info", 3.0)
        version = self._transact("version", 3.0)
        identity = self._transact("deviceid", 3.0)
        self._identity = {"info": info, "firmware": version}
        if not matches_identity(info, version) or not identity.strip():
            raise WorkerError(
                f"tinySA Ultra: модель/firmware не поддержаны профилем {PROFILE_ID}; "
                "setup и scan не отправлены"
            )
        rbw_khz = self.rbw_hz / 1000
        rbw_text = str(round(rbw_khz, 6)).rstrip("0").rstrip(".")
        for command in (
            "output off", "mode input", "lna off", "attenuate auto",
            f"rbw {rbw_text}", "calc off",
        ):
            self._acknowledge(command)
        self._ready = True
        self._generator_output_source = "command_acknowledged"

    def execute(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command != 'instrument':
            return super().execute(command,payload)
        if self._instrument is None:
            from .tinysa.controller import Controller
            self._instrument=Controller(self)
        return self._instrument.execute(payload)

    def instrument_idle(self) -> bool:
        return self._instrument is not None and not self._instrument.before_poll()

    def poll(self) -> Sweep:
        self._ensure_usable()
        if self._instrument is not None and self.start_hz==self.stop_hz:
            from .tinysa.artifacts import InstrumentSample
            started=self._clock()
            item=self._instrument._raw({},zero_span=True)['artifact']
            return InstrumentSample(TimeInterval(started,self._clock()),item)
        try:
            self._prepare()
            started_at = self._clock()
            command = (
                f"scan {_frequency_token(self.start_hz)} "
                f"{_frequency_token(self.stop_hz)} {self.points} 3"
            )
            frequencies, levels = parse_tinysa_scan(
                self._transact(command, 90.0, allow_leading_prompt=True), command, self.points
            )
            if frequencies[0] != self.start_hz or frequencies[-1] != self.stop_hz:
                raise WorkerError("tinySA: границы scan не соответствуют запрошенному диапазону")
        except Exception as exc:
            raise self._taint_connection(exc) from exc
        ended_at = self._clock()
        self._sequence += 1
        if self._instrument is not None and (self._instrument._trace_processed or self._instrument.settings.get('calculation','off')!='off' or self._instrument.settings.get('trace_unit','dBm')!='dBm'):
            import json
            from .tinysa.artifacts import InstrumentSample
            data=json.dumps(dict(frequencies_hz=frequencies,levels=levels,
                level_unit=self._instrument.settings.get('trace_unit','dBm'),
                started_at=format_time(started_at),ended_at=format_time(ended_at)),allow_nan=False).encode()
            item=self._instrument._artifact('processed_trace',data,'application/json','tinysa-trace.json',started_at=format_time(started_at),ended_at=format_time(ended_at),timing_source='host_usb_interval')['artifact']
            return InstrumentSample(TimeInterval(started_at,ended_at),item)
        return Sweep(
            session_id=self.stream.session_id,
            stream_id=self.stream.id,
            interval=TimeInterval(started_at, ended_at),
            frequencies_hz=frequencies,
            levels=levels,
            level_unit="dBm",
            settings={
                "adapter": self.adapter_name,
                "rbw_hz": self.rbw_hz,  # Legacy field: requested, not measured.
                "receive_profile": PROFILE_ID,
                "device_identity": dict(self._identity),
                "rbw_requested_hz": self.rbw_hz,
                "rbw_applied_state": "unknown",
                "settings_source": "command_acknowledged",
                "settings_readback": False,
                "sequence": self._sequence,
                "simulated": False,
                "generator_output": "off",
                "generator_output_source": self._generator_output_source,
                "generator_output_readback": False,
                "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
                **({'instrument':self._instrument.provenance(),'receive_profile':self._instrument.status()['profile'],
                    'generator_output':'unknown','generator_output_source':'no_physical_readback'} if self._instrument is not None else {}),
            },
        )

    def details(self) -> dict[str, Any]:
        result = {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "points": self.points,
            "receive_profile": profile_description(),
            "device_identity": dict(self._identity),
            "rbw_requested_hz": self.rbw_hz,
            "rbw_applied_hz": None,
            "settings_source": "command_acknowledged" if self._ready else "unknown",
            "settings_readback": False,
            "generator_output": "off" if self._ready else "unknown",
            "generator_output_source": self._generator_output_source,
            "generator_output_readback": False,
            "restart_required": self._restart_required,
            "simulated": False,
            "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
        }
        if self._instrument is not None:
            result['instrument']=self._instrument.status()
            result['generator_output']='unknown'
            result['settings_source']=result['instrument']['settings_source']
        return result

    def request_stop(self) -> None:
        if self._instrument is not None and not self._instrument.request_stop():
            return  # idle lab: owning worker can perform orderly off in close
        cancel = getattr(self.transport, "cancel", None)
        if cancel is not None:
            cancel()

    def close(self) -> None:
        if not self._closed:
            if self._instrument is not None:self._instrument.shutdown()
            self._closed = True
            self._ready = False
            self._generator_output_source = "unknown"
            self.transport.close()


class ARDV1SerialAdapter(SimulatedAdapter):
    adapter_name = "ardv1-serial"
    MONITOR_MODES = {'AUTO':'MD000', 'DSTAR':'MD010', 'YSF':'MD020',
                     'ALINCO':'MD030', 'NXDN':'MD040', 'P25':'MD050',
                     'DPMR':'MD060', 'DMR':'MD070', 'FM':'MD0F0', 'AM':'MD0F1'}
    DECODED = {'1':'DSTAR', '2':'YSF', '3':'ALINCO', '4':'NXDN',
               '5':'P25', '6':'DPMR', '7':'DMR'}

    def __init__(
        self,
        stream: DeviceStream,
        *,
        clock: Callable[[], datetime] = utc_now,
        transport: Any = None,
        transport_factory: Callable[..., Any] = create_serial_transport,
    ) -> None:
        super().__init__(stream)
        if stream.role is not DeviceRole.RECEIVER:
            raise ValueError("ardv1-serial требует роль receiver")
        if stream.level_unit != "s_meter_count":
            raise ValueError("ardv1-serial выдаёт только исходную единицу s_meter_count")
        self._clock = clock
        self._requested_frequency_hz = self.settings.get("frequency_hz", 145_500_000)
        _rf_command(self._requested_frequency_hz)
        self._requested_mode = str(self.settings.get("mode") or "FM").strip().upper()
        profile = self.settings.get('receiver_profile')
        if profile not in (None, 'ardv1-monitor-v1'):
            raise ValueError('Неизвестный профиль AR-DV1')
        self._monitor_profile = profile == 'ardv1-monitor-v1'
        self._modes = self.MONITOR_MODES if self._monitor_profile else {'FM':'MD0F0','AM':'MD0F1'}
        if self._requested_mode not in self._modes:
            raise ValueError('Режим не поддерживается выбранным профилем AR-DV1')
        self._firmware = None
        if transport is None:
            if not stream.port or stream.port.startswith("sim://"):
                raise WorkerError("ardv1-serial требует физический последовательный порт")
            transport = transport_factory(stream.port, int(self.settings.get("baud", 115200)))
        self.transport = transport
        self._ready = False
        self._closed = False
        self._sequence = 0
        self._tune_sequence = 0
        self.frequency_hz: Optional[int] = None
        self.mode: Optional[str] = None
        self._frequency_source = "unconfirmed"
        self._restart_required = False

    def _ensure_usable(self) -> None:
        if self._restart_required or self._closed:
            raise WorkerError("AR-DV1: требуется перезапуск потока (stop/start) с новым соединением")

    def _taint_connection(self, error: Exception) -> WorkerError:
        # Replies have no command IDs. After an uncertain exchange, a delayed
        # ACK/LM can be mistaken for the next reply. Never flush and retry on
        # this connection, including an explicit tune or EX during close.
        self._restart_required = True
        self.frequency_hz = None
        self.mode = None
        self._frequency_source = "unknown"
        return WorkerError(
            f"{error}; AR-DV1: требуется перезапуск потока (stop/start) с новым соединением"
        )

    def _transact(self, text: str) -> str:
        self.transport.send(_command(text))
        return _ardv1_body(self.transport.read_line(timeout=1.5, label=text))

    def _acknowledge(self, text: str) -> None:
        self.transport.send(_command(text))
        raw = self.transport.read_line(timeout=1.5, label=text).strip()
        _ardv1_body(raw)  # Retain the specific rejection diagnostic.
        # Only terminal, body-free success codes qualify. Echoes, data for a
        # different command and continuation frames cannot confirm a mutation.
        # This result-code framing remains unverified on physical hardware.
        if raw not in ("10", "20"):
            raise WorkerError(f"AR-DV1 не подтвердил команду {text}: {raw!r}")

    def _prepare(self) -> None:
        if self._ready:
            return
        self._acknowledge("RE1")
        firmware = self._transact("VR")
        if not firmware.startswith("VR") or len(firmware) <= 2:
            raise WorkerError("AR-DV1 не подтвердил VR")
        self._firmware = firmware[2:]
        if self._monitor_profile and self._firmware != '2504A':
            raise WorkerError('Профиль ardv1-monitor-v1 требует подтверждённый VR2504A')
        serial = self._transact("RN")
        if not serial.startswith("RN") or len(serial) <= 2:
            raise WorkerError("AR-DV1 не подтвердил RN")
        self._ready = True

    def _tune(self, frequency_hz: int, mode: str) -> None:
        self._ensure_usable()
        rf = _rf_command(frequency_hz)  # Validate before any device mutation.
        self._requested_frequency_hz = frequency_hz
        self._requested_mode = mode
        # A failed write/ACK may still have changed VFO, mode or frequency.
        # Never retain the previous frequency as if it described later LM data.
        self.frequency_hz = None
        self.mode = None
        self._frequency_source = "unknown"
        try:
            self._prepare()
            self._acknowledge("VFA")
            self._acknowledge(self._modes[mode])
            self._acknowledge(rf)
        except Exception as exc:
            raise self._taint_connection(exc) from exc
        self.frequency_hz = frequency_hz
        self.mode = mode
        self._frequency_source = "command_acknowledged"
        self._tune_sequence += 1

    def execute(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_usable()
        if command != "tune":
            return super().execute(command, payload)
        frequency_hz = payload.get("frequency_hz")
        _rf_command(frequency_hz)
        mode = str(payload.get("mode") or self._requested_mode).strip().upper()
        if mode not in self._modes:
            raise WorkerError('Режим не поддерживается профилем (старый профиль: только FM и AM); команда не отправлена')
        self._tune(frequency_hz, mode)
        return self.details()

    def poll(self) -> ReceiverObservation:
        self._ensure_usable()
        if self._frequency_source == "unconfirmed":
            self._tune(self._requested_frequency_hz, self._requested_mode)
        if self.frequency_hz is None or self.mode is None:
            raise WorkerError("AR-DV1: частота неизвестна; требуется успешная перестройка")
        started_at = self._clock()
        try:
            level, squelch = _ardv1_level(self._transact("LM"))
            mode_status = self._transact('MD') if self._monitor_profile else None
            if mode_status is not None and not re.fullmatch(r'MD[0-7][0-7F][0-6]', mode_status):
                raise WorkerError('Некорректный статус MD')
            if mode_status is not None and self.MONITOR_MODES[self.mode][3:] != mode_status[3:]:
                raise WorkerError('Прочитанный режим AR-DV1 изменился вне ядра')
        except Exception as exc:
            raise self._taint_connection(exc) from exc
        ended_at = self._clock()
        self._sequence += 1
        return ReceiverObservation(
            session_id=self.stream.session_id,
            stream_id=self.stream.id,
            interval=TimeInterval(started_at, ended_at),
            frequency_hz=self.frequency_hz,
            mode=self.mode,
            level=level,
            level_unit="s_meter_count",
            squelch_open=squelch != 0,
            decoded_mode=self.DECODED.get(mode_status[2]) if mode_status else None,
            metadata={
                'receiver_profile': self.settings.get('receiver_profile', 'legacy-fm-am'),
                'receiver_firmware': self._firmware,
                'raw_mode_status': mode_status,
                'decoder_source': 'receiver_MD' if mode_status else 'unavailable',
                'raw_squelch_status': squelch,
                "adapter": self.adapter_name,
                "sequence": self._sequence,
                "tune_sequence": self._tune_sequence,
                "frequency_source": self._frequency_source,
                "mode_source": self._frequency_source,
                "frequency_readback": False,
                "simulated": False,
                "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
            },
        )

    def details(self) -> dict[str, Any]:
        return {
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "requested_frequency_hz": self._requested_frequency_hz,
            "requested_mode": self._requested_mode,
            "frequency_source": self._frequency_source,
            "mode_source": self._frequency_source,
            "frequency_readback": False,
            "restart_required": self._restart_required,
            "tune_sequence": self._tune_sequence,
            "simulated": False,
            "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
        }

    def request_stop(self) -> None:
        cancel = getattr(self.transport, "cancel", None)
        if cancel is not None:
            cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._ready and not self._restart_required:
                try:
                    self.transport.send(_command("EX"))
                    self.transport.read_line(timeout=0.4, label="EX")
                except Exception:
                    pass
        finally:
            self.transport.close()


def create_device_adapter(
    stream: DeviceStream,
    *,
    clock: Callable[[], datetime] = utc_now,
    transport: Any = None,
    transport_factory: Optional[Callable[..., Any]] = None,
) -> SimulatedAdapter:
    """Создаёт sim или serial адаптер только по явному settings.adapter."""
    adapter = stream.settings.get("adapter")
    factory = transport_factory or create_serial_transport
    if adapter == TinySASerialAdapter.adapter_name:
        return TinySASerialAdapter(
            stream, clock=clock, transport=transport, transport_factory=factory
        )
    if adapter == ARDV1SerialAdapter.adapter_name:
        return ARDV1SerialAdapter(
            stream, clock=clock, transport=transport, transport_factory=factory
        )
    if adapter in ("tinysa-sim", "ardv1-sim"):
        return create_simulated_adapter(stream, clock=clock)
    raise UnsupportedAdapterError(
        f"Поток {stream.id}: поддерживаются явные адаптеры "
        "tinysa-sim, ardv1-sim, tinysa-serial и ardv1-serial"
    )


from .device_workers import UnsupportedAdapterError  # noqa: E402  (циклический реэкспорт)
