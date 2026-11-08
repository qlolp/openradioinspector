"""Независимые исполнители приборных потоков и их адаптеры.

Симуляторы tinySA Ultra и AR-DV1 дополнены явными serial-адаптерами.
Каждый исполнитель имеет собственный поток, очередь команд и журнал состояния;
ошибка одного прибора не останавливает другой.
"""

from __future__ import annotations

import math
import os
import queue
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from .contracts import (
    DeviceRole,
    DeviceState,
    DeviceStream,
    MonitoringSession,
    ReceiverObservation,
    Sweep,
    TimeInterval,
    format_time,
    utc_now,
)
from .session_service import SessionService
from .serial_ports import serial_port_identity


class WorkerError(RuntimeError):
    """Ошибка запуска, команды или остановки исполнителя."""


class UnsupportedAdapterError(WorkerError):
    """Для потока не указан поддерживаемый программный адаптер."""


class SimulatedDeviceError(WorkerError):
    """Намеренно смоделированный сбой измерения."""


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} должно быть целым числом")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} должно быть в диапазоне {minimum}…{maximum}")
    return value


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должно быть числом") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} должно быть конечным числом")
    return result


def _period(settings: dict[str, Any]) -> float:
    value = _finite(settings.get("period_s", 0.25), "period_s")
    if not 0.0 <= value <= 3600.0:
        raise ValueError("period_s должно быть в диапазоне 0…3600")
    return value


class SimulatedAdapter:
    """Базовый интерфейт программного адаптера периодического измерения."""

    adapter_name = "unknown"

    def __init__(self, stream: DeviceStream) -> None:
        self.stream = stream
        self.settings = dict(stream.settings)
        self.period_s = _period(self.settings)
        self.max_consecutive_errors = _integer(
            self.settings.get("max_consecutive_errors", 3),
            "max_consecutive_errors",
            minimum=1,
            maximum=100,
        )
        self._failures_remaining = _integer(
            self.settings.get("failures_remaining", 0),
            "failures_remaining",
            minimum=0,
            maximum=10_000,
        )

    def _inject_failure(self) -> None:
        if self._failures_remaining:
            self._failures_remaining -= 1
            raise SimulatedDeviceError(
                f"Смоделированный сбой {self.adapter_name}; "
                f"осталось сбоев: {self._failures_remaining}"
            )

    def poll(self) -> Sweep | ReceiverObservation:
        raise NotImplementedError

    def execute(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise WorkerError(f"Команда {command!r} не поддерживается {self.adapter_name}")

    def details(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        """Симуляторам не требуется освобождать аппаратные ресурсы."""


class TinySASimulator(SimulatedAdapter):
    """Детерминированный источник свипов в исходных дБм, output всегда off."""

    adapter_name = "tinysa-sim"

    def __init__(
        self,
        stream: DeviceStream,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(stream)
        if stream.role is not DeviceRole.SPECTRUM_ANALYZER:
            raise ValueError("tinysa-sim требует роль spectrum_analyzer")
        if stream.level_unit != "dBm":
            raise ValueError("tinysa-sim выдаёт только исходную единицу dBm")
        self._clock = clock
        self.start_hz = _integer(
            self.settings.get("start_hz", 88_000_000),
            "start_hz",
            minimum=1,
            maximum=20_000_000_000,
        )
        self.stop_hz = _integer(
            self.settings.get("stop_hz", 108_000_000),
            "stop_hz",
            minimum=2,
            maximum=20_000_000_000,
        )
        if self.stop_hz <= self.start_hz:
            raise ValueError("stop_hz должно быть больше start_hz")
        self.points = _integer(
            self.settings.get("points", 201), "points", minimum=2, maximum=4096
        )
        self.rbw_hz = _integer(
            self.settings.get("rbw_hz", 10_000),
            "rbw_hz",
            minimum=1,
            maximum=1_000_000_000,
        )
        self.noise_dbm = _finite(self.settings.get("noise_dbm", -100.0), "noise_dbm")
        self.noise_sigma_db = _finite(
            self.settings.get("noise_sigma_db", 1.5), "noise_sigma_db"
        )
        if self.noise_sigma_db < 0:
            raise ValueError("noise_sigma_db не может быть отрицательным")
        self._rng = random.Random(
            _integer(self.settings.get("seed", 7), "seed", maximum=2_147_483_647)
        )
        self._emitters = self._read_emitters(self.settings.get("emitters"))
        self._sequence = 0

    @staticmethod
    def _read_emitters(value: Any) -> list[tuple[int, int, float]]:
        if value is None:
            value = [
                {"frequency_hz": 102_500_000, "bandwidth_hz": 200_000, "level_dbm": -42.0},
                {"frequency_hz": 104_300_000, "bandwidth_hz": 25_000, "level_dbm": -55.0},
            ]
        if not isinstance(value, (list, tuple)):
            raise TypeError("emitters должно быть массивом")
        emitters = []
        for item in value:
            if not isinstance(item, dict):
                raise TypeError("Каждый emitter должен быть объектом")
            emitters.append(
                (
                    _integer(
                        item["frequency_hz"],
                        "emitter.frequency_hz",
                        minimum=1,
                        maximum=20_000_000_000,
                    ),
                    _integer(
                        item["bandwidth_hz"],
                        "emitter.bandwidth_hz",
                        minimum=1,
                        maximum=20_000_000_000,
                    ),
                    _finite(item["level_dbm"], "emitter.level_dbm"),
                )
            )
        return emitters

    def poll(self) -> Sweep:
        started_at = self._clock()
        self._inject_failure()
        span = self.stop_hz - self.start_hz
        frequencies = tuple(
            self.start_hz + round(span * index / (self.points - 1))
            for index in range(self.points)
        )
        levels = [
            self.noise_dbm + self._rng.gauss(0.0, self.noise_sigma_db)
            for _ in frequencies
        ]
        for center_hz, bandwidth_hz, peak_dbm in self._emitters:
            half = max(bandwidth_hz / 2.0, 1.0)
            for index, frequency_hz in enumerate(frequencies):
                distance = abs(frequency_hz - center_hz)
                if distance > half:
                    continue
                profile_db = 30.0 * (distance / half) ** 2
                levels[index] = max(levels[index], peak_dbm - profile_db)
        ended_at = self._clock()
        self._sequence += 1
        return Sweep(
            session_id=self.stream.session_id,
            stream_id=self.stream.id,
            interval=TimeInterval(started_at, ended_at),
            frequencies_hz=frequencies,
            levels=tuple(levels),
            level_unit="dBm",
            settings={
                "adapter": self.adapter_name,
                "rbw_hz": self.rbw_hz,
                "sequence": self._sequence,
                "simulated": True,
                "generator_output": "off",
            },
        )

    def details(self) -> dict[str, Any]:
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "points": self.points,
            "generator_output": "off",
        }


class ARDV1Simulator(SimulatedAdapter):
    """Симулятор настройки и наблюдений с исходными отсчётами S-метра."""

    adapter_name = "ardv1-sim"
    DIGITAL_MODES = {"DMR", "DPMR", "NXDN", "P25", "DSTAR", "YSF"}

    def __init__(
        self,
        stream: DeviceStream,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(stream)
        if stream.role is not DeviceRole.RECEIVER:
            raise ValueError("ardv1-sim требует роль receiver")
        if stream.level_unit != "s_meter_count":
            raise ValueError("ardv1-sim выдаёт только исходную единицу s_meter_count")
        self._clock = clock
        self.frequency_hz = _integer(
            self.settings.get("frequency_hz", 145_500_000),
            "frequency_hz",
            minimum=1,
            maximum=20_000_000_000,
        )
        self.mode = str(self.settings.get("mode") or "FM").strip().upper()
        if not self.mode:
            raise ValueError("mode не должно быть пустым")
        self.squelch = _integer(
            self.settings.get("squelch", 10), "squelch", maximum=255
        )
        self._rng = random.Random(
            _integer(self.settings.get("seed", 17), "seed", maximum=2_147_483_647)
        )
        self._sequence = 0
        self._tune_sequence = 0

    def execute(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command != "tune":
            return super().execute(command, payload)
        frequency_hz = _integer(
            payload.get("frequency_hz"),
            "frequency_hz",
            minimum=1,
            maximum=20_000_000_000,
        )
        mode = str(payload.get("mode") or self.mode).strip().upper()
        if not mode:
            raise ValueError("mode не должно быть пустым")
        self.frequency_hz = frequency_hz
        self.mode = mode
        self._tune_sequence += 1
        return self.details()

    def poll(self) -> ReceiverObservation:
        started_at = self._clock()
        self._inject_failure()
        level = self._rng.randint(0, 60)
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
            squelch_open=level >= self.squelch,
            decoded_mode=self.mode if self.mode in self.DIGITAL_MODES else None,
            metadata={
                "adapter": self.adapter_name,
                "sequence": self._sequence,
                "tune_sequence": self._tune_sequence,
                "simulated": True,
            },
        )

    def details(self) -> dict[str, Any]:
        return {
            "frequency_hz": self.frequency_hz,
            "mode": self.mode,
            "squelch": self.squelch,
            "tune_sequence": self._tune_sequence,
        }


def create_simulated_adapter(
    stream: DeviceStream, *, clock: Callable[[], datetime] = utc_now
) -> SimulatedAdapter:
    """Создаёт адаптер только по явному settings.adapter, не по имени прибора."""
    adapter = stream.settings.get("adapter")
    if adapter == TinySASimulator.adapter_name:
        return TinySASimulator(stream, clock=clock)
    if adapter == ARDV1Simulator.adapter_name:
        return ARDV1Simulator(stream, clock=clock)
    raise UnsupportedAdapterError(
        f"Поток {stream.id}: поддерживаются только явные адаптеры "
        "tinysa-sim и ardv1-sim; аппаратное управление не реализовано"
    )


class DeviceWorker:
    """Один поток измерений и одна очередь команд для одного DeviceStream."""

    def __init__(
        self,
        sessions: SessionService,
        adapter: SimulatedAdapter,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sessions = sessions
        self.adapter = adapter
        self.session_id = adapter.stream.session_id
        self.stream_id = adapter.stream.id
        self.device = adapter.stream.device
        self._clock = clock
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._state = DeviceState.DISCONNECTED
        self._terminal_state = DeviceState.DISCONNECTED
        self._measurements = 0
        self._commands_completed = 0
        self._consecutive_errors = 0
        self._last_error: Optional[str] = None
        self._last_measurement_id: Optional[str] = None
        self._last_measurement_at: Optional[str] = None
        self._started_at: Optional[str] = None
        self._ended_at: Optional[str] = None
        self._finished = False

    @property
    def alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise WorkerError(f"Исполнитель уже запущен: {self.stream_id}")
            self._stop_event.clear()
            self._finished = False
            self._terminal_state = DeviceState.DISCONNECTED
            self._last_error = None
            self._consecutive_errors = 0
            self._started_at = format_time(self._clock())
            self._ended_at = None
            self._thread = threading.Thread(
                target=self._run,
                name=f"device-worker-{self.stream_id}",
                daemon=True,
            )
            self._set_state(DeviceState.READY)
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            self._reject_pending_commands()
        if thread is None or not thread.is_alive():
            return
        request_stop = getattr(self.adapter, "request_stop", None)
        if request_stop is not None:
            request_stop()
        self._commands.put({"command": "__wake__"})
        if thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise WorkerError(f"Исполнитель не остановился за {timeout} с: {self.stream_id}")

    def submit(
        self, command: str, payload: Optional[dict[str, Any]] = None, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "command": command,
            "payload": dict(payload or {}),
            "done": threading.Event(),
            "result": None,
            "error": None,
        }
        with self._lock:
            if not self.alive or self._stop_event.is_set():
                raise WorkerError(f"Исполнитель не запущен: {self.stream_id}")
            self._commands.put(request)
        if not request["done"].wait(timeout):
            with self._lock:
                if not request["done"].is_set():
                    # Уже начатый I/O не прерываем; ожидающий запрос больше
                    # не должен исполниться после ошибки таймаута у клиента.
                    request["cancelled"] = True
                    raise WorkerError(f"Команда {command!r} не выполнена за {timeout} с")
        if request["error"] is not None:
            raise WorkerError(str(request["error"]))
        return dict(request["result"] or {})

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "stream_id": self.stream_id,
                "device": self.device,
                "adapter": self.adapter.adapter_name,
                "state": self._state.value,
                "alive": bool(self._thread and self._thread.is_alive()),
                "measurements": self._measurements,
                "commands_completed": self._commands_completed,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "last_measurement_id": self._last_measurement_id,
                "last_measurement_at": self._last_measurement_at,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "current": self.adapter.details(),
            }

    def _set_state(self, state: DeviceState) -> None:
        with self._lock:
            if self._state is state:
                return
            self._state = state
        self.sessions.set_stream_state(self.session_id, self.stream_id, state)

    def _run(self) -> None:
        try:
            self._set_state(DeviceState.RUNNING)
            while not self._stop_event.is_set():
                self._drain_commands()
                if self._stop_event.is_set():
                    break
                try:
                    measurement = self.adapter.poll()
                    if isinstance(measurement, Sweep):
                        self.sessions.ingest_sweep(measurement)
                    else:
                        self.sessions.ingest_observation(measurement)
                except Exception as exc:  # ошибка одного прибора изолирована от остальных
                    self._consecutive_errors += 1
                    self._last_error = str(exc)
                    self._set_state(DeviceState.DEGRADED)
                    self.sessions.publish(
                        "device.error",
                        session_id=self.session_id,
                        payload={
                            "stream_id": self.stream_id,
                            "adapter": self.adapter.adapter_name,
                            "error": str(exc),
                            "consecutive_errors": self._consecutive_errors,
                        },
                    )
                    if self._consecutive_errors >= self.adapter.max_consecutive_errors:
                        self._terminal_state = DeviceState.ERROR
                        break
                else:
                    self._consecutive_errors = 0
                    self._measurements += 1
                    self._last_measurement_id = measurement.id
                    self._last_measurement_at = format_time(measurement.interval.ended_at)
                    if self._state is not DeviceState.RUNNING:
                        self._set_state(DeviceState.RUNNING)
                try:
                    request = self._commands.get(timeout=self.adapter.period_s)
                except queue.Empty:
                    continue
                self._execute_command(request)
        except Exception as exc:  # защитная граница потока
            self._last_error = str(exc)
            self._terminal_state = DeviceState.ERROR
            try:
                self.sessions.publish(
                    "device.error",
                    session_id=self.session_id,
                    payload={
                        "stream_id": self.stream_id,
                        "adapter": self.adapter.adapter_name,
                        "error": str(exc),
                        "fatal": True,
                    },
                )
            except Exception:
                pass
        finally:
            self._finish()

    def _finish(self, error: Optional[Exception] = None) -> None:
        """Освобождает адаптер ровно один раз, в том числе до старта потока ОС."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._stop_event.set()
            self._reject_pending_commands()
            if error is not None:
                self._last_error = str(error)
                self._terminal_state = DeviceState.ERROR
        try:
            self.adapter.close()
        except Exception as exc:
            with self._lock:
                self._last_error = (
                    f"{self._last_error}; {exc}" if self._last_error else str(exc)
                )
                self._terminal_state = DeviceState.ERROR
        finally:
            self._ended_at = format_time(self._clock())
            try:
                self._set_state(self._terminal_state)
            except Exception:
                # Служба может уже закрываться; локальное состояние всё равно сохранено.
                pass

    def _drain_commands(self) -> None:
        while not self._stop_event.is_set():
            try:
                request = self._commands.get_nowait()
            except queue.Empty:
                return
            self._execute_command(request)

    def _reject_pending_commands(self) -> None:
        while True:
            try:
                request = self._commands.get_nowait()
            except queue.Empty:
                return
            if request.get("done") is not None:
                request["error"] = f"Исполнитель остановлен: {self.stream_id}"
                request["done"].set()

    def _execute_command(self, request: dict[str, Any]) -> None:
        command = request.get("command")
        if command == "__wake__":
            return
        try:
            with self._lock:
                if self._stop_event.is_set() or request.get("cancelled"):
                    raise WorkerError(f"Команда {command!r} отменена: {self.stream_id}")
            result = self.adapter.execute(command, dict(request.get("payload") or {}))
            self._commands_completed += 1
            if command == "tune":
                self.sessions.publish(
                    "device.tuned",
                    session_id=self.session_id,
                    payload={"stream_id": self.stream_id, **result},
                )
            request["result"] = result
        except Exception as exc:
            request["error"] = str(exc)
        finally:
            done = request.get("done")
            if done is not None:
                done.set()


class _WorkerLifecycle:
    """lock принадлежит только API lifecycle; worker никогда его не захватывает."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Счётчик читается/изменяется только под общим lock реестра.
        self.stop_generation = 0


class DeviceWorkerManager:
    """Реестр независимых исполнителей, связанный с одной SessionService."""

    def __init__(
        self,
        sessions: SessionService,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sessions = sessions
        self._clock = clock
        self._workers: dict[tuple[str, str], DeviceWorker] = {}
        self._lifecycles: dict[tuple[str, str], _WorkerLifecycle] = {}
        self._stopping_sessions: dict[str, int] = {}
        self._starting_ports: dict[str, tuple[str, str]] = {}
        self._pending_starts: set[tuple[str, str]] = set()
        self._closed = False
        self._lock = threading.RLock()

    def capabilities(self) -> dict[str, Any]:
        return {
            "simulated_device_workers": True,
            "independent_worker_commands": True,
            "continuous_simulated_monitoring": True,
            "hardware_control": True,
            "hardware_validation_not_run": True,
            "explicit_serial_adapters": True,
        }

    @property
    def active_count(self) -> int:
        with self._lock:
            workers = list(self._workers.values())
        return sum(worker.alive for worker in workers)

    @property
    def pending_start_count(self) -> int:
        with self._lock:
            return len(self._pending_starts)

    @contextmanager
    def _starting(self, key):
        with self._lock:
            if self._closed:
                raise WorkerError("Реестр исполнителей закрыт")
            self._pending_starts.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._pending_starts.discard(key)

    def _lifecycle(self, key: tuple[str, str]) -> _WorkerLifecycle:
        with self._lock:
            return self._lifecycles.setdefault(key, _WorkerLifecycle())

    def _check_start_allowed(
        self, key: tuple[str, str], lifecycle: _WorkerLifecycle, generation: int
    ) -> None:
        # Вызывается под self._lock; никаких операций с прибором здесь нет.
        if self._closed:
            raise WorkerError("Реестр исполнителей закрыт")
        if self._stopping_sessions.get(key[0]):
            raise WorkerError("Выполняется остановка исполнителей сеанса")
        if lifecycle.stop_generation != generation:
            raise WorkerError(f"Запуск исполнителя отменён остановкой: {key[1]}")

    @contextmanager
    def stopping_session(self, session_id: str) -> Iterator[None]:
        """Запрещает новые старты до завершения остановки и записи состояния сеанса.

        Вложенные/параллельные guards безопасны. Они не удерживают общий lock
        во время I/O; незавершённые старты отменяются даже после таймаута stop.
        """
        with self._lock:
            self._stopping_sessions[session_id] = self._stopping_sessions.get(session_id, 0) + 1
            for key, lifecycle in self._lifecycles.items():
                if key[0] == session_id:
                    lifecycle.stop_generation += 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._stopping_sessions[session_id] - 1
                if remaining:
                    self._stopping_sessions[session_id] = remaining
                else:
                    del self._stopping_sessions[session_id]

    @staticmethod
    def _stream(session: MonitoringSession, stream_id: str) -> DeviceStream:
        stream = next((item for item in session.streams if item.id == stream_id), None)
        if stream is None:
            raise WorkerError(f"Поток не принадлежит сеансу: {stream_id}")
        return stream

    def start_stream(self, session_id: str, stream_id: str) -> dict[str, Any]:
        self._stream(self.sessions.get_session(session_id), stream_id)
        key = (session_id, stream_id)
        lifecycle = self._lifecycle(key)
        with self._lock:
            generation = lifecycle.stop_generation
            self._check_start_allowed(key, lifecycle, generation)
        return self._start_stream(key, lifecycle, generation)

    @staticmethod
    def _serial_identity(stream: DeviceStream) -> Optional[str]:
        if stream.settings.get("adapter") not in ("tinysa-serial", "ardv1-serial"):
            return None
        if not stream.port:
            raise WorkerError("Для физического прибора требуется последовательный порт")
        identity = serial_port_identity(stream.port)
        return identity if identity.upper().startswith("COM") else os.path.realpath(identity)

    @contextmanager
    def _serial_port_guard(self, key: tuple[str, str]) -> Iterator[None]:
        stream = self._stream(self.sessions.get_session(key[0]), key[1])
        port = self._serial_identity(stream)
        if port is None:
            yield
            return
        with self._lock:
            if port in self._starting_ports:
                raise WorkerError("Порт уже открывается другим исполнителем")
            for other_key, worker in self._workers.items():
                if other_key != key and worker.alive and self._serial_identity(worker.adapter.stream) == port:
                    raise WorkerError("Порт уже принадлежит другому исполнителю")
            self._starting_ports[port] = key
        try:
            yield
        finally:
            with self._lock:
                if self._starting_ports.get(port) == key:
                    del self._starting_ports[port]

    def _start_stream(
        self, key: tuple[str, str], lifecycle: _WorkerLifecycle, generation: int
    ) -> dict[str, Any]:
        session_id, stream_id = key
        # Сериализация действует только на один session+stream. В частности,
        # connect и join не удерживают общий lock и не мешают другому прибору.
        with lifecycle.lock, self._starting(key), self._serial_port_guard(key):
            with self._lock:
                self._check_start_allowed(key, lifecycle, generation)
                existing = self._workers.get(key)
            session = self.sessions.get_session(session_id)
            if session.state.value != "running":
                raise WorkerError("Сначала запустите сеанс мониторинга")
            stream = self._stream(session, stream_id)
            if existing and existing.alive:
                raise WorkerError(f"Исполнитель уже запущен: {stream_id}")
            from .serial_instruments import create_device_adapter

            try:
                adapter = create_device_adapter(stream, clock=self._clock)
            except (ValueError, TypeError, OSError) as exc:
                raise WorkerError(str(exc)) from exc
            worker: Optional[DeviceWorker] = None
            try:
                worker = DeviceWorker(self.sessions, adapter, clock=self._clock)
                with self._lock:
                    self._check_start_allowed(key, lifecycle, generation)
                    self._workers[key] = worker
                worker.start()
                with self._lock:
                    self._check_start_allowed(key, lifecycle, generation)
                return worker.status()
            except Exception as exc:
                if worker is None:
                    try:
                        adapter.close()
                    except Exception:
                        pass  # Сохраняем исходную ошибку создания исполнителя.
                else:
                    try:
                        worker.stop()
                    except WorkerError:
                        pass  # Живой worker остаётся видимым, повторный start запрещён.
                    if not worker.alive:
                        worker._finish(exc)
                        with self._lock:
                            if self._workers.get(key) is worker:
                                del self._workers[key]
                raise

    def start_session(self, session_id: str) -> list[dict[str, Any]]:
        session = self.sessions.get_session(session_id)
        if session.state.value != "running":
            raise WorkerError("Сначала запустите сеанс мониторинга")
        # Весь пакет получает tickets до первого I/O. Иначе stop_session между
        # двумя потоками позволил бы старому запросу запустить оставшийся прибор.
        with self._lock:
            admissions = []
            for stream in session.streams:
                key = (session_id, stream.id)
                lifecycle = self._lifecycle(key)
                generation = lifecycle.stop_generation
                self._check_start_allowed(key, lifecycle, generation)
                admissions.append((stream, key, lifecycle, generation))
        results = []
        for stream, key, lifecycle, generation in admissions:
            try:
                results.append(self._start_stream(key, lifecycle, generation))
            except WorkerError as exc:
                # Cancellation invalidates the entire admitted bulk request.
                # Only an isolated device failure may continue to the next one.
                with self._lock:
                    self._check_start_allowed(key, lifecycle, generation)
                # Ошибка открытия/адаптера одного прибора не останавливает остальные.
                results.append(
                    {
                        "session_id": session_id,
                        "stream_id": stream.id,
                        "device": stream.device,
                        "adapter": stream.settings.get("adapter"),
                        "state": DeviceState.ERROR.value,
                        "alive": False,
                        "measurements": 0,
                        "commands_completed": 0,
                        "last_error": str(exc),
                        "current": {},
                    }
                )
        return results

    def _stop_worker(
        self, key: tuple[str, str], lifecycle: _WorkerLifecycle, *, timeout: float = 5.0
    ) -> Optional[DeviceWorker]:
        deadline = time.monotonic() + timeout
        with self._lock:
            lifecycle.stop_generation += 1
        if not lifecycle.lock.acquire(timeout=max(0.0, timeout)):
            raise WorkerError(f"Исполнитель не остановился за {timeout} с: {key[1]}")
        try:
            with self._lock:
                worker = self._workers.get(key)
            if worker is not None:
                worker.stop(timeout=max(0.0, deadline - time.monotonic()))
            return worker
        finally:
            lifecycle.lock.release()

    def stop_stream(
        self, session_id: str, stream_id: str, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        self._stream(self.sessions.get_session(session_id), stream_id)
        key = (session_id, stream_id)
        worker = self._stop_worker(key, self._lifecycle(key), timeout=timeout)
        if worker is None:
            session = self.sessions.get_session(session_id)
            return self._static_status(session, self._stream(session, stream_id))
        return worker.status()

    def stop_session(self, session_id: str) -> list[dict[str, Any]]:
        self.sessions.get_session(session_id)
        with self.stopping_session(session_id):
            with self._lock:
                lifecycles = [
                    (key, lifecycle) for key, lifecycle in self._lifecycles.items()
                    if key[0] == session_id
                ]
            first_error: Optional[Exception] = None
            for key, lifecycle in lifecycles:
                try:
                    self._stop_worker(key, lifecycle)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
            return self.list_workers(session_id)

    def tune(
        self, session_id: str, stream_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = self.sessions.get_session(session_id)
        stream = self._stream(session, stream_id)
        if stream.role is not DeviceRole.RECEIVER:
            raise WorkerError("Перестройка доступна только потоку receiver")
        with self._lock:
            worker = self._workers.get((session_id, stream_id))
        if worker is None:
            raise WorkerError(f"Исполнитель не запущен: {stream_id}")
        tuned = worker.submit("tune", payload)
        return {"tuned": tuned, "worker": worker.status()}

    def list_workers(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        sessions = (
            [self.sessions.get_session(session_id)]
            if session_id is not None
            else self.sessions.list_sessions()
        )
        results = []
        with self._lock:
            workers = dict(self._workers)
        for session in sessions:
            for stream in session.streams:
                worker = workers.get((session.id, stream.id))
                results.append(worker.status() if worker else self._static_status(session, stream))
        return results

    @staticmethod
    def _static_status(
        session: MonitoringSession, stream: DeviceStream
    ) -> dict[str, Any]:
        return {
            "session_id": session.id,
            "stream_id": stream.id,
            "device": stream.device,
            "adapter": stream.settings.get("adapter"),
            "state": stream.state.value,
            "alive": False,
            "measurements": 0,
            "commands_completed": 0,
            "consecutive_errors": 0,
            "last_error": None,
            "last_measurement_id": None,
            "last_measurement_at": None,
            "started_at": None,
            "ended_at": None,
            "current": {},
        }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            lifecycles = list(self._lifecycles.items())
        for key, lifecycle in lifecycles:
            try:
                self._stop_worker(key, lifecycle)
            except WorkerError:
                # close является best-effort; явная остановка по-прежнему сообщает ошибку.
                pass
