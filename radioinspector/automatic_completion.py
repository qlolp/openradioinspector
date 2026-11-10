"""Opt-in завершение активных исследований после подходящих наблюдений."""

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
    "minimum_listening_seconds": 0.0,
    "minimum_observations": 1,
    "receiver_stream_id": None,
}
POLICY_FIELDS = frozenset(DEFAULT_POLICY)
ACTIVE_STATES = (InvestigationState.LISTENING, InvestigationState.DECODING)


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
        "completed_investigations": 0,
        "skipped_investigations": 0,
    }


class AutomaticInvestigationCompletion:
    """Независимый runtime-планировщик без automatic fail/cancel."""

    def __init__(
        self,
        sessions: SessionService,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        start_background: bool = True,
    ) -> None:
        self.sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._start_background = start_background
        self._policies: dict[str, dict[str, Any]] = {}
        self._policy_versions: dict[str, int] = {}
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
                target=self._run, name="automatic-investigation-completion", daemon=True
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
            if dispatch is not threading.current_thread():
                dispatch.join(timeout=max(0.0, deadline - time.monotonic()))

    def configure(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        if "minimum_listening_seconds" in payload:
            policy["minimum_listening_seconds"] = _number(
                payload["minimum_listening_seconds"],
                "minimum_listening_seconds",
                0.0,
                86_400.0,
            )
        if "minimum_observations" in payload:
            policy["minimum_observations"] = _integer(
                payload["minimum_observations"], "minimum_observations", 1, 1000
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
            self._policies[session_id] = copy.deepcopy(policy)
            self._policy_versions[session_id] = self._policy_versions.get(session_id, 0) + 1
            runtime["last_result"] = "enabled" if policy["enabled"] else "disabled"
            runtime["last_error"] = None
            self._next_run[session_id] = (
                self._monotonic() if policy["enabled"] else math.inf
            )
        self.sessions.publish(
            "automatic_completion.policy_changed",
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
            "automatic_fail_or_cancel": False,
        }

    def run_once(self, session_id: str) -> Optional[Investigation]:
        """Завершает первое подходящее исследование либо возвращает None."""
        with self._lock:
            dispatch_lock = self._dispatch_locks.setdefault(session_id, threading.Lock())
        with dispatch_lock:
            with self._lock:
                policy = copy.deepcopy(self._policies.get(session_id, DEFAULT_POLICY))
                policy_version = self._policy_versions.get(session_id, 0)
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
            for investigation in investigations:
                if investigation.state not in ACTIVE_STATES:
                    continue
                if (
                    policy["receiver_stream_id"] is not None
                    and investigation.target_stream_id != policy["receiver_stream_id"]
                ):
                    continue
                if investigation.started_at is None:
                    continue
                minimum = policy["minimum_listening_seconds"]
                if minimum > 0:
                    # Legacy records have no reliable listening origin. Never
                    # count setup/tuning time or invent a historical timestamp.
                    origin = investigation.listening_started_at
                    if origin is None or (self._clock() - origin).total_seconds() < minimum:
                        continue
                observation_ids = self.sessions.matching_observation_ids(
                    session_id,
                    investigation.id,
                    limit=policy["minimum_observations"],
                )
                if len(observation_ids) < policy["minimum_observations"]:
                    continue
                with self._lock:
                    current_version = self._policy_versions.get(session_id, 0)
                    current_enabled = self._policies.get(session_id, DEFAULT_POLICY)["enabled"]
                    if current_version != policy_version or not current_enabled:
                        runtime["last_result"] = "policy_changed"
                        runtime["skipped_investigations"] += 1
                        return None
                    runtime["last_investigation_id"] = investigation.id
                    runtime["last_result"] = "completing"
                    try:
                        result = self.sessions.complete_investigation(
                            session_id, investigation.id
                        )
                    except ValueError as exc:
                        # Ручное complete/fail/cancel после предварительного выбора —
                        # гонка оператора, а не сбой прибора или архива.
                        runtime["last_result"] = "skipped"
                        runtime["last_error"] = str(exc)
                        runtime["skipped_investigations"] += 1
                        return None
                with self._lock:
                    runtime["last_result"] = "completed"
                    runtime["last_error"] = None
                    runtime["completed_investigations"] += 1
                self.sessions.publish(
                    "automatic_completion.completed",
                    session_id=session_id,
                    payload={
                        "investigation_id": result.id,
                        "target_stream_id": result.target_stream_id,
                        "result_observation_ids": list(result.result_observation_ids),
                    },
                )
                return result
            with self._lock:
                runtime["last_result"] = "idle"
            return None

    def _dispatch_session(self, session_id: str) -> None:
        try:
            self.run_once(session_id)
        except Exception as exc:
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
                        name=f"automatic-completion-{session_id[:12]}",
                        daemon=True,
                    )
                    self._dispatch_threads.add(dispatch)
                    dispatch.start()
            self._wake.wait(0.1)
            self._wake.clear()
