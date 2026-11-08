"""Явные последовательные адаптеры tinySA Ultra и AOR AR-DV1.

Порты открываются только при явном ``settings.adapter`` значении
``tinysa-serial`` или ``ardv1-serial``. Каждый worker получает отдельный
transport. Проверка на физическом стенде здесь не заявляется.
"""

from __future__ import annotations

import math
import os
import select
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    DeviceRole,
    DeviceStream,
    ReceiverObservation,
    Sweep,
    TimeInterval,
    utc_now,
)
from .device_workers import SimulatedAdapter, WorkerError, create_simulated_adapter


HARDWARE_VALIDATION_NOT_RUN = "not_run"


class PosixSerialTransport:
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
        self._fd = -1
        self._original_settings: Optional[list[Any]] = None
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
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

    def read_line(self, timeout: float, label: str) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            newline = self._stash.find(b"\n")
            if newline >= 0:
                raw = bytes(self._stash[:newline]).rstrip(b"\r")
                del self._stash[: newline + 1]
                return raw.decode("utf-8", errors="replace")
            self._read_once(max(0.01, deadline - time.monotonic()), label)
        raise WorkerError(f"Таймаут ответа {label} на {self.path}")

    def read_until_quiet(self, quiet: float, maximum: float, label: str) -> str:
        deadline = time.monotonic() + maximum
        last_byte = time.monotonic()
        received = False
        while time.monotonic() < deadline:
            chunk = self._read_once(min(quiet, max(0.01, deadline - time.monotonic())), label)
            if chunk:
                received = True
                last_byte = time.monotonic()
                text = self._stash.decode("utf-8", errors="replace").strip()
                if text.endswith("ch>"):
                    break
            elif received and time.monotonic() - last_byte >= quiet:
                break
        if not received:
            raise WorkerError(f"Пустой ответ {label} на {self.path}")
        text = self._stash.decode("utf-8", errors="replace")
        self._stash.clear()
        return text

    def _read_once(self, timeout: float, label: str) -> bytes:
        try:
            ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
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
        transport_factory: Callable[..., Any] = PosixSerialTransport,
    ) -> None:
        super().__init__(stream)
        if stream.role is not DeviceRole.SPECTRUM_ANALYZER:
            raise ValueError("tinysa-serial требует роль spectrum_analyzer")
        if stream.level_unit != "dBm":
            raise ValueError("tinysa-serial выдаёт только исходную единицу dBm")
        self._clock = clock
        self.start_hz = self._frequency_setting("start_hz", 88_000_000)
        self.stop_hz = self._frequency_setting("stop_hz", 108_000_000)
        if self.stop_hz <= self.start_hz:
            raise ValueError("stop_hz должно быть больше start_hz")
        self.points = self._frequency_setting("points", 201, maximum=4096)
        self.rbw_hz = self._frequency_setting("rbw_hz", 10_000, maximum=1_000_000_000)
        if transport is None:
            if not stream.port or stream.port.startswith("sim://"):
                raise WorkerError("tinysa-serial требует физический последовательный порт")
            transport = transport_factory(stream.port, int(self.settings.get("baud", 9600)))
        self.transport = transport
        self._ready = False
        self._closed = False
        self._sequence = 0

    def _frequency_setting(self, name: str, default: int, *, maximum: int = 20_000_000_000) -> int:
        value = self.settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} должно быть целым в диапазоне 1…{maximum}")
        return value

    def _transact(self, text: str, maximum: float) -> str:
        self.transport.send(_command(text))
        return self.transport.read_until_quiet(quiet=0.25, maximum=maximum, label=text)

    def _prepare(self) -> None:
        if self._ready:
            return
        info = self._transact("info", 3.0)
        version = self._transact("version", 3.0)
        identity = self._transact("deviceid", 3.0)
        if "tinysa ultra" not in info.lower() or "tinySA4_" not in version or not identity.strip():
            raise WorkerError("Ответ не похож на tinySA Ultra; scan не отправлен")
        rbw_khz = self.rbw_hz / 1000
        rbw_text = str(round(rbw_khz, 6)).rstrip("0").rstrip(".")
        for command in (
            "output off", "mode input", "lna off", "attenuate auto",
            f"rbw {rbw_text}", "calc off",
        ):
            self._transact(command, 3.0)
        self._ready = True

    def poll(self) -> Sweep:
        self._prepare()
        started_at = self._clock()
        command = (
            f"scan {_frequency_token(self.start_hz)} "
            f"{_frequency_token(self.stop_hz)} {self.points} 3"
        )
        frequencies, levels = parse_tinysa_scan(
            self._transact(command, 90.0), command, self.points
        )
        ended_at = self._clock()
        self._sequence += 1
        return Sweep(
            session_id=self.stream.session_id,
            stream_id=self.stream.id,
            interval=TimeInterval(started_at, ended_at),
            frequencies_hz=frequencies,
            levels=levels,
            level_unit="dBm",
            settings={
                "adapter": self.adapter_name,
                "rbw_hz": self.rbw_hz,
                "sequence": self._sequence,
                "simulated": False,
                "generator_output": "off",
                "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
            },
        )

    def details(self) -> dict[str, Any]:
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "points": self.points,
            "generator_output": "off",
            "simulated": False,
            "hardware_validation": HARDWARE_VALIDATION_NOT_RUN,
        }

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.transport.close()


class ARDV1SerialAdapter(SimulatedAdapter):
    adapter_name = "ardv1-serial"

    def __init__(
        self,
        stream: DeviceStream,
        *,
        clock: Callable[[], datetime] = utc_now,
        transport: Any = None,
        transport_factory: Callable[..., Any] = PosixSerialTransport,
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
        if self._requested_mode not in ("FM", "AM"):
            raise ValueError("ardv1-serial поддерживает только FM и AM в этом срезе")
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
            self._acknowledge("MD0F0" if mode == "FM" else "MD0F1")
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
        if mode not in ("FM", "AM"):
            raise WorkerError("ardv1-serial поддерживает только FM и AM; цифровой режим не отправлен")
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
            decoded_mode=None,
            metadata={
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
    factory = transport_factory or PosixSerialTransport
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
