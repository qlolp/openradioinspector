"""Явно включаемый автоматический запуск очереди исследований.

Политика является runtime-настройкой: после перезапуска процесса планировщик
снова выключен. Кандидаты по-прежнему создаются оператором или внешним
анализатором; этот модуль не обнаруживает сигналы и не завершает исследования.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    DeviceRole,
    Investigation,
    InvestigationState,
    SessionState,
    format_time,
    utc_now,
)
from .session_service import SessionService


DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 1.0,
    "minimum_priority": 0,
    "receiver_stream_id": None,
    "max_consecutive_failures": 3,
    "retry_delay_seconds": 30.0,
}
POLICY_FIELDS = frozenset(DEFAULT_POLICY)


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
        "last_investigation_id": None,
        "last_result": "disabled",
        "last_error": None,
        "consecutive_failures": 0,
        "successful_dispatches": 0,
        "failed_dispatches": 0,
    }


class AutomaticHandoffScheduler:
    """Независимый scheduler с opt-in политикой для каждого сеанса."""

    def __init__(
        self,
        sessions: SessionService,
        *,
        tune: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        start_background: bool = True,
    ) -> None:
        self.sessions = sessions
        self._start_background = start_background
        self._tune = tune
        self._clock = clock
        self._monotonic = monotonic
        self._policies: dict[str, dict[str, Any]] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._next_run: dict[str, float] = {}
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
                raise RuntimeError("Планировщик уже закрыт")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="automatic-handoff-scheduler",
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
        """Меняет политику только через явный API-запрос и пишет audit event."""
        if not isinstance(payload, dict):
            raise ValueError("Политика должна быть JSON-объектом")
        unknown = set(payload) - POLICY_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля политики: {', '.join(sorted(unknown))}")
        session = self.sessions.get_session(session_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("Планировщик уже закрыт")
            policy = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
            runtime = self._runtime.setdefault(session_id, _runtime())
            self._dispatch_locks.setdefault(session_id, threading.Lock())

        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("enabled должно быть JSON boolean")
            policy["enabled"] = payload["enabled"]
        if "interval_seconds" in payload:
            policy["interval_seconds"] = _number(
                payload["interval_seconds"], "interval_seconds", 0.1, 3600.0
            )
        if "minimum_priority" in payload:
            policy["minimum_priority"] = _integer(
                payload["minimum_priority"], "minimum_priority", 0, 1_000_000
            )
        if "max_consecutive_failures" in payload:
            policy["max_consecutive_failures"] = _integer(
                payload["max_consecutive_failures"],
                "max_consecutive_failures",
                1,
                100,
            )
        if "retry_delay_seconds" in payload:
            policy["retry_delay_seconds"] = _number(
                payload["retry_delay_seconds"], "retry_delay_seconds", 0.1, 86_400.0
            )
        if "receiver_stream_id" in payload:
            receiver_id = payload["receiver_stream_id"]
            if receiver_id is not None and (
                not isinstance(receiver_id, str) or not receiver_id.strip()
            ):
                raise ValueError("receiver_stream_id должно быть непустой строкой или null")
            policy["receiver_stream_id"] = receiver_id

        receiver_id = policy["receiver_stream_id"]
        if receiver_id is not None:
            stream = next((item for item in session.streams if item.id == receiver_id), None)
            if stream is None:
                raise ValueError("receiver_stream_id не принадлежит сеансу")
            if stream.role is not DeviceRole.RECEIVER:
                raise ValueError("Политика может выбирать только receiver")

        with self._lock:
            if self._closed:
                raise RuntimeError("Планировщик уже закрыт")
            previous = self._policies.get(session_id, DEFAULT_POLICY)
            # A validated PATCH must not replay unrelated stale policy fields.
            policy = {**copy.deepcopy(previous), **{key: policy[key] for key in payload}}
            self._policies[session_id] = copy.deepcopy(policy)
            if policy["enabled"] != previous["enabled"]:
                runtime["consecutive_failures"] = 0
                runtime["last_error"] = None
            runtime["last_result"] = "enabled" if policy["enabled"] else "disabled"
            self._next_run[session_id] = (
                self._monotonic() if policy["enabled"] else math.inf
            )
        self.sessions.publish(
            "automatic_handoff.policy_changed",
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
        }

    def run_once(self, session_id: str) -> Optional[Investigation]:
        """Выполняет один проход; одновременные сеансы не делят dispatch lock."""
        with self._lock:
            dispatch_lock = self._dispatch_locks.setdefault(session_id, threading.Lock())
        with dispatch_lock:
            with self._lock:
                policy = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
                if self._closed or not policy["enabled"]:
                    return None
                runtime = self._runtime.setdefault(session_id, _runtime())
                runtime["last_run_at"] = format_time(self._clock())
                self._next_run[session_id] = (
                    self._monotonic() + policy["interval_seconds"]
                )

            session = self.sessions.get_session(session_id)
            if session.state is not SessionState.RUNNING:
                with self._lock:
                    runtime["last_result"] = "session_not_running"
                return None

            investigations = self.sessions.list_investigations(session_id)
            busy = {
                item.target_stream_id
                for item in investigations
                if item.state in (
                    InvestigationState.TUNING,
                    InvestigationState.LISTENING,
                    InvestigationState.DECODING,
                )
            }
            selected = next(
                (
                    item
                    for item in investigations
                    if item.state is InvestigationState.QUEUED
                    and item.priority >= policy["minimum_priority"]
                    and item.target_stream_id not in busy
                    and (
                        policy["receiver_stream_id"] is None
                        or item.target_stream_id == policy["receiver_stream_id"]
                    )
                ),
                None,
            )
            if selected is None:
                with self._lock:
                    runtime["last_result"] = "idle"
                return None

            with self._lock:
                runtime["last_investigation_id"] = selected.id
                runtime["last_result"] = "dispatching"
            try:
                result = self.sessions.start_investigation(
                    session_id, selected.id, tune=self._tune
                )
            except ValueError as exc:
                # Ручной запуск мог занять receiver после выбора записи. Это гонка
                # очереди, а не отказ прибора; circuit breaker её не наказывает.
                with self._lock:
                    runtime["last_result"] = "skipped"
                    runtime["last_error"] = str(exc)
                return None

            if result.state is InvestigationState.LISTENING:
                with self._lock:
                    runtime["last_result"] = "listening"
                    runtime["last_error"] = None
                    runtime["consecutive_failures"] = 0
                    runtime["successful_dispatches"] += 1
                self.sessions.publish(
                    "automatic_handoff.started",
                    session_id=session_id,
                    payload={
                        "investigation_id": result.id,
                        "target_stream_id": result.target_stream_id,
                    },
                )
                return result

            if result.state is InvestigationState.FAILED:
                self._record_failure(session_id, policy, result.error or "Сбой handoff")
            else:
                with self._lock:
                    runtime["last_result"] = result.state.value
            return result

    def _record_failure(
        self, session_id: str, policy: dict[str, Any], error: str
    ) -> None:
        disabled = False
        with self._lock:
            runtime = self._runtime[session_id]
            runtime["last_result"] = "failed"
            runtime["last_error"] = error
            runtime["consecutive_failures"] += 1
            runtime["failed_dispatches"] += 1
            self._next_run[session_id] = (
                self._monotonic() + policy["retry_delay_seconds"]
            )
            if runtime["consecutive_failures"] >= policy["max_consecutive_failures"]:
                current = self._policies[session_id]
                current["enabled"] = False
                runtime["last_result"] = "circuit_breaker"
                self._next_run[session_id] = math.inf
                disabled = True
        if disabled:
            self.sessions.publish(
                "automatic_handoff.disabled",
                session_id=session_id,
                payload={
                    "reason": "max_consecutive_failures",
                    "last_error": error,
                    "consecutive_failures": runtime["consecutive_failures"],
                },
            )

    def _dispatch_session(self, session_id: str) -> None:
        try:
            self.run_once(session_id)
        except Exception as exc:
            # Ошибка архива или неожиданная ошибка одного сеанса не должна
            # завершать планировщик или задерживать остальные сеансы.
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
                        name=f"automatic-handoff-{session_id[:12]}",
                        daemon=True,
                    )
                    self._dispatch_threads.add(dispatch)
                    # Запуск под lock исключает join ещё не запущенного потока в close().
                    dispatch.start()
            self._wake.wait(0.1)
            self._wake.clear()
