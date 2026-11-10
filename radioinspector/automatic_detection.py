"""Явно включаемое обнаружение кандидатов в новых спектральных sweeps.

Детектор работает только с исходными уровнями ``dBm``. Он не преобразует
отсчёты S-метра, не ставит исследования в очередь и не настраивает receiver.
Политика и курсор обработки существуют только в текущем процессе.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    Candidate,
    CandidateState,
    DeviceRole,
    SessionState,
    Sweep,
    format_time,
    utc_now,
)
from .session_service import SessionService


DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 1.0,
    "stream_id": None,
    "threshold_dbm": -70.0,
    "dedup_bandwidth_hz": 100_000,
    "cooldown_seconds": 60.0,
    "max_candidates_per_sweep": 10,
}
POLICY_FIELDS = frozenset(DEFAULT_POLICY)
PROTECTED_STATES = frozenset(
    {
        CandidateState.NEW,
        CandidateState.QUEUED,
        CandidateState.INVESTIGATING,
        CandidateState.DISMISSED,
    }
)


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} должно быть конечным числом")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должно быть конечным числом") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} должно быть в диапазоне {minimum}…{maximum}")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} должно быть целым числом")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} должно быть в диапазоне {minimum}…{maximum}")
    return value


def _runtime() -> dict[str, Any]:
    return {
        "last_run_at": None,
        "last_sweep_id": None,
        "last_result": "disabled",
        "last_error": None,
        "sweeps_processed": 0,
        "candidates_created": 0,
        "duplicates_suppressed": 0,
    }


def detect_regions(
    sweep: Sweep, *, threshold_dbm: float, maximum: int
) -> list[dict[str, Any]]:
    """Возвращает непрерывные участки на/выше порога, strongest-first.

    Функция чистая: не меняет sweep и не создаёт записи в архиве. Оценённая
    полоса — расстояние между крайними точками участка; для одной точки
    используется локальный шаг частоты, минимум 1 Гц.
    """
    if sweep.level_unit != "dBm":
        raise ValueError("Автоматическое обнаружение поддерживает только исходные dBm")
    regions: list[tuple[int, int]] = []
    start: Optional[int] = None
    for index, level in enumerate(sweep.levels):
        if level >= threshold_dbm and start is None:
            start = index
        if start is not None and (level < threshold_dbm or index == len(sweep.levels) - 1):
            end = index if level >= threshold_dbm else index - 1
            regions.append((start, end))
            start = None

    detected: list[dict[str, Any]] = []
    for start, end in regions:
        peak_index = max(
            range(start, end + 1), key=lambda index: sweep.levels[index]
        )
        bandwidth = sweep.frequencies_hz[end] - sweep.frequencies_hz[start]
        if bandwidth <= 0:
            neighbours = []
            if start > 0:
                neighbours.append(
                    abs(sweep.frequencies_hz[start] - sweep.frequencies_hz[start - 1])
                )
            if end + 1 < len(sweep.frequencies_hz):
                neighbours.append(
                    abs(sweep.frequencies_hz[end + 1] - sweep.frequencies_hz[end])
                )
            bandwidth = min((item for item in neighbours if item > 0), default=1)
        peak = sweep.levels[peak_index]
        detected.append(
            {
                "center_frequency_hz": sweep.frequencies_hz[peak_index],
                "bandwidth_hz": max(1, bandwidth),
                "peak_level": peak,
                "score": peak - threshold_dbm,
            }
        )
    detected.sort(
        key=lambda item: (-item["peak_level"], item["center_frequency_hz"])
    )
    return detected[:maximum]


class AutomaticCandidateDetector:
    """Независимый opt-in detector с отдельным состоянием для каждого сеанса."""

    def __init__(
        self,
        sessions: SessionService,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        start_background: bool = True,
        history_limit: int = 5000,
    ) -> None:
        self.sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._start_background = start_background
        self._history_limit = max(1000, _integer(history_limit, "history_limit", 100, 100_000))
        self._policies: dict[str, dict[str, Any]] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._next_run: dict[str, float] = {}
        self._cursor: dict[str, int] = {}
        self._recent: dict[str, list[dict[str, Any]]] = {}
        self._dispatch_locks: dict[str, threading.Lock] = {}
        self._dispatching: set[str] = set()
        self._dispatch_threads: set[threading.Thread] = set()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Детектор уже закрыт")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="automatic-candidate-detector",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            dispatch_threads = list(self._dispatch_threads)
        self._wake.set()
        deadline = time.monotonic() + 5.0
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        for dispatch in dispatch_threads:
            if dispatch is threading.current_thread():
                continue
            dispatch.join(timeout=max(0.0, deadline - time.monotonic()))

    def configure(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Меняет runtime-политику и фиксирует границу обработки архива."""
        if not isinstance(payload, dict):
            raise ValueError("Политика должна быть JSON-объектом")
        unknown = set(payload) - POLICY_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля политики: {', '.join(sorted(unknown))}")
        session = self.sessions.get_session(session_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("Детектор уже закрыт")
            previous = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
            policy = copy.deepcopy(previous)
            self._runtime.setdefault(session_id, _runtime())
            self._dispatch_locks.setdefault(session_id, threading.Lock())

        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("enabled должно быть JSON boolean")
            policy["enabled"] = payload["enabled"]
        if "interval_seconds" in payload:
            policy["interval_seconds"] = _number(
                payload["interval_seconds"], "interval_seconds", 0.1, 3600.0
            )
        if "threshold_dbm" in payload:
            policy["threshold_dbm"] = _number(
                payload["threshold_dbm"], "threshold_dbm", -300.0, 100.0
            )
        if "dedup_bandwidth_hz" in payload:
            policy["dedup_bandwidth_hz"] = _integer(
                payload["dedup_bandwidth_hz"], "dedup_bandwidth_hz", 1, 10_000_000_000
            )
        if "cooldown_seconds" in payload:
            policy["cooldown_seconds"] = _number(
                payload["cooldown_seconds"], "cooldown_seconds", 0.0, 86_400.0
            )
        if "max_candidates_per_sweep" in payload:
            policy["max_candidates_per_sweep"] = _integer(
                payload["max_candidates_per_sweep"],
                "max_candidates_per_sweep",
                1,
                100,
            )
        if "stream_id" in payload:
            stream_id = payload["stream_id"]
            if stream_id is not None and (
                not isinstance(stream_id, str) or not stream_id.strip()
            ):
                raise ValueError("stream_id должно быть непустой строкой или null")
            policy["stream_id"] = stream_id

        if policy["stream_id"] is not None:
            stream = next(
                (item for item in session.streams if item.id == policy["stream_id"]),
                None,
            )
            if stream is None:
                raise ValueError("stream_id не принадлежит сеансу")
            if stream.role is not DeviceRole.SPECTRUM_ANALYZER:
                raise ValueError("Политика может выбирать только spectrum_analyzer")
            if stream.level_unit != "dBm":
                raise ValueError("Политика может выбирать только поток с исходными dBm")

        # Validation may block; do not delay a concurrent disable or replay
        # fields from its stale policy snapshot. Only merge supplied fields.
        validated_patch = {key: policy[key] for key in payload}
        with self._lock:
            dispatch_lock = self._dispatch_locks[session_id]
        with dispatch_lock:
            return self._configure_serialized(session_id, validated_patch)

    def _configure_serialized(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Детектор уже закрыт")
            previous = self._policies.get(session_id, DEFAULT_POLICY)
            policy = {**copy.deepcopy(previous), **patch}
            enabling = policy["enabled"] and not previous["enabled"]
        boundary = self.sessions.sweep_ingestion_boundary(session_id) if enabling else None
        with self._lock:
            if self._closed:
                raise RuntimeError("Детектор уже закрыт")
            runtime = self._runtime.setdefault(session_id, _runtime())
            if enabling:
                self._cursor[session_id] = boundary
            self._policies[session_id] = copy.deepcopy(policy)
            runtime["last_result"] = "enabled" if policy["enabled"] else "disabled"
            runtime["last_error"] = None
            self._next_run[session_id] = (
                self._monotonic() if policy["enabled"] else math.inf
            )
            if enabling:
                self._recent[session_id] = []
        self.sessions.publish(
            "automatic_detection.policy_changed",
            session_id=session_id,
            payload={"policy": copy.deepcopy(policy), "source": "operator_api"},
        )
        if policy["enabled"] and self._start_background:
            self.start()
        self._wake.set()
        return self.status(session_id)

    def status(self, session_id: str) -> dict[str, Any]:
        self.sessions.get_session(session_id)
        with self._lock:
            policy = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
            runtime = copy.deepcopy(self._runtime.get(session_id, _runtime()))
            alive = self._thread is not None and self._thread.is_alive()
        return {
            "policy": policy,
            "runtime": runtime,
            "scheduler_alive": alive,
            "policy_persistent": False,
            "restart_requires_opt_in": True,
            "processes_existing_archive": False,
            "automatically_enqueues_investigations": False,
        }

    def run_once(self, session_id: str) -> list[Candidate]:
        """Обрабатывает ещё не виденные sweeps одного сеанса."""
        with self._lock:
            dispatch_lock = self._dispatch_locks.setdefault(session_id, threading.Lock())
        with dispatch_lock:
            with self._lock:
                policy = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
                if self._closed or not policy["enabled"]:
                    return []
                runtime = self._runtime.setdefault(session_id, _runtime())
                runtime["last_run_at"] = format_time(self._clock())
                self._next_run[session_id] = (
                    self._monotonic() + policy["interval_seconds"]
                )

            session = self.sessions.get_session(session_id)
            if session.state is not SessionState.RUNNING:
                with self._lock:
                    runtime["last_result"] = "session_not_running"
                return []
            with self._lock:
                after = self._cursor[session_id]
            pending = self.sessions.sweeps_after_ingestion(
                session_id, after=after, stream_id=policy["stream_id"], limit=1000,
            )
            created: list[Candidate] = []
            duplicates = 0
            processed = 0
            last_sweep_id = None
            for sequence, sweep in pending:
                last_sweep_id = sweep.id
                completed = False
                try:
                    if (
                        policy["stream_id"] is not None
                        and sweep.stream_id != policy["stream_id"]
                    ):
                        completed = True
                        continue
                    stream = next(
                        item for item in session.streams if item.id == sweep.stream_id
                    )
                    if (
                        stream.role is not DeviceRole.SPECTRUM_ANALYZER
                        or stream.level_unit != "dBm"
                    ):
                        completed = True
                        continue
                    regions = detect_regions(
                        sweep,
                        threshold_dbm=policy["threshold_dbm"],
                        maximum=policy["max_candidates_per_sweep"],
                    )
                    existing = self.sessions.list_candidates(session_id)
                    for region in regions:
                        if self._duplicate(
                            session_id, sweep.stream_id, region, existing, policy
                        ):
                            duplicates += 1
                            continue
                        candidate = self.sessions.create_candidate(
                            session_id,
                            {
                                "source_stream_id": sweep.stream_id,
                                "interval": {
                                    "started_at": format_time(sweep.interval.started_at),
                                    "ended_at": format_time(sweep.interval.ended_at),
                                },
                                **region,
                                "level_unit": "dBm",
                            },
                        )
                        created.append(candidate)
                        existing.append(candidate)
                        self._remember(session_id, sweep.stream_id, candidate, policy)
                        self.sessions.publish(
                            "automatic_detection.candidate_created",
                            session_id=session_id,
                            payload={
                                "candidate_id": candidate.id,
                                "sweep_id": sweep.id,
                                "source_stream_id": sweep.stream_id,
                                "center_frequency_hz": candidate.center_frequency_hz,
                                "peak_level": candidate.peak_level,
                                "level_unit": candidate.level_unit,
                            },
                        )
                    processed += 1
                    completed = True
                finally:
                    if completed:
                        with self._lock:
                            self._cursor[session_id] = sequence
            with self._lock:
                if len(pending) == 1000:
                    self._next_run[session_id] = self._monotonic()
                runtime["sweeps_processed"] += processed
                runtime["candidates_created"] += len(created)
                runtime["duplicates_suppressed"] += duplicates
                runtime["last_sweep_id"] = last_sweep_id
                runtime["last_error"] = None
                runtime["last_result"] = (
                    "candidates_created" if created else "idle" if pending else "no_new_sweeps"
                )
            return created

    def _duplicate(
        self,
        session_id: str,
        stream_id: str,
        region: dict[str, Any],
        existing: list[Candidate],
        policy: dict[str, Any],
    ) -> bool:
        frequency = region["center_frequency_hz"]
        tolerance = max(policy["dedup_bandwidth_hz"], region["bandwidth_hz"])
        for candidate in existing:
            if (
                candidate.source_stream_id == stream_id
                and candidate.state in PROTECTED_STATES
                and abs(candidate.center_frequency_hz - frequency)
                <= max(tolerance, candidate.bandwidth_hz or 1)
            ):
                return True
        now = self._monotonic()
        with self._lock:
            recent = self._recent.setdefault(session_id, [])
            recent[:] = [item for item in recent if item["expires_at"] > now]
            return any(
                item["stream_id"] == stream_id
                and abs(item["frequency_hz"] - frequency) <= max(tolerance, item.get("bandwidth_hz", 1))
                for item in recent
            )

    def _remember(
        self,
        session_id: str,
        stream_id: str,
        candidate: Candidate,
        policy: dict[str, Any],
    ) -> None:
        with self._lock:
            self._recent.setdefault(session_id, []).append(
                {
                    "stream_id": stream_id,
                    "frequency_hz": candidate.center_frequency_hz,
                    "bandwidth_hz": candidate.bandwidth_hz or 1,
                    "expires_at": self._monotonic() + policy["cooldown_seconds"],
                }
            )

    def _dispatch_session(self, session_id: str) -> None:
        try:
            self.run_once(session_id)
        except Exception as exc:
            # Один сеанс или архив не должен останавливать остальные проходы.
            with self._lock:
                runtime = self._runtime.setdefault(session_id, _runtime())
                runtime["last_result"] = "scheduler_error"
                runtime["last_error"] = str(exc)
                self._next_run[session_id] = self._monotonic() + 1.0
        finally:
            with self._lock:
                self._dispatching.discard(session_id)
                self._dispatch_threads.discard(threading.current_thread())
            self._wake.set()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                due = [
                    session_id
                    for session_id, policy in self._policies.items()
                    if policy["enabled"]
                    and session_id not in self._dispatching
                    and self._next_run.get(session_id, 0.0) <= self._monotonic()
                ]
                for session_id in due:
                    self._dispatching.add(session_id)
                    dispatch = threading.Thread(
                        target=self._dispatch_session,
                        args=(session_id,),
                        name=f"automatic-detection-{session_id[:12]}",
                        daemon=True,
                    )
                    self._dispatch_threads.add(dispatch)
                    dispatch.start()
            self._wake.wait(0.1)
            self._wake.clear()
