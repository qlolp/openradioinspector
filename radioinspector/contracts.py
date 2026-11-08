"""Версионированный контракт общей платформы OpenRadioInspector.

Модели не зависят от HTTP, конкретного UI и физического оборудования. Они
служат общей границей между службой получения данных, архивом и клиентами
Raspberry Pi, Windows и macOS.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

CONTRACT_NAME = "openradioinspector-platform"
CONTRACT_VERSION = 1
API_VERSION = "v1"


def utc_now() -> datetime:
    """Текущее время с явной зоной UTC."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime, field_name: str = "время") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} должно быть datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должно содержать часовой пояс")
    return value.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_time(value: str, field_name: str = "время") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должно быть ISO-строкой")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Некорректное {field_name}: {value}") from exc
    return ensure_utc(parsed, field_name)


def new_id() -> str:
    return str(uuid.uuid4())


def to_primitive(value: Any) -> Any:
    """Рекурсивно преобразует модели в стандартные JSON-значения."""
    if isinstance(value, datetime):
        return format_time(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    return value


def api_payload(data: Any) -> dict[str, Any]:
    """Стандартная оболочка ответа API с явной версией контракта."""
    return {
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "data": to_primitive(data),
    }


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} не должно быть пустым")
    return value.strip()


def _hz(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} должно быть целым числом Гц")
    if value < 0:
        raise ValueError(f"{name} не может быть отрицательным")
    return value


def _frequency(value: int, name: str = "frequency_hz") -> int:
    value = _hz(value, name)
    if value == 0:
        raise ValueError(f"{name} должно быть положительным")
    return value


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} должно быть конечным числом")
    return number


class SessionState(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class DeviceState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    ERROR = "error"


class DeviceRole(str, Enum):
    SPECTRUM_ANALYZER = "spectrum_analyzer"
    RECEIVER = "receiver"
    SIMULATOR = "simulator"


class CandidateState(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class InvestigationState(str, Enum):
    QUEUED = "queued"
    TUNING = "tuning"
    LISTENING = "listening"
    DECODING = "decoding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TimeInterval:
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        self.started_at = ensure_utc(self.started_at, "started_at")
        self.ended_at = ensure_utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at не может предшествовать started_at")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TimeInterval":
        return cls(
            started_at=parse_time(payload["started_at"], "started_at"),
            ended_at=parse_time(payload["ended_at"], "ended_at"),
        )


@dataclass
class DeviceStream:
    session_id: str
    device: str
    role: DeviceRole
    level_unit: str
    id: str = field(default_factory=new_id)
    label: str = ""
    port: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    firmware: Optional[str] = None
    state: DeviceState = DeviceState.DISCONNECTED
    capabilities: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session_id = _required_text(self.session_id, "session_id")
        self.device = _required_text(self.device, "device")
        self.role = DeviceRole(self.role)
        self.level_unit = _required_text(self.level_unit, "level_unit")
        self.state = DeviceState(self.state)
        self.capabilities = tuple(str(item) for item in self.capabilities)
        self.settings = dict(self.settings)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DeviceStream":
        return cls(
            session_id=str(payload["session_id"]),
            device=str(payload["device"]),
            role=DeviceRole(payload["role"]),
            level_unit=str(payload["level_unit"]),
            id=str(payload.get("id") or new_id()),
            label=str(payload.get("label") or ""),
            port=payload.get("port"),
            model=payload.get("model"),
            serial_number=payload.get("serial_number"),
            firmware=payload.get("firmware"),
            state=DeviceState(payload.get("state", DeviceState.DISCONNECTED)),
            capabilities=tuple(payload.get("capabilities") or ()),
            settings=dict(payload.get("settings") or {}),
        )


@dataclass
class MonitoringSession:
    name: str
    streams: tuple[DeviceStream, ...] = ()
    id: str = field(default_factory=new_id)
    state: SessionState = SessionState.DRAFT
    created_at: datetime = field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        self.name = _required_text(self.name, "name")
        self.state = SessionState(self.state)
        self.created_at = ensure_utc(self.created_at, "created_at")
        if self.started_at is not None:
            self.started_at = ensure_utc(self.started_at, "started_at")
        if self.ended_at is not None:
            self.ended_at = ensure_utc(self.ended_at, "ended_at")
        self.streams = tuple(
            item if isinstance(item, DeviceStream) else DeviceStream.from_dict(item)
            for item in self.streams
        )
        self.metadata = dict(self.metadata)
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"Неподдерживаемая версия контракта: {self.contract_version}")
        ids = [item.id for item in self.streams]
        if len(ids) != len(set(ids)):
            raise ValueError("Идентификаторы потоков должны быть уникальны")
        ports = [item.port for item in self.streams if item.port]
        if len(ports) != len(set(ports)):
            raise ValueError("Один порт нельзя назначить двум потокам")
        for stream in self.streams:
            if stream.session_id != self.id:
                raise ValueError("Поток принадлежит другому сеансу")

    def start(self, at: Optional[datetime] = None) -> None:
        if self.state is not SessionState.DRAFT:
            raise ValueError(f"Нельзя запустить сеанс из состояния {self.state.value}")
        self.started_at = ensure_utc(at or utc_now(), "started_at")
        self.state = SessionState.RUNNING

    def stop(self, at: Optional[datetime] = None) -> None:
        if self.state is not SessionState.RUNNING:
            raise ValueError(f"Нельзя остановить сеанс из состояния {self.state.value}")
        ended_at = ensure_utc(at or utc_now(), "ended_at")
        if self.started_at and ended_at < self.started_at:
            raise ValueError("ended_at не может предшествовать started_at")
        self.ended_at = ended_at
        self.state = SessionState.STOPPED

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MonitoringSession":
        session_id = str(payload.get("id") or new_id())
        streams = []
        for item in payload.get("streams") or ():
            data = dict(item)
            data["session_id"] = session_id
            streams.append(DeviceStream.from_dict(data))
        return cls(
            name=str(payload["name"]),
            streams=tuple(streams),
            id=session_id,
            state=SessionState(payload.get("state", SessionState.DRAFT)),
            created_at=parse_time(payload["created_at"], "created_at")
            if payload.get("created_at")
            else utc_now(),
            started_at=parse_time(payload["started_at"], "started_at")
            if payload.get("started_at")
            else None,
            ended_at=parse_time(payload["ended_at"], "ended_at")
            if payload.get("ended_at")
            else None,
            metadata=dict(payload.get("metadata") or {}),
            contract_version=int(payload.get("contract_version", CONTRACT_VERSION)),
        )


@dataclass
class Sweep:
    session_id: str
    stream_id: str
    interval: TimeInterval
    frequencies_hz: tuple[int, ...]
    levels: tuple[float, ...]
    level_unit: str
    id: str = field(default_factory=new_id)
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session_id = _required_text(self.session_id, "session_id")
        self.stream_id = _required_text(self.stream_id, "stream_id")
        if not isinstance(self.interval, TimeInterval):
            self.interval = TimeInterval.from_dict(self.interval)
        self.frequencies_hz = tuple(
            _hz(item, "frequencies_hz") for item in self.frequencies_hz
        )
        self.levels = tuple(_finite(item, "level") for item in self.levels)
        self.level_unit = _required_text(self.level_unit, "level_unit")
        self.settings = dict(self.settings)
        if not self.frequencies_hz or len(self.frequencies_hz) != len(self.levels):
            raise ValueError("Частоты и уровни должны быть непустыми массивами одной длины")
        if self.frequencies_hz[0] < 0:
            raise ValueError("Частота не может быть отрицательной")
        if any(b < a for a, b in zip(self.frequencies_hz, self.frequencies_hz[1:])):
            raise ValueError("Частоты свипа должны идти в неубывающем порядке")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Sweep":
        return cls(
            session_id=str(payload["session_id"]),
            stream_id=str(payload["stream_id"]),
            interval=TimeInterval.from_dict(payload["interval"]),
            frequencies_hz=tuple(payload["frequencies_hz"]),
            levels=tuple(payload["levels"]),
            level_unit=str(payload["level_unit"]),
            id=str(payload.get("id") or new_id()),
            settings=dict(payload.get("settings") or {}),
        )


@dataclass
class ReceiverObservation:
    session_id: str
    stream_id: str
    interval: TimeInterval
    frequency_hz: int
    mode: str
    level_unit: str = "s_meter_count"
    id: str = field(default_factory=new_id)
    level: Optional[float] = None
    squelch_open: Optional[bool] = None
    decoded_mode: Optional[str] = None
    audio_uri: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.session_id = _required_text(self.session_id, "session_id")
        self.stream_id = _required_text(self.stream_id, "stream_id")
        if not isinstance(self.interval, TimeInterval):
            self.interval = TimeInterval.from_dict(self.interval)
        self.frequency_hz = _frequency(self.frequency_hz)
        self.mode = _required_text(self.mode, "mode")
        self.level_unit = _required_text(self.level_unit, "level_unit")
        if self.level is not None:
            self.level = _finite(self.level, "level")
        self.metadata = dict(self.metadata)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReceiverObservation":
        return cls(
            session_id=str(payload["session_id"]),
            stream_id=str(payload["stream_id"]),
            interval=TimeInterval.from_dict(payload["interval"]),
            frequency_hz=payload["frequency_hz"],
            mode=str(payload["mode"]),
            level_unit=str(payload.get("level_unit", "s_meter_count")),
            id=str(payload.get("id") or new_id()),
            level=payload.get("level"),
            squelch_open=payload.get("squelch_open"),
            decoded_mode=payload.get("decoded_mode"),
            audio_uri=payload.get("audio_uri"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class Candidate:
    session_id: str
    source_stream_id: str
    interval: TimeInterval
    center_frequency_hz: int
    level_unit: str
    id: str = field(default_factory=new_id)
    bandwidth_hz: Optional[int] = None
    peak_level: Optional[float] = None
    score: Optional[float] = None
    observation_ids: tuple[str, ...] = ()
    state: CandidateState = CandidateState.NEW

    def __post_init__(self) -> None:
        self.session_id = _required_text(self.session_id, "session_id")
        self.source_stream_id = _required_text(self.source_stream_id, "source_stream_id")
        if not isinstance(self.interval, TimeInterval):
            self.interval = TimeInterval.from_dict(self.interval)
        self.center_frequency_hz = _frequency(self.center_frequency_hz, "center_frequency_hz")
        self.level_unit = _required_text(self.level_unit, "level_unit")
        if self.bandwidth_hz is not None:
            self.bandwidth_hz = _frequency(self.bandwidth_hz, "bandwidth_hz")
        if self.peak_level is not None:
            self.peak_level = _finite(self.peak_level, "peak_level")
        if self.score is not None:
            self.score = _finite(self.score, "score")
        self.observation_ids = tuple(str(item) for item in self.observation_ids)
        self.state = CandidateState(self.state)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Candidate":
        return cls(
            session_id=str(payload["session_id"]),
            source_stream_id=str(payload["source_stream_id"]),
            interval=TimeInterval.from_dict(payload["interval"]),
            center_frequency_hz=payload["center_frequency_hz"],
            level_unit=str(payload["level_unit"]),
            id=str(payload.get("id") or new_id()),
            bandwidth_hz=payload.get("bandwidth_hz"),
            peak_level=payload.get("peak_level"),
            score=payload.get("score"),
            observation_ids=tuple(payload.get("observation_ids") or ()),
            state=CandidateState(payload.get("state", CandidateState.NEW)),
        )


@dataclass
class Investigation:
    candidate_id: str
    target_stream_id: str
    target_frequency_hz: int
    id: str = field(default_factory=new_id)
    state: InvestigationState = InvestigationState.QUEUED
    priority: int = 0
    mode: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    result_observation_ids: tuple[str, ...] = ()
    error: Optional[str] = None

    def __post_init__(self) -> None:
        self.candidate_id = _required_text(self.candidate_id, "candidate_id")
        self.target_stream_id = _required_text(self.target_stream_id, "target_stream_id")
        self.target_frequency_hz = _frequency(self.target_frequency_hz, "target_frequency_hz")
        self.state = InvestigationState(self.state)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ValueError("priority должно быть неотрицательным целым")
        self.created_at = ensure_utc(self.created_at, "created_at")
        if self.started_at is not None:
            self.started_at = ensure_utc(self.started_at, "started_at")
        if self.ended_at is not None:
            self.ended_at = ensure_utc(self.ended_at, "ended_at")
        self.result_observation_ids = tuple(str(item) for item in self.result_observation_ids)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Investigation":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            target_stream_id=str(payload["target_stream_id"]),
            target_frequency_hz=payload["target_frequency_hz"],
            id=str(payload.get("id") or new_id()),
            state=InvestigationState(payload.get("state", InvestigationState.QUEUED)),
            priority=int(payload.get("priority", 0)),
            mode=payload.get("mode"),
            created_at=parse_time(payload["created_at"], "created_at")
            if payload.get("created_at")
            else utc_now(),
            started_at=parse_time(payload["started_at"], "started_at")
            if payload.get("started_at")
            else None,
            ended_at=parse_time(payload["ended_at"], "ended_at")
            if payload.get("ended_at")
            else None,
            result_observation_ids=tuple(payload.get("result_observation_ids") or ()),
            error=payload.get("error"),
        )


@dataclass
class SessionEvent:
    sequence: int
    event_type: str
    occurred_at: datetime
    session_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence должно быть положительным целым")
        self.event_type = _required_text(self.event_type, "event_type")
        self.occurred_at = ensure_utc(self.occurred_at, "occurred_at")
        self.payload = dict(self.payload)
