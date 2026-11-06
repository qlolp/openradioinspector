"""Служба жизненного цикла совместных сеансов наблюдения.

Первый срез намеренно хранит сеансы в памяти. Персистентный архив будет
подключён к этой же службе отдельным адаптером, без изменения HTTP-контракта.
"""

from __future__ import annotations

import copy
import queue
import threading
from collections import deque
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    API_VERSION,
    CONTRACT_VERSION,
    MonitoringSession,
    SessionEvent,
    to_primitive,
    utc_now,
)


class SessionNotFoundError(KeyError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Сеанс не найден: {session_id}")


class SessionService:
    """Потокобезопасный реестр сеансов и журнал событий платформы."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        history_limit: int = 1000,
        subscriber_queue_size: int = 100,
    ) -> None:
        if history_limit < 1 or subscriber_queue_size < 1:
            raise ValueError("Размеры журнала и очереди должны быть положительными")
        self._clock = clock
        self._history_limit = history_limit
        self._subscriber_queue_size = subscriber_queue_size
        self._sessions: dict[str, MonitoringSession] = {}
        self._events: deque[SessionEvent] = deque(maxlen=history_limit)
        self._subscribers: dict[int, queue.Queue[SessionEvent]] = {}
        self._sequence = 0
        self._subscriber_sequence = 0
        self._lock = threading.RLock()

    @staticmethod
    def capabilities() -> dict[str, Any]:
        """Фактические возможности текущего среза, без обещаний будущих функций."""
        return {
            "api_version": API_VERSION,
            "contract_version": CONTRACT_VERSION,
            "hardware_ownership": "core",
            "session_storage": "memory",
            "persistent_archive": False,
            "clients": ["raspberry-pi-webui", "windows-gui", "macos-swiftui"],
            "features": {
                "monitoring_sessions": True,
                "independent_device_streams": True,
                "session_lifecycle": True,
                "session_event_history": True,
                "sse_replay": True,
                "measurement_ingest": False,
                "candidate_investigations": False,
                "hardware_control": False,
            },
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            running = sum(
                item.state.value == "running" for item in self._sessions.values()
            )
            return {
                "status": "ok",
                "api_version": API_VERSION,
                "contract_version": CONTRACT_VERSION,
                "sessions": len(self._sessions),
                "running_sessions": running,
                "last_event_id": self._sequence,
            }

    def create_session(self, payload: dict[str, Any]) -> MonitoringSession:
        if not isinstance(payload, dict):
            raise TypeError("Тело запроса должно быть JSON-объектом")
        session = MonitoringSession.from_dict(payload)
        with self._lock:
            if session.id in self._sessions:
                raise ValueError(f"Сеанс уже существует: {session.id}")
            self._sessions[session.id] = session
            self._append_event_locked(
                "session.created",
                session_id=session.id,
                payload={"session": to_primitive(session)},
            )
            return copy.deepcopy(session)

    def list_sessions(self) -> list[MonitoringSession]:
        with self._lock:
            items = sorted(
                self._sessions.values(), key=lambda item: (item.created_at, item.id)
            )
            return copy.deepcopy(items)

    def get_session(self, session_id: str) -> MonitoringSession:
        with self._lock:
            return copy.deepcopy(self._get_locked(session_id))

    def start_session(
        self, session_id: str, *, at: Optional[datetime] = None
    ) -> MonitoringSession:
        with self._lock:
            session = self._get_locked(session_id)
            session.start(at or self._clock())
            self._append_event_locked(
                "session.started",
                session_id=session.id,
                payload={"session": to_primitive(session)},
            )
            return copy.deepcopy(session)

    def stop_session(
        self, session_id: str, *, at: Optional[datetime] = None
    ) -> MonitoringSession:
        with self._lock:
            session = self._get_locked(session_id)
            session.stop(at or self._clock())
            self._append_event_locked(
                "session.stopped",
                session_id=session.id,
                payload={"session": to_primitive(session)},
            )
            return copy.deepcopy(session)

    def publish(
        self,
        event_type: str,
        *,
        session_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> SessionEvent:
        """Публикует событие следующих слоёв: измерения, кандидатов и приборов."""
        with self._lock:
            if session_id is not None:
                self._get_locked(session_id)
            return self._append_event_locked(
                event_type, session_id=session_id, payload=payload or {}
            )

    def events(self, *, after: int = 0, limit: int = 100) -> list[SessionEvent]:
        if after < 0 or limit < 1:
            raise ValueError("after должен быть неотрицательным, limit — положительным")
        with self._lock:
            selected = [item for item in self._events if item.sequence > after]
            return copy.deepcopy(selected[:limit])

    def subscribe(self, *, after: int = 0) -> tuple[int, queue.Queue[SessionEvent]]:
        """Создаёт подписку без окна потери между историей и живыми событиями."""
        if after < 0:
            raise ValueError("after должен быть неотрицательным")
        subscriber: queue.Queue[SessionEvent] = queue.Queue(
            maxsize=max(self._subscriber_queue_size, self._history_limit)
        )
        with self._lock:
            for event in self._events:
                if event.sequence > after:
                    subscriber.put_nowait(copy.deepcopy(event))
            self._subscriber_sequence += 1
            subscriber_id = self._subscriber_sequence
            self._subscribers[subscriber_id] = subscriber
        return subscriber_id, subscriber

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def _get_locked(self, session_id: str) -> MonitoringSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(session_id) from exc

    def _append_event_locked(
        self,
        event_type: str,
        *,
        session_id: Optional[str],
        payload: dict[str, Any],
    ) -> SessionEvent:
        self._sequence += 1
        event = SessionEvent(
            sequence=self._sequence,
            event_type=event_type,
            occurred_at=self._clock(),
            session_id=session_id,
            payload=copy.deepcopy(payload),
        )
        self._events.append(event)
        for subscriber in self._subscribers.values():
            self._offer(subscriber, event)
        return copy.deepcopy(event)

    @staticmethod
    def _offer(
        subscriber: queue.Queue[SessionEvent], event: SessionEvent
    ) -> None:
        """Не блокирует приборный поток, если клиент читает SSE слишком медленно."""
        try:
            subscriber.put_nowait(copy.deepcopy(event))
            return
        except queue.Full:
            pass
        try:
            subscriber.get_nowait()
        except queue.Empty:
            pass
        try:
            subscriber.put_nowait(copy.deepcopy(event))
        except queue.Full:
            # Единственный потребитель мог освободить место между операциями;
            # повторное переполнение означает, что событие допустимо пропустить
            # для этого клиента. Полная история остаётся доступна через replay.
            pass
