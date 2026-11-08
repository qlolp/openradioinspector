"""Служба совместных сеансов, событий и исходных измерений.

SQLite-архив подключается через ``SQLiteSessionStore``. Без него служба сохраняет
прежний memory-only режим, а ``capabilities`` честно сообщает выбранный режим.
"""

from __future__ import annotations

from dataclasses import replace
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
    Candidate,
    CandidateState,
    DeviceRole,
    DeviceState,
    Investigation,
    InvestigationState,
    MonitoringSession,
    ReceiverObservation,
    SessionEvent,
    SessionState,
    Sweep,
    new_id,
    parse_time,
    to_primitive,
    utc_now,
)
from .frequency_rounding import (
    FrequencyRoundingIncompatibleError, calculate_candidate_frequency_rounding,
    validate_rounding_request,
)
from .serial_ports import serial_port_identity

from .frequency_raster import (
    calculate_frequency_rounding, parse_frequency_raster, validate_rounding_request as validate_raster_request,
)
from .frequency_counter import (
    calculate_frequency_counter, validate_counter_request,
)
from .dvb_parameters import evaluate_dvb_parameters
from .phonogram import PhonogramError, PhonogramRecorder
from .signal_list import (
    SignalListError, build_signal_list, import_candidates, parse_signal_list,
)
from .measurement_map import build_measurement_map, validate_map_request
from .audio_stream import AudioStreamError, AudioStreamHub
from .measurement_results import (
    METHODOLOGIES, describe_catalog, make_result_record,
)
from .session_store import ARCHIVE_WARNING_THRESHOLD, SQLiteSessionStore
from .spectral_mask import calculate_sweep_mask
from .sweep_comparison import compare_sweeps
from .sweep_context import (
    calculate_sweep_context, select_context_neighbours, validate_context_request,
)
from .sweep_average import (
    calculate_sweep_average_change_detection,
    calculate_sweep_average_comparison,
    validate_average_request,
    validate_change_detection_request,
)
from .sweep_threshold import calculate_sweep_threshold, validate_threshold_parameters
from .sweep_three_thresholds import calculate_sweep_three_thresholds, validate_three_thresholds_request
from .sweep_references import (
    MAX_SWEEP_REFERENCES, check_reference_creation, normalize_reference_name,
)


class SessionNotFoundError(KeyError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Сеанс не найден: {session_id}")


class SweepNotFoundError(KeyError):
    def __init__(self, sweep_id: str) -> None:
        self.sweep_id = sweep_id
        super().__init__(f"Свип не найден: {sweep_id}")


class CandidateNotFoundError(KeyError):
    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        super().__init__(f"Кандидат не найден: {candidate_id}")


class InvestigationNotFoundError(KeyError):
    def __init__(self, investigation_id: str) -> None:
        self.investigation_id = investigation_id
        super().__init__(f"Исследование не найдено: {investigation_id}")


OPEN_INVESTIGATION_STATES = frozenset(
    {
        InvestigationState.QUEUED,
        InvestigationState.TUNING,
        InvestigationState.LISTENING,
        InvestigationState.DECODING,
    }
)
RUNTIME_INVESTIGATION_STATES = OPEN_INVESTIGATION_STATES - {InvestigationState.QUEUED}
MAX_MEASUREMENT_BATCH = 100


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
        self._sweep_references: list[dict[str, Any]] = []
        self._observations: list[ReceiverObservation] = []
        self._candidates: dict[str, Candidate] = {}
        self._investigations: dict[str, Investigation] = {}
        self._investigation_sessions: dict[str, str] = {}
        self._measurement_results: list[dict[str, Any]] = []
        self._phonograms: dict[tuple[str, str], tuple[PhonogramRecorder, Any]] = {}
        self._audio_hub = AudioStreamHub()
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
            self._candidates = {item.id: item for item in store.load_candidates()}
            for session_id, investigation in store.load_investigations():
                candidate = self._candidates.get(investigation.candidate_id)
                if candidate is None or candidate.session_id != session_id:
                    raise ValueError(
                        f"Архив исследования {investigation.id} нарушает связь с кандидатом"
                    )
                self._investigations[investigation.id] = investigation
                self._investigation_sessions[investigation.id] = session_id
            events = store.load_events(limit=history_limit)
            self._events.extend(events)
            self._sequence = events[-1].sequence if events else 0
            for investigation_id, session_id in list(self._investigation_sessions.items()):
                current = self._investigations[investigation_id]
                if current.state not in RUNTIME_INVESTIGATION_STATES:
                    continue
                # Команда прибору и поток прослушивания не переживают процесс.
                # Сохраняем прерывание, а не имитируем продолжение исследования.
                investigation = copy.deepcopy(current)
                candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
                investigation.state = InvestigationState.FAILED
                investigation.ended_at = self._clock()
                investigation.error = "Исследование прервано перезапуском службы"
                candidate.state = CandidateState.QUEUED
                event = self._make_event_locked(
                    "investigation.interrupted",
                    session_id=session_id,
                    payload=self._investigation_payload(investigation, candidate),
                )
                store.save_investigation(investigation, candidate=candidate, event=event)
                self._investigations[investigation.id] = investigation
                self._candidates[candidate.id] = candidate
                self._publish_event_locked(event)

    def capabilities(self) -> dict[str, Any]:
        """Фактические возможности текущего экземпляра без обещаний будущих функций."""
        persistent = self._store is not None and self._store.persistent
        return {
            "api_version": API_VERSION,
            "contract_version": CONTRACT_VERSION,
            "hardware_ownership": "core",
            "session_storage": "sqlite" if self._store is not None else "memory",
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
                "sweep_comparison": True,
                "adaptive_sweep_threshold": True,
                "three_sweep_thresholds": True,
                "sweep_context": True,
                "spectral_mask_analysis": True,
                "averaged_sweep_reference": True,
                "averaged_sweep_change_detection": True,
                "named_sweep_references": True,
                "frequency_raster_rounding": True,
                "measurement_results": True,
                "measurement_methodology_catalog": True,
                "frequency_counter": True,
                "dvb_t_evaluation": True,
                "phonogram_pre_history": True,
                "signal_list_exchange": True,
                "measurement_map": True,
                "remote_audio_stream": True,
                "archive_capacity_guard": True,
                "archive_automatic_deletion": False,
                "measurement_batch_ingest": True,
                "measurement_batch_atomic": True,
                "measurement_batch_limit": MAX_MEASUREMENT_BATCH,
                "candidate_investigations": True,
                "candidate_frequency_rounding": True,
                "manual_investigation_handoff": True,
                "semi_automatic_handoff": True,
                "automatic_handoff": True,
                "automatic_handoff_opt_in_required": True,
                "automatic_handoff_policy_persistent": False,
                "automatic_candidate_detection": True,
                "automatic_candidate_detection_opt_in_required": True,
                "automatic_candidate_detection_policy_persistent": False,
                "automatic_investigation_completion": False,
                "hardware_control": False,
            },
        }

    def archive_capacity(self) -> dict[str, Any]:
        """Статус архива; режим без SQLite не имеет метрик дискового объёма."""
        with self._lock:
            if self._store is not None:
                return self._store.archive_capacity()
            return {
                "storage_mode": "memory",
                "database_bytes": None,
                "max_database_bytes": None,
                "remaining_bytes": None,
                "state": "unavailable",
                "warning_threshold": ARCHIVE_WARNING_THRESHOLD,
                "measurement_writes_blocked": False,
                "automatic_deletion": False,
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
                "candidates": len(self._candidates),
                "queued_investigations": sum(
                    item.state is InvestigationState.QUEUED
                    for item in self._investigations.values()
                ),
                "active_investigations": sum(
                    item.state in RUNTIME_INVESTIGATION_STATES
                    for item in self._investigations.values()
                ),
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
        # Creation-only validation: do not reinterpret historical archives or
        # simulator profiles, and never open/enumerate ports to compare names.
        serial_ports: set[str] = set()
        for stream in session.streams:
            if stream.settings.get("adapter") not in ("tinysa-serial", "ardv1-serial"):
                continue
            if not isinstance(stream.port, str) or not stream.port:
                continue
            identity = serial_port_identity(stream.port)
            if identity in serial_ports:
                raise ValueError("Один последовательный порт нельзя назначить двум serial-потокам")
            serial_ports.add(identity)
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
            ended_at = at or self._clock()
            session.stop(ended_at)
            events = [self._make_event_locked(
                "session.stopped", session_id=session_id,
                payload={"session": to_primitive(session)},
            )]
            cancellations = []
            for investigation_id, owner_id in list(self._investigation_sessions.items()):
                current = self._investigations[investigation_id]
                if owner_id != session_id or current.state not in OPEN_INVESTIGATION_STATES:
                    continue
                investigation = copy.deepcopy(current)
                candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
                investigation.state = InvestigationState.CANCELLED
                investigation.ended_at = ended_at
                investigation.error = None
                candidate.state = CandidateState.QUEUED
                event = replace(self._make_event_locked(
                    "investigation.cancelled", session_id=session_id,
                    payload=self._investigation_payload(investigation, candidate),
                ), sequence=self._sequence + len(events) + 1)
                events.append(event)
                cancellations.append((investigation, candidate, event))
            if self._store is not None:
                self._store.save_session_stop(session, event=events[0], cancellations=cancellations)
            # No memory mutation or subscriber notification before the commit.
            self._sessions[session_id] = session
            for investigation, candidate, _event in cancellations:
                self._investigations[investigation.id] = investigation
                self._candidates[candidate.id] = candidate
            for event in events:
                self._publish_event_locked(event)
            return copy.deepcopy(session)

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
    def _batch_payloads(payloads: Any) -> list[Any]:
        if not isinstance(payloads, list):
            raise TypeError("Пакет измерений должен быть JSON-массивом")
        if not 1 <= len(payloads) <= MAX_MEASUREMENT_BATCH:
            raise ValueError(
                f"Пакет должен содержать от 1 до {MAX_MEASUREMENT_BATCH} измерений"
            )
        return payloads

    def ingest_sweeps(self, payloads: list[dict[str, Any] | Sweep]) -> list[Sweep]:
        """Атомарно принимает пакет свипов и публикует события после commit."""
        items = self._batch_payloads(payloads)
        sweeps = [item if isinstance(item, Sweep) else Sweep.from_dict(item) for item in items]
        with self._lock:
            batch_ids: set[str] = set()
            for sweep in sweeps:
                self._validate_measurement_locked(
                    sweep.session_id, sweep.stream_id, sweep.level_unit
                )
                if sweep.id in batch_ids:
                    raise ValueError(f"Дубликат ID внутри пакета: {sweep.id}")
                batch_ids.add(sweep.id)
                if self._store is None and any(item.id == sweep.id for item in self._sweeps):
                    raise ValueError(f"Свип уже существует: {sweep.id}")
            events = [
                SessionEvent(
                    sequence=self._sequence + offset,
                    event_type="sweep.received",
                    occurred_at=self._clock(),
                    session_id=sweep.session_id,
                    payload={"sweep": to_primitive(sweep)},
                )
                for offset, sweep in enumerate(sweeps, start=1)
            ]
            try:
                if self._store is not None:
                    self._store.record_sweeps(sweeps, events=events)
                else:
                    self._sweeps.extend(copy.deepcopy(sweeps))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Пакет свипов содержит дубликат или нарушает связи") from exc
            for event in events:
                self._publish_event_locked(event)
            return copy.deepcopy(sweeps)

    def ingest_observations(
        self, payloads: list[dict[str, Any] | ReceiverObservation]
    ) -> list[ReceiverObservation]:
        """Атомарно принимает пакет наблюдений и публикует события после commit."""
        items = self._batch_payloads(payloads)
        observations = [
            item if isinstance(item, ReceiverObservation) else ReceiverObservation.from_dict(item)
            for item in items
        ]
        with self._lock:
            batch_ids: set[str] = set()
            for observation in observations:
                self._validate_measurement_locked(
                    observation.session_id, observation.stream_id, observation.level_unit
                )
                if observation.id in batch_ids:
                    raise ValueError(f"Дубликат ID внутри пакета: {observation.id}")
                batch_ids.add(observation.id)
                if self._store is None and any(
                    item.id == observation.id for item in self._observations
                ):
                    raise ValueError(f"Наблюдение уже существует: {observation.id}")
            events = [
                SessionEvent(
                    sequence=self._sequence + offset,
                    event_type="observation.received",
                    occurred_at=self._clock(),
                    session_id=observation.session_id,
                    payload={"observation": to_primitive(observation)},
                )
                for offset, observation in enumerate(observations, start=1)
            ]
            try:
                if self._store is not None:
                    self._store.record_observations(observations, events=events)
                else:
                    self._observations.extend(copy.deepcopy(observations))
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "Пакет наблюдений содержит дубликат или нарушает связи"
                ) from exc
            for event in events:
                self._publish_event_locked(event)
            return copy.deepcopy(observations)

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit должен быть целым от 1 до 1000")
        return limit

    def archive_measurements(
        self, session_id: str, *, stream_id: Optional[str] = None
    ) -> tuple[list[Sweep], list[ReceiverObservation]]:
        """Возвращает оба типа измерений одним снимком без окна для writer."""
        with self._lock:
            sweeps = self.list_sweeps(session_id, stream_id=stream_id, limit=None)
            observations = self.list_observations(
                session_id, stream_id=stream_id, limit=None
            )
            return sweeps, observations

    def compare_sweeps(
        self, session_id: str, *, reference_id: str, target_id: str
    ) -> dict[str, Any]:
        """Compare exact archived IDs, independent of recent-list/event limits."""
        for name, value in (("reference_id", reference_id), ("target_id", target_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} должен быть непустым ID свипа")
        with self._lock:
            self._get_locked(session_id)

            reference = self._get_sweep_locked(session_id, reference_id)
            target = reference if target_id == reference_id else self._get_sweep_locked(session_id, target_id)
            return compare_sweeps(session_id, reference, target)

    def sweep_average_comparison(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Read every exact source under one lock, without archive scans or writes."""
        reference_ids, target_id = validate_average_request(payload)
        with self._lock:
            self._get_locked(session_id)
            references = [self._get_sweep_locked(session_id, sweep_id) for sweep_id in reference_ids]
            target = self._get_sweep_locked(session_id, target_id)
            return calculate_sweep_average_comparison(session_id, references, target)

    def sweep_average_change_detection(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Read exact sources under one lock and detect regions without writes."""
        reference_ids, target_id, minimum_increase_db, minimum_bins = (
            validate_change_detection_request(payload)
        )
        with self._lock:
            self._get_locked(session_id)
            references = [
                self._get_sweep_locked(session_id, sweep_id) for sweep_id in reference_ids
            ]
            target = self._get_sweep_locked(session_id, target_id)
            return calculate_sweep_average_change_detection(
                session_id, references, target,
                minimum_increase_db=minimum_increase_db, minimum_bins=minimum_bins,
            )

    def sweep_threshold(
        self, session_id: str, *, sweep_id: str, window_points: int, margin_db: float,
    ) -> dict[str, Any]:
        """Analyze one exact archived ID without writes, scans or device work."""
        if not isinstance(sweep_id, str) or not sweep_id.strip():
            raise ValueError("sweep_id должен быть непустым ID свипа")
        window_points, margin_db = validate_threshold_parameters(window_points, margin_db)
        with self._lock:
            self._get_locked(session_id)
            sweep = self._get_sweep_locked(session_id, sweep_id)
            return calculate_sweep_threshold(
                session_id, sweep, window_points=window_points, margin_db=margin_db,
            )

    def sweep_three_thresholds(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Evaluate three independent lines over one exact ID without writes."""
        sweep_id, levels_dbm = validate_three_thresholds_request(payload)
        with self._lock:
            self._get_locked(session_id)
            sweep = self._get_sweep_locked(session_id, sweep_id)
            return calculate_sweep_three_thresholds(session_id, sweep, levels_dbm=levels_dbm)

    def sweep_context(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Read actual nearest neighbours without writes or recent-list limits."""
        sweep_id, before, after = validate_context_request(payload)
        with self._lock:
            self._get_locked(session_id)
            if self._store is not None:
                window = self._store.get_sweep_context(
                    session_id, sweep_id, before=before, after=after,
                )
                if window is None:
                    raise SweepNotFoundError(sweep_id)
                anchor, preceding, following = window
            else:
                anchor = self._get_sweep_locked(session_id, sweep_id)
                preceding, following = select_context_neighbours(
                    anchor, self._sweeps, before=before, after=after,
                )
            return calculate_sweep_context(
                session_id, anchor, preceding, following, before=before, after=after,
            )

    def sweep_mask_analysis(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Evaluate an imported mask against one exact ID, without persistence."""
        if not isinstance(payload, dict) or set(payload) != {"sweep_id", "mask_json"}:
            raise ValueError("Разрешены только поля sweep_id и mask_json")
        sweep_id = payload["sweep_id"]
        if not isinstance(sweep_id, str) or not sweep_id.strip():
            raise ValueError("sweep_id должен быть непустым ID свипа")
        if not isinstance(payload["mask_json"], str):
            raise ValueError("mask_json должен быть строкой JSON UTF-8")
        with self._lock:
            self._get_locked(session_id)
            sweep = self._get_sweep_locked(session_id, sweep_id)
            return calculate_sweep_mask(session_id, sweep, mask_json=payload["mask_json"])

    def frequency_rounding(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Round archived candidate frequencies by an imported table, without writes."""
        table_json, candidate_ids = validate_raster_request(payload)
        # The table is validated before any lookup: a malformed artifact must
        # not depend on archive contents to be rejected.
        table = parse_frequency_raster(table_json)
        with self._lock:
            self._get_locked(session_id)
            candidates = [
                self._get_candidate_locked(session_id, candidate_id)
                for candidate_id in candidate_ids
            ]
            return calculate_frequency_rounding(session_id, candidates, table)

    def frequency_counter(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Measure the peak frequency of one archived sweep, without writes."""
        request = validate_counter_request(payload)
        with self._lock:
            self._get_locked(session_id)
            sweep = self._get_sweep_locked(session_id, request["sweep_id"])
            return calculate_frequency_counter(
                session_id, sweep,
                resolution_hz=request["resolution_hz"],
                start_hz=request["start_hz"], stop_hz=request["stop_hz"],
            )

    def dvb_evaluation(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Evaluate DVB-T parameters against explicit thresholds, without writes."""
        with self._lock:
            self._get_locked(session_id)
            result = evaluate_dvb_parameters(payload)
            return {"session_id": session_id, **result}

    def _phonogram_recorder(self, session_id: str, stream_id: str) -> Any:
        """Runtime recorder per session and stream; nothing enters the archive."""
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id должен быть непустым ID потока")
        session = self._get_locked(session_id)
        if not any(item.id == stream_id for item in session.streams):
            raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")
        key = (session_id, stream_id)
        if key not in self._phonograms:
            self._phonograms[key] = (PhonogramRecorder(), None)
        return self._phonograms[key][0]

    def phonogram_push(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Подать отсчёты аудиопотока: в буфер предыстории и сетевым клиентам."""
        if not isinstance(payload, dict) or set(payload) != {"stream_id", "samples"}:
            raise ValueError("Разрешены только поля stream_id и samples")
        samples = payload["samples"]
        if not isinstance(samples, list) or not samples or len(samples) > 4800:
            raise ValueError("samples должен содержать от 1 до 4800 отсчётов")
        with self._lock:
            recorder = self._phonogram_recorder(session_id, payload["stream_id"])
            recorder.push(samples)
            # Delivery to network clients happens after the recorder accepts
            # the block; a slow client only ever loses its own queue tail.
            self._audio_hub.publish(session_id, payload["stream_id"], samples)
            return recorder.status()

    def audio_stream_status(self, session_id: str, stream_id: str) -> dict[str, Any]:
        with self._lock:
            self._phonogram_recorder(session_id, stream_id)
            return self._audio_hub.status(session_id, stream_id)

    def audio_stream_subscribe(self, session_id: str, stream_id: str):
        """Подписка на сетевую трансляцию; HTTP-слой пишет блоки клиенту."""
        with self._lock:
            self._phonogram_recorder(session_id, stream_id)
            return self._audio_hub.subscribe(session_id, stream_id)

    def audio_stream_unsubscribe(self, session_id: str, stream_id: str, subscription) -> None:
        self._audio_hub.unsubscribe(session_id, stream_id, subscription)

    def phonogram_start(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Начать запись фонограммы; окно буфера становится предысторией."""
        if not isinstance(payload, dict) or set(payload) != {"stream_id"}:
            raise ValueError("Разрешено только поле stream_id")
        with self._lock:
            recorder = self._phonogram_recorder(session_id, payload["stream_id"])
            recorder.start()
            return {"session_id": session_id, "stream_id": payload["stream_id"], **recorder.status()}

    def phonogram_stop(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Завершить запись: предыстория и записанная часть в одном результате."""
        if not isinstance(payload, dict) or set(payload) != {"stream_id"}:
            raise ValueError("Разрешено только поле stream_id")
        with self._lock:
            recorder = self._phonogram_recorder(session_id, payload["stream_id"])
            recorder.stop()
            return {"session_id": session_id, "stream_id": payload["stream_id"], **recorder.status()}

    def phonogram_status(self, session_id: str, stream_id: str) -> dict[str, Any]:
        with self._lock:
            recorder = self._phonogram_recorder(session_id, stream_id)
            return {"session_id": session_id, "stream_id": stream_id, **recorder.status()}

    def phonogram_wav(self, session_id: str, stream_id: str) -> bytes:
        """WAV последней фонограммы; запись в архив не выполняется."""
        with self._lock:
            recorder = self._phonogram_recorder(session_id, stream_id)
            phonogram = recorder.completed()
        return phonogram.wav_bytes()

    def measurement_map(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Build a measurement map from supplied coordinates; read-only."""
        request = validate_map_request(payload)
        with self._lock:
            self._get_locked(session_id)
            result = build_measurement_map(request["name"], request["points"])
            return {"session_id": session_id, **result}

    def signal_list_export(self, session_id: str, name: str) -> dict[str, Any]:
        """Export archived candidates as a portable signal list; read-only."""
        with self._lock:
            self._get_locked(session_id)
            candidates = [
                item for item in self._candidates.values()
                if item.session_id == session_id
            ]
            candidates.sort(key=lambda item: (item.interval.started_at, item.id))
            artifact = build_signal_list(candidates, name)
            return {
                "session_id": session_id,
                "exported": len(artifact["entries"]),
                "list": artifact,
            }

    def signal_list_import(
        self, session_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create new candidates from an imported signal list.

        This is an explicit write through the normal candidate lifecycle:
        each entry becomes one ``new`` candidate on the chosen stream, with
        its own creation event. Levels are never converted.
        """
        if not isinstance(payload, dict) or set(payload) != {"stream_id", "list_json"}:
            raise ValueError("Разрешены только поля stream_id и list_json")
        stream_id = payload["stream_id"]
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id должен быть непустым ID потока")
        artifact = parse_signal_list(payload["list_json"])
        with self._lock:
            session = self._get_locked(session_id)
            stream = next((item for item in session.streams if item.id == stream_id), None)
            if stream is None:
                raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")
            payloads = import_candidates(stream_id, stream.level_unit, artifact)
            candidates = [Candidate.from_dict(dict(item, session_id=session_id, state="new"))
                          for item in payloads]
            created = [candidate.id for candidate in candidates]
            if len(set(created)) != len(created) or any(identifier in self._candidates for identifier in created):
                raise ValueError("Повторяющийся ID кандидата")
            events = []
            for index, candidate in enumerate(candidates, 1):
                self._validate_measurement_locked(session_id, candidate.source_stream_id, candidate.level_unit)
                events.append(SessionEvent(sequence=self._sequence + index, event_type="candidate.created",
                                           occurred_at=self._clock(), session_id=session_id,
                                           payload={"candidate": to_primitive(candidate)}))
            if self._store is not None:
                self._store.save_candidates_batch(list(zip(candidates, events)))
            for candidate, event in zip(candidates, events):
                self._candidates[candidate.id] = candidate
                self._publish_event_locked(event)
            return {
                "session_id": session_id, "stream_id": stream_id,
                "imported": len(created), "candidate_ids": created,
                "name": artifact["name"],
            }

    def save_measurement_result(
        self, session_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Записывает результат измерения с явной методикой и кодом результата.

        Значения хранятся отдельно от ``result_code``; исходные измерения
        не изменяются — это производная запись результата.
        """
        stream_id = payload.get("stream_id") if isinstance(payload, dict) else None
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id должен быть непустым ID потока")
        body = {key: value for key, value in payload.items() if key != "stream_id"}
        record = make_result_record(session_id, stream_id, body, new_id())
        with self._lock:
            session = self._get_locked(session_id)
            if not any(item.id == stream_id for item in session.streams):
                raise ValueError(f"Поток не принадлежит сеансу: {stream_id}")
            event = self._make_event_locked(
                "measurement_result.saved",
                session_id=session_id,
                payload={"measurement_result": to_primitive(record)},
            )
            if self._store is not None:
                self._store.save_measurement_result(record, event=event)
            else:
                self._measurement_results.append(copy.deepcopy(record))
            self._publish_event_locked(event)
            return copy.deepcopy(record)

    def list_measurement_results(
        self, session_id: str, *, methodology: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> list[dict[str, Any]]:
        if methodology is not None and methodology not in METHODOLOGIES:
            raise ValueError(f"Неизвестная методика: {methodology}")
        if limit is not None:
            limit = self._limit(limit)
        with self._lock:
            self._get_locked(session_id)
            if self._store is not None:
                items = self._store.list_measurement_results(
                    session_id, methodology=methodology, limit=limit,
                )
            else:
                items = [
                    item for item in self._measurement_results
                    if item["session_id"] == session_id
                    and (methodology is None or item["methodology"] == methodology)
                ]
                items.sort(key=lambda item: (item["created_at"], item["id"]))
                if limit is not None:
                    items = items[-limit:]
            return copy.deepcopy(items)

    def measurement_catalog(self) -> list[dict[str, Any]]:
        """Каталог методик: id, название, таблица оригинала и поля значений."""
        return describe_catalog()

    def _get_sweep_locked(self, session_id: str, sweep_id: str) -> Sweep:
        if self._store is not None:
            sweep = self._store.get_sweep(sweep_id, session_id=session_id)
        else:
            sweep = next((item for item in self._sweeps
                          if item.id == sweep_id and item.session_id == session_id), None)
        if sweep is None:
            raise SweepNotFoundError(sweep_id)
        return sweep

    def list_sweep_references(
        self, session_id: str, *, stream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_stream_query_locked(session_id, stream_id)
            if self._store is not None:
                items = self._store.list_sweep_references(session_id, stream_id=stream_id)
            else:
                items = [item for item in self._sweep_references
                         if item["session_id"] == session_id
                         and (stream_id is None or item["stream_id"] == stream_id)]
                items.sort(key=lambda item: (parse_time(item["created_at"]), item["id"]))
            return copy.deepcopy({
                "session_id": session_id, "stream_id": stream_id,
                "storage_mode": "sqlite" if self._store is not None else "memory",
                "persistent": self._store is not None and self._store.persistent,
                "limit": MAX_SWEEP_REFERENCES, "items": items,
            })

    def create_sweep_reference(
        self, session_id: str, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Capture immutable archive provenance; replay returns (existing, False)."""
        if not isinstance(payload, dict) or set(payload) != {"name", "sweep_id"}:
            raise ValueError("Нужны только поля name и sweep_id")
        name = normalize_reference_name(payload["name"])
        sweep_id = payload["sweep_id"]
        if not isinstance(sweep_id, str) or not sweep_id.strip():
            raise ValueError("sweep_id должен быть непустым ID свипа")
        with self._lock:
            self._get_locked(session_id)
            sweep = self._get_sweep_locked(session_id, sweep_id)
            # Reuse the scientific compatibility gate unchanged. No separate
            # client/catalog implementation of settings or point-limit rules.
            compare_sweeps(session_id, sweep, sweep)
            existing = check_reference_creation(
                self.list_sweep_references(session_id)["items"], name, sweep_id,
            )
            if existing is not None:
                return existing, False
            reference = {
                "id": new_id(), "session_id": session_id, "stream_id": sweep.stream_id,
                "sweep_id": sweep.id, "name": name, "created_at": to_primitive(self._clock()),
                "interval": to_primitive(sweep.interval), "level_unit": sweep.level_unit,
                "points": len(sweep.frequencies_hz),
            }
            event = self._make_event_locked(
                "sweep_reference.created", session_id=session_id,
                payload={"reference": reference},
            )
            if self._store is not None:
                reference, created = self._store.save_sweep_reference(reference, event=event)
                if not created:
                    return copy.deepcopy(reference), False
            else:
                self._sweep_references.append(copy.deepcopy(reference))
            self._publish_event_locked(event)
            return copy.deepcopy(reference), True

    def list_sweeps(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: Optional[int] = 100
    ) -> list[Sweep]:
        if limit is not None:
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
            return copy.deepcopy(selected if limit is None else selected[-limit:])

    def list_observations(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: Optional[int] = 100
    ) -> list[ReceiverObservation]:
        if limit is not None:
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
            return copy.deepcopy(selected if limit is None else selected[-limit:])

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

    def create_candidate(
        self, session_id: str, payload: dict[str, Any] | Candidate
    ) -> Candidate:
        """Создаёт кандидата в исходных единицах его приборного потока."""
        if isinstance(payload, Candidate):
            candidate = copy.deepcopy(payload)
            if candidate.state is not CandidateState.NEW:
                raise ValueError("Новый кандидат должен иметь состояние new")
        else:
            if not isinstance(payload, dict):
                raise TypeError("Тело запроса должно быть JSON-объектом")
            if payload.get("state", "new") != "new":
                raise ValueError("Новый кандидат должен иметь состояние new")
            supplied_session = payload.get("session_id")
            if supplied_session is not None and supplied_session != session_id:
                raise ValueError("session_id в теле не совпадает с маршрутом")
            creation = dict(payload)
            creation["session_id"] = session_id
            creation["state"] = "new"
            candidate = Candidate.from_dict(creation)
        if candidate.session_id != session_id:
            raise ValueError("Кандидат принадлежит другому сеансу")
        with self._lock:
            self._validate_measurement_locked(
                session_id, candidate.source_stream_id, candidate.level_unit
            )
            references = candidate.observation_ids
            if len(references) > 1000 or any(not isinstance(item, str) or not item for item in references):
                raise ValueError("Допустимо до 1000 непустых ID исходных измерений")
            if references:
                if self._store is not None:
                    owners = self._store.measurement_owners(references)
                else:
                    owners = {identifier: set() for identifier in references}
                    for item in (*self._sweeps, *self._observations):
                        if item.id in owners:
                            owners[item.id].add((item.session_id, item.stream_id))
                expected_owner = {(session_id, candidate.source_stream_id)}
                if any(owners[identifier] != expected_owner for identifier in references):
                    raise ValueError("Исходное измерение отсутствует или принадлежит другому сеансу/потоку")
            if candidate.id in self._candidates:
                raise ValueError(f"Кандидат уже существует: {candidate.id}")
            event = self._make_event_locked(
                "candidate.created",
                session_id=session_id,
                payload={"candidate": to_primitive(candidate)},
            )
            try:
                if self._store is not None:
                    self._store.save_candidate(candidate, event=event)
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Кандидат уже существует или нарушает связи: {candidate.id}"
                ) from exc
            self._candidates[candidate.id] = candidate
            self._publish_event_locked(event)
            return copy.deepcopy(candidate)

    def list_candidates(
        self, session_id: str, *, state: CandidateState | str | None = None
    ) -> list[Candidate]:
        selected_state = CandidateState(state) if state is not None else None
        with self._lock:
            self._get_locked(session_id)
            items = [
                item for item in self._candidates.values()
                if item.session_id == session_id
                and (selected_state is None or item.state is selected_state)
            ]
            items.sort(key=lambda item: (item.interval.started_at, item.id))
            return copy.deepcopy(items)

    def get_candidate(self, session_id: str, candidate_id: str) -> Candidate:
        with self._lock:
            return copy.deepcopy(self._get_candidate_locked(session_id, candidate_id))

    def candidate_frequency_rounding(self, session_id: str, payload: dict) -> dict[str, Any]:
        """Derive an exact candidate frequency without writes or handoff changes."""
        candidate_id, profile_json = validate_rounding_request(payload)
        with self._lock:
            candidate = self._get_candidate_locked(session_id, candidate_id)
            if candidate.id != candidate_id:
                raise FrequencyRoundingIncompatibleError("Исходный ID кандидата не совпадает с запросом")
            return calculate_candidate_frequency_rounding(
                session_id, candidate, profile_json=profile_json,
            )

    def dismiss_candidate(self, session_id: str, candidate_id: str) -> Candidate:
        with self._lock:
            current = self._get_candidate_locked(session_id, candidate_id)
            if current.state not in (CandidateState.NEW, CandidateState.QUEUED):
                raise ValueError(
                    f"Нельзя отклонить кандидата из состояния {current.state.value}"
                )
            if any(
                item.candidate_id == candidate_id and item.state in OPEN_INVESTIGATION_STATES
                for item in self._investigations.values()
            ):
                raise ValueError("Сначала завершите или отмените открытое исследование")
            candidate = copy.deepcopy(current)
            candidate.state = CandidateState.DISMISSED
            return self._save_candidate_locked(candidate, "candidate.dismissed")

    def enqueue_investigation(
        self, session_id: str, candidate_id: str, payload: dict[str, Any]
    ) -> Investigation:
        if not isinstance(payload, dict):
            raise TypeError("Тело запроса должно быть JSON-объектом")
        for field in (
            "state", "created_at", "started_at", "ended_at",
            "result_observation_ids", "error",
        ):
            if payload.get(field) is not None:
                raise ValueError(f"{field} назначается службой жизненного цикла")
        with self._lock:
            session = self._get_locked(session_id)
            if session.state.value in ("stopped", "failed"):
                raise ValueError("Завершённый сеанс не принимает новые исследования")
            current = self._get_candidate_locked(session_id, candidate_id)
            if current.state not in (CandidateState.NEW, CandidateState.QUEUED):
                raise ValueError(
                    f"Нельзя поставить кандидата из состояния {current.state.value}"
                )
            if any(
                item.candidate_id == candidate_id and item.state in OPEN_INVESTIGATION_STATES
                for item in self._investigations.values()
            ):
                raise ValueError("Для кандидата уже существует открытое исследование")
            target_stream_id = str(payload["target_stream_id"])
            target = next(
                (item for item in session.streams if item.id == target_stream_id), None
            )
            if target is None:
                raise ValueError(f"Поток не принадлежит сеансу: {target_stream_id}")
            if target.role is not DeviceRole.RECEIVER:
                raise ValueError("Исследование может настраивать только поток receiver")
            investigation = Investigation(
                id=str(payload.get("id") or new_id()),
                candidate_id=candidate_id,
                target_stream_id=target_stream_id,
                target_frequency_hz=payload.get(
                    "target_frequency_hz", current.center_frequency_hz
                ),
                priority=payload.get("priority", 0),
                mode=payload.get("mode"),
                created_at=self._clock(),
            )
            if investigation.id in self._investigations:
                raise ValueError(f"Исследование уже существует: {investigation.id}")
            candidate = copy.deepcopy(current)
            candidate.state = CandidateState.QUEUED
            event = self._make_event_locked(
                "investigation.queued",
                session_id=session_id,
                payload=self._investigation_payload(investigation, candidate),
            )
            try:
                if self._store is not None:
                    self._store.save_investigation(
                        investigation, candidate=candidate, event=event
                    )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Исследование уже существует или нарушает связи: {investigation.id}"
                ) from exc
            self._investigations[investigation.id] = investigation
            self._investigation_sessions[investigation.id] = session_id
            self._candidates[candidate.id] = candidate
            self._publish_event_locked(event)
            return copy.deepcopy(investigation)

    def list_investigations(
        self, session_id: str, *, state: InvestigationState | str | None = None
    ) -> list[Investigation]:
        selected_state = InvestigationState(state) if state is not None else None
        with self._lock:
            self._get_locked(session_id)
            items = [
                item for item in self._investigations.values()
                if self._investigation_sessions[item.id] == session_id
                and (selected_state is None or item.state is selected_state)
            ]
            items.sort(
                key=lambda item: (
                    item.state is not InvestigationState.QUEUED,
                    -item.priority if item.state is InvestigationState.QUEUED else 0,
                    item.created_at,
                    item.id,
                )
            )
            return copy.deepcopy(items)

    def get_investigation(self, session_id: str, investigation_id: str) -> Investigation:
        with self._lock:
            return copy.deepcopy(
                self._get_investigation_locked(session_id, investigation_id)
            )

    def next_investigation(self, session_id: str) -> Optional[Investigation]:
        """Возвращает рекомендуемое исследование без изменения очереди."""
        with self._lock:
            self._get_locked(session_id)
            return copy.deepcopy(self._next_investigation_locked(session_id))

    def _next_investigation_locked(self, session_id: str) -> Optional[Investigation]:
        queued = [
            item for item in self._investigations.values()
            if self._investigation_sessions[item.id] == session_id
            and item.state is InvestigationState.QUEUED
        ]
        queued.sort(key=lambda item: (-item.priority, item.created_at, item.id))
        return queued[0] if queued else None

    def start_next_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        tune: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    ) -> Investigation:
        """Запускает подтверждённую оператором рекомендацию, если очередь не изменилась."""
        if not isinstance(investigation_id, str) or not investigation_id.strip():
            raise ValueError("Для подтверждения требуется investigation_id")
        return self._start_investigation(
            session_id, investigation_id, tune=tune, require_next=True
        )

    def start_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        tune: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    ) -> Investigation:
        """Выполняет ручной handoff, не удерживая общий lock на время tune."""
        return self._start_investigation(session_id, investigation_id, tune=tune)

    def _start_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        tune: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        require_next: bool = False,
    ) -> Investigation:
        with self._lock:
            session = self._get_locked(session_id)
            if session.state is not SessionState.RUNNING:
                raise ValueError("Сначала запустите сеанс мониторинга")
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.state is not InvestigationState.QUEUED:
                raise ValueError(
                    f"Нельзя запустить исследование из состояния {current.state.value}"
                )
            if require_next:
                recommended = self._next_investigation_locked(session_id)
                if recommended is None:
                    raise ValueError("Очередь исследований пуста")
                if recommended.id != investigation_id:
                    raise ValueError(
                        "Очередь изменилась: запросите и подтвердите актуальное "
                        "следующее исследование"
                    )
            if any(
                item.id != investigation_id
                and self._investigation_sessions[item.id] == session_id
                and item.target_stream_id == current.target_stream_id
                and item.state in RUNTIME_INVESTIGATION_STATES
                for item in self._investigations.values()
            ):
                raise ValueError("Выбранный receiver уже выполняет другое исследование")
            candidate = copy.deepcopy(self._candidates[current.candidate_id])
            investigation = copy.deepcopy(current)
            investigation.state = InvestigationState.TUNING
            investigation.started_at = self._clock()
            investigation.ended_at = None
            investigation.error = None
            self._save_investigation_locked(
                session_id, investigation, candidate, "investigation.tuning"
            )
            tune_payload = {"frequency_hz": investigation.target_frequency_hz}
            if investigation.mode is not None:
                tune_payload["mode"] = investigation.mode
            target_stream_id = investigation.target_stream_id

        try:
            tune(session_id, target_stream_id, tune_payload)
        except Exception as exc:
            with self._lock:
                current = self._get_investigation_locked(session_id, investigation_id)
                if current.state is not InvestigationState.TUNING:
                    return copy.deepcopy(current)
                investigation = copy.deepcopy(current)
                candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
                investigation.state = InvestigationState.FAILED
                investigation.ended_at = self._clock()
                investigation.error = str(exc) or type(exc).__name__
                candidate.state = CandidateState.QUEUED
                return self._save_investigation_locked(
                    session_id, investigation, candidate, "investigation.failed"
                )

        with self._lock:
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.state is not InvestigationState.TUNING:
                # Отмена во время команды не отменяет уже выполненную перестройку
                # прибора и не должна быть перезаписана состоянием listening.
                return copy.deepcopy(current)
            investigation = copy.deepcopy(current)
            candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
            investigation.state = InvestigationState.LISTENING
            candidate.state = CandidateState.INVESTIGATING
            return self._save_investigation_locked(
                session_id, investigation, candidate, "investigation.listening"
            )

    def matching_observation_ids(
        self, session_id: str, investigation_id: str, *, limit: Optional[int] = None
    ) -> tuple[str, ...]:
        """Read-only отбор наблюдений цели без изменения исследования."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit должен быть положительным целым или None")
        with self._lock:
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.started_at is None:
                return ()
            if self._store is not None:
                return self._store.matching_observation_ids(
                    session_id,
                    current.target_stream_id,
                    current.target_frequency_hz,
                    current.started_at,
                    limit=limit,
                )
            matches = [
                item for item in self._observations
                if item.session_id == session_id
                and item.stream_id == current.target_stream_id
                and item.frequency_hz == current.target_frequency_hz
                and item.interval.started_at >= current.started_at
            ]
            matches.sort(key=lambda item: (item.interval.started_at, item.id))
            selected = matches if limit is None else matches[:limit]
            return tuple(item.id for item in selected)

    def complete_investigation(
        self, session_id: str, investigation_id: str
    ) -> Investigation:
        with self._lock:
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.state not in (
                InvestigationState.LISTENING, InvestigationState.DECODING
            ):
                raise ValueError(
                    f"Нельзя завершить исследование из состояния {current.state.value}"
                )
            if current.started_at is None:
                raise ValueError("У активного исследования отсутствует started_at")
            if self._store is not None:
                observation_ids = self._store.matching_observation_ids(
                    session_id, current.target_stream_id,
                    current.target_frequency_hz, current.started_at,
                )
            else:
                matches = [
                    item for item in self._observations
                    if item.session_id == session_id
                    and item.stream_id == current.target_stream_id
                    and item.frequency_hz == current.target_frequency_hz
                    and item.interval.started_at >= current.started_at
                ]
                matches.sort(key=lambda item: (item.interval.started_at, item.id))
                observation_ids = tuple(item.id for item in matches)
            if not observation_ids:
                raise ValueError(
                    "Нет наблюдений целевого потока на выбранной частоте после начала исследования"
                )
            investigation = copy.deepcopy(current)
            candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
            investigation.state = InvestigationState.COMPLETED
            investigation.ended_at = self._clock()
            investigation.result_observation_ids = observation_ids
            investigation.error = None
            candidate.state = CandidateState.RESOLVED
            return self._save_investigation_locked(
                session_id, investigation, candidate, "investigation.completed"
            )

    def fail_investigation(
        self, session_id: str, investigation_id: str, error: str
    ) -> Investigation:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("Для сбоя исследования требуется непустое описание error")
        with self._lock:
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.state not in OPEN_INVESTIGATION_STATES:
                raise ValueError(
                    f"Нельзя завершить сбоем состояние {current.state.value}"
                )
            investigation = copy.deepcopy(current)
            candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
            investigation.state = InvestigationState.FAILED
            investigation.ended_at = self._clock()
            investigation.error = error.strip()
            candidate.state = CandidateState.QUEUED
            return self._save_investigation_locked(
                session_id, investigation, candidate, "investigation.failed"
            )

    def cancel_investigation(
        self, session_id: str, investigation_id: str
    ) -> Investigation:
        with self._lock:
            current = self._get_investigation_locked(session_id, investigation_id)
            if current.state not in OPEN_INVESTIGATION_STATES:
                raise ValueError(
                    f"Нельзя отменить исследование из состояния {current.state.value}"
                )
            investigation = copy.deepcopy(current)
            candidate = copy.deepcopy(self._candidates[investigation.candidate_id])
            investigation.state = InvestigationState.CANCELLED
            investigation.ended_at = self._clock()
            investigation.error = None
            candidate.state = CandidateState.QUEUED
            return self._save_investigation_locked(
                session_id, investigation, candidate, "investigation.cancelled"
            )

    def _get_candidate_locked(self, session_id: str, candidate_id: str) -> Candidate:
        self._get_locked(session_id)
        candidate = self._candidates.get(candidate_id)
        if candidate is None or candidate.session_id != session_id:
            raise CandidateNotFoundError(candidate_id)
        return candidate

    def _get_investigation_locked(
        self, session_id: str, investigation_id: str
    ) -> Investigation:
        self._get_locked(session_id)
        investigation = self._investigations.get(investigation_id)
        if (
            investigation is None
            or self._investigation_sessions.get(investigation_id) != session_id
        ):
            raise InvestigationNotFoundError(investigation_id)
        return investigation

    @staticmethod
    def _investigation_payload(
        investigation: Investigation, candidate: Candidate
    ) -> dict[str, Any]:
        return {
            "investigation": to_primitive(investigation),
            "candidate": to_primitive(candidate),
        }

    def _save_candidate_locked(
        self, candidate: Candidate, event_type: str
    ) -> Candidate:
        event = self._make_event_locked(
            event_type,
            session_id=candidate.session_id,
            payload={"candidate": to_primitive(candidate)},
        )
        if self._store is not None:
            self._store.save_candidate(candidate, event=event)
        self._candidates[candidate.id] = candidate
        self._publish_event_locked(event)
        return copy.deepcopy(candidate)

    def _save_investigation_locked(
        self,
        session_id: str,
        investigation: Investigation,
        candidate: Candidate,
        event_type: str,
    ) -> Investigation:
        event = self._make_event_locked(
            event_type,
            session_id=session_id,
            payload=self._investigation_payload(investigation, candidate),
        )
        if self._store is not None:
            self._store.save_investigation(
                investigation, candidate=candidate, event=event
            )
        self._investigations[investigation.id] = investigation
        self._investigation_sessions[investigation.id] = session_id
        self._candidates[candidate.id] = candidate
        self._publish_event_locked(event)
        return copy.deepcopy(investigation)

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
