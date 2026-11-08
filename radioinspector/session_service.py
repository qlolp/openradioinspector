"""Служба совместных сеансов, событий и исходных измерений.

SQLite-архив подключается через ``SQLiteSessionStore``. Без него служба сохраняет
прежний memory-only режим, а ``capabilities`` честно сообщает выбранный режим.
"""

from __future__ import annotations

import copy
import queue
import sqlite3
import threading
from collections import deque
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (
    API_VERSION,
    CONTRACT_VERSION,
    DeviceState,
    MonitoringSession,
    ReceiverObservation,
    SessionEvent,
    Sweep,
    to_primitive,
    utc_now,
)
from .session_store import SQLiteSessionStore


class SessionNotFoundError(KeyError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Сеанс не найден: {session_id}")


class SessionService:
    """Потокобезопасный реестр сеансов, измерений и журнала событий."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        history_limit: int = 1000,
        subscriber_queue_size: int = 100,
        store: Optional[SQLiteSessionStore] = None,
    ) -> None:
        if history_limit < 1 or subscriber_queue_size < 1:
            raise ValueError("Размеры журнала и очереди должны быть положительными")
        self._clock = clock
        self._history_limit = history_limit
        self._subscriber_queue_size = subscriber_queue_size
        self._store = store
        self._sessions: dict[str, MonitoringSession] = {}
        self._events: deque[SessionEvent] = deque(maxlen=history_limit)
        self._sweeps: list[Sweep] = []
        self._observations: list[ReceiverObservation] = []
        self._subscribers: dict[int, queue.Queue[SessionEvent]] = {}
        self._sequence = 0
        self._subscriber_sequence = 0
        self._lock = threading.RLock()
        if store is not None:
            restored = store.load_sessions()
            for session in restored:
                runtime_state_changed = False
                for stream in session.streams:
                    if stream.state is not DeviceState.DISCONNECTED:
                        # Потоки ОС не переживают перезапуск процесса. Сеанс и
                        # измерения восстанавливаются, но исполнители не запускаются
                        # автоматически и не должны выглядеть работающими.
                        stream.state = DeviceState.DISCONNECTED
                        runtime_state_changed = True
                if runtime_state_changed:
                    store.save_session(session)
            self._sessions = {item.id: item for item in restored}
            events = store.load_events(limit=history_limit)
            self._events.extend(events)
            self._sequence = events[-1].sequence if events else 0

    def capabilities(self) -> dict[str, Any]:
        """Фактические возможности текущего экземпляра без обещаний будущих функций."""
        persistent = self._store is not None
        return {
            "api_version": API_VERSION,
            "contract_version": CONTRACT_VERSION,
            "hardware_ownership": "core",
            "session_storage": "sqlite" if persistent else "memory",
            "persistent_archive": persistent,
            "clients": ["raspberry-pi-webui", "windows-gui", "macos-swiftui"],
            "features": {
                "monitoring_sessions": True,
                "independent_device_streams": True,
                "session_lifecycle": True,
                "session_event_history": True,
                "sse_replay": True,
                "session_recovery": persistent,
                "measurement_ingest": True,
                "measurement_archive": True,
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
        if payload.get("state", "draft") != "draft":
            raise ValueError("Новый сеанс должен иметь состояние draft")
        for field in ("created_at", "started_at", "ended_at"):
            if payload.get(field) is not None:
                raise ValueError(f"{field} назначается службой жизненного цикла")
        creation = dict(payload)
        creation["created_at"] = to_primitive(self._clock())
        session = MonitoringSession.from_dict(creation)
        with self._lock:
            if session.id in self._sessions:
                raise ValueError(f"Сеанс уже существует: {session.id}")
            existing_ids = {
                stream.id for item in self._sessions.values() for stream in item.streams
            }
            for stream in session.streams:
                if stream.id in existing_ids:
                    raise ValueError(f"Поток уже принадлежит другому сеансу: {stream.id}")
            event = self._make_event_locked(
                "session.created",
                session_id=session.id,
                payload={"session": to_primitive(session)},
            )
            if self._store is not None:
                self._store.save_session(session, event=event)
            self._sessions[session.id] = session
            self._publish_event_locked(event)
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
            session = copy.deepcopy(self._get_locked(session_id))
            session.start(at or self._clock())
            return self._save_transition_locked(session, "session.started")

    def stop_session(
        self, session_id: str, *, at: Optional[datetime] = None
    ) -> MonitoringSession:
        with self._lock:
            session = copy.deepcopy(self._get_locked(session_id))
            session.stop(at or self._clock())
            return self._save_transition_locked(session, "session.stopped")

    def _save_transition_locked(
        self, session: MonitoringSession, event_type: str
    ) -> MonitoringSession:
        event = self._make_event_locked(
            event_type, session_id=session.id, payload={"session": to_primitive(session)}
        )
        if self._store is not None:
            self._store.save_session(session, event=event)
        self._sessions[session.id] = session
        self._publish_event_locked(event)
        return copy.deepcopy(session)

    def set_stream_state(
        self, session_id: str, stream_id: str, state: DeviceState | str
    ) -> MonitoringSession:
        """Сохраняет фактическое состояние потока и публикует его изменение."""
        state = DeviceState(state)
        with self._lock:
            current = self._get_locked(session_id)
            stream = next((item for item in current.streams if item.id == stream_id), None)
            if stream is None:
                raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")
            if stream.state is state:
                return copy.deepcopy(current)
            session = copy.deepcopy(current)
            stream = next(item for item in session.streams if item.id == stream_id)
            stream.state = state
            event = self._make_event_locked(
                "device.state",
                session_id=session_id,
                payload={"stream_id": stream_id, "state": state.value},
            )
            if self._store is not None:
                self._store.save_session(session, event=event)
            self._sessions[session_id] = session
            self._publish_event_locked(event)
            return copy.deepcopy(session)

    def ingest_sweep(self, payload: dict[str, Any] | Sweep) -> Sweep:
        sweep = payload if isinstance(payload, Sweep) else Sweep.from_dict(payload)
        with self._lock:
            self._validate_measurement_locked(
                sweep.session_id, sweep.stream_id, sweep.level_unit
            )
            if self._store is None and any(item.id == sweep.id for item in self._sweeps):
                raise ValueError(f"Свип уже существует: {sweep.id}")
            event = self._make_event_locked(
                "sweep.received",
                session_id=sweep.session_id,
                payload={"sweep": to_primitive(sweep)},
            )
            try:
                if self._store is not None:
                    self._store.record_sweep(sweep, event=event)
                else:
                    self._sweeps.append(copy.deepcopy(sweep))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Свип уже существует или нарушает связи: {sweep.id}") from exc
            self._publish_event_locked(event)
            return copy.deepcopy(sweep)

    def ingest_observation(
        self, payload: dict[str, Any] | ReceiverObservation
    ) -> ReceiverObservation:
        observation = (
            payload
            if isinstance(payload, ReceiverObservation)
            else ReceiverObservation.from_dict(payload)
        )
        with self._lock:
            self._validate_measurement_locked(
                observation.session_id, observation.stream_id, observation.level_unit
            )
            if self._store is None and any(
                item.id == observation.id for item in self._observations
            ):
                raise ValueError(f"Наблюдение уже существует: {observation.id}")
            event = self._make_event_locked(
                "observation.received",
                session_id=observation.session_id,
                payload={"observation": to_primitive(observation)},
            )
            try:
                if self._store is not None:
                    self._store.record_observation(observation, event=event)
                else:
                    self._observations.append(copy.deepcopy(observation))
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Наблюдение уже существует или нарушает связи: {observation.id}"
                ) from exc
            self._publish_event_locked(event)
            return copy.deepcopy(observation)

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit должен быть целым от 1 до 1000")
        return limit

    def list_sweeps(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: int = 100
    ) -> list[Sweep]:
        limit = self._limit(limit)
        with self._lock:
            self._validate_stream_query_locked(session_id, stream_id)
            if self._store is not None:
                return self._store.list_sweeps(session_id, stream_id=stream_id, limit=limit)
            selected = [
                item for item in self._sweeps
                if item.session_id == session_id
                and (stream_id is None or item.stream_id == stream_id)
            ]
            selected.sort(key=lambda item: (item.interval.started_at, item.id))
            return copy.deepcopy(selected[-limit:])

    def list_observations(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: int = 100
    ) -> list[ReceiverObservation]:
        limit = self._limit(limit)
        with self._lock:
            self._validate_stream_query_locked(session_id, stream_id)
            if self._store is not None:
                return self._store.list_observations(
                    session_id, stream_id=stream_id, limit=limit
                )
            selected = [
                item for item in self._observations
                if item.session_id == session_id
                and (stream_id is None or item.stream_id == stream_id)
            ]
            selected.sort(key=lambda item: (item.interval.started_at, item.id))
            return copy.deepcopy(selected[-limit:])

    def _validate_stream_query_locked(
        self, session_id: str, stream_id: Optional[str]
    ) -> None:
        session = self._get_locked(session_id)
        if stream_id is not None and not any(
            item.id == stream_id for item in session.streams
        ):
            raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")

    def _validate_measurement_locked(
        self, session_id: str, stream_id: str, level_unit: str
    ) -> None:
        session = self._get_locked(session_id)
        stream = next((item for item in session.streams if item.id == stream_id), None)
        if stream is None:
            raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")
        if stream.level_unit != level_unit:
            raise ValueError(
                f"Единица измерения {level_unit!r} не совпадает с потоком "
                f"{stream.level_unit!r}; автоматическое преобразование запрещено"
            )

    def publish(
        self,
        event_type: str,
        *,
        session_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> SessionEvent:
        """Публикует служебное событие приборных исполнителей и адаптеров."""
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
            if self._store is not None:
                return self._store.events(after=after, limit=limit)
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

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()

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
        event = self._make_event_locked(
            event_type, session_id=session_id, payload=payload
        )
        if self._store is not None:
            self._store.append_event(event)
        return self._publish_event_locked(event)

    def _make_event_locked(
        self,
        event_type: str,
        *,
        session_id: Optional[str],
        payload: dict[str, Any],
    ) -> SessionEvent:
        return SessionEvent(
            sequence=self._sequence + 1,
            event_type=event_type,
            occurred_at=self._clock(),
            session_id=session_id,
            payload=copy.deepcopy(payload),
        )

    def _publish_event_locked(self, event: SessionEvent) -> SessionEvent:
        """Обновляет replay и подписчиков только после успешного commit архива."""
        self._sequence = event.sequence
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
