"""Плановые задания радиоконтроля и профиль контроля фиксированных частот.

Исходный комплекс содержит подсистему планирования: оператор создаёт
плановое задание, к заданию сканирования прикрепляется организационно-
административная информация, назначаются дата, время и длительность
выполнения. Задания хранятся в отдельной базе данных планирования и
выполняются автоматически, если в назначенный момент запущена программа
управления средствами измерений.

Документ описывает шесть статусов задания, дополнительные статусы ошибки и
предупреждения, а также профиль контроля фиксированных частот — набор
параметров сканирования и порогов обнаружения, применяемых к каждой
контролируемой частоте.

Модуль реализует модель задания, машину состояний и правила проверки
профиля. Планировщик с таймером и сетевой протокол удалённого контроля
комплексов в объём модуля не входят — см. ``docs/feature-map.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Статусы задания
# ---------------------------------------------------------------------------

#: Задание сформировано, но файл задания на сканирование не выбран.
STATUS_DRAFT = "Сформировано"

#: Задание ожидает наступления назначенного времени.
STATUS_QUEUED = "В очереди на выполнение"

#: Статус зарезервирован и в настоящее время не применяется.
STATUS_CANCELLED = "Отменено"

#: Задание выполняется в текущий момент.
STATUS_RUNNING = "Выполняется"

#: Задание отработало заданное время и нормально завершилось.
STATUS_COMPLETED = "Выполнено"

#: Задание было снято с выполнения до истечения заданного времени.
STATUS_ABORTED = "Снято с выполнения"

TASK_STATUSES = (
    STATUS_DRAFT,
    STATUS_QUEUED,
    STATUS_CANCELLED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_ABORTED,
)

#: Дополнительные статусы ошибки — выделяются красным цветом.
OUTCOME_FILE_NOT_FOUND = "файл задания (на сканирование частот) не найден"
OUTCOME_APPLICATION_CRASH = "аварийное завершение приложения"

#: Дополнительные статусы предупреждения — выделяются синим цветом.
OUTCOME_STOPPED_BY_OPERATOR = "принудительно завершено оператором"
OUTCOME_STOPPED_BY_TASK = "принудительно завершено другой задачей"

ERROR_OUTCOMES = (OUTCOME_FILE_NOT_FOUND, OUTCOME_APPLICATION_CRASH)
WARNING_OUTCOMES = (OUTCOME_STOPPED_BY_OPERATOR, OUTCOME_STOPPED_BY_TASK)


# ---------------------------------------------------------------------------
# Профиль контроля фиксированных частот
# ---------------------------------------------------------------------------

#: Действие: запись фонограммы демодулированного аудиосигнала.
ACTION_PHONOGRAM = "phonogram"

#: Действие: измерение частоты.
ACTION_FREQUENCY = "frequency"

#: Действие: измерение контрольной полосы занимаемых частот и внеполосовых
#: излучений.
ACTION_CONTROL_BANDWIDTH = "control_bandwidth"

#: Минимальный динамический порог для каждого вида действий, дБ.
ACTION_THRESHOLDS_DB = {
    ACTION_PHONOGRAM: 6.0,
    ACTION_FREQUENCY: 20.0,
    ACTION_CONTROL_BANDWIDTH: 30.0,
}

#: Рекомендуемое число точек измерения на одну контролируемую частоту.
POINTS_PER_FREQUENCY_MIN = 7
POINTS_PER_FREQUENCY_MAX = 11

#: Предельное отношение полосы пропускания к необходимой полосе сигнала.
BANDWIDTH_RATIO_MAX = 0.1

#: Диапазон отношения полосы обзора к необходимой полосе сигнала.
SPAN_RATIO_MIN = 1.5
SPAN_RATIO_MAX = 3.0

#: Минимальная доля экрана, которую должен занимать сигнал.
SCREEN_FILL_MIN = 0.5


@dataclass
class FrequencyControlProfile:
    """Профиль контроля фиксированных частот.

    Параметры профиля относятся к каждой контролируемой частоте отдельно:
    число точек измерения, динамический порог обнаружения, полоса
    пропускания и полоса обзора.
    """

    points: int = POINTS_PER_FREQUENCY_MIN
    dynamic_threshold_db: float = ACTION_THRESHOLDS_DB[ACTION_CONTROL_BANDWIDTH]
    action: str = ACTION_CONTROL_BANDWIDTH
    bandwidth_hz: float = 0.0
    signal_bandwidth_hz: float = 0.0
    span_hz: float = 0.0
    screen_fill: float = 1.0
    measurement_limit: int = 0
    phonogram_limit: int = 0

    def validate(self) -> list[str]:
        """Проверка профиля и замечания по параметрам сканирования."""
        warnings: list[str] = []

        if not POINTS_PER_FREQUENCY_MIN <= self.points <= POINTS_PER_FREQUENCY_MAX:
            warnings.append(
                f"Количество точек измерения {self.points} выходит за "
                f"рекомендуемый диапазон "
                f"{POINTS_PER_FREQUENCY_MIN}–{POINTS_PER_FREQUENCY_MAX}"
            )

        required = ACTION_THRESHOLDS_DB.get(self.action)
        if required is not None and self.dynamic_threshold_db < required:
            warnings.append(
                f"Динамический порог {self.dynamic_threshold_db:g} дБ меньше "
                f"требуемого для действия «{self.action}» ({required:g} дБ)"
            )

        if self.signal_bandwidth_hz > 0 and self.bandwidth_hz > 0:
            ratio = self.bandwidth_hz / self.signal_bandwidth_hz
            if ratio > BANDWIDTH_RATIO_MAX:
                warnings.append(
                    f"Полоса пропускания составляет {ratio:.2f} от необходимой "
                    f"полосы сигнала — допустимо не более {BANDWIDTH_RATIO_MAX:g}"
                )
            span_ratio = self.span_hz / self.signal_bandwidth_hz if self.span_hz else 0.0
            if span_ratio and not SPAN_RATIO_MIN <= span_ratio <= SPAN_RATIO_MAX:
                warnings.append(
                    f"Полоса обзора составляет {span_ratio:.2f} от необходимой "
                    f"полосы сигнала — рекомендуемый диапазон "
                    f"{SPAN_RATIO_MIN:g}–{SPAN_RATIO_MAX:g}"
                )

        if self.screen_fill < SCREEN_FILL_MIN:
            warnings.append(
                f"Размах сигнала {self.screen_fill:.0%} экрана меньше "
                f"рекомендуемых {SCREEN_FILL_MIN:.0%}"
            )

        return warnings

    def as_dict(self) -> dict:
        return {
            "points": self.points,
            "dynamic_threshold_db": self.dynamic_threshold_db,
            "action": self.action,
            "bandwidth_hz": self.bandwidth_hz,
            "signal_bandwidth_hz": self.signal_bandwidth_hz,
            "span_hz": self.span_hz,
            "screen_fill": self.screen_fill,
            "measurement_limit": self.measurement_limit,
            "phonogram_limit": self.phonogram_limit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FrequencyControlProfile":
        fields = {key: data[key] for key in data if key in cls.__dataclass_fields__}
        return cls(**fields)


# ---------------------------------------------------------------------------
# Справочники задания
# ---------------------------------------------------------------------------


def _parse_moment(value: Optional[str]) -> Optional[datetime]:
    """Разбор момента времени из строки ISO 8601."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class TaskReference:
    """Запись справочника планового задания.

    Исходный комплекс хранит справочники вида задания, цели, РКП, отдела,
    группы ПА, основания и носителя в отдельных таблицах базы планирования;
    каждая запись справочника имеет собственный идентификатор, выдаваемый
    генератором базы.
    """

    reference_id: int
    name: str
    code: str = ""

    def as_dict(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "name": self.name,
            "code": self.code,
        }


class ReferenceBook:
    """Набор справочников планового задания.

    Идентификаторы выдаются по порядку добавления — так же, как генераторы
    базы планирования в исходном комплексе.
    """

    def __init__(self) -> None:
        self._items: dict[str, list[TaskReference]] = {}

    def add(self, kind: str, name: str, code: str = "") -> TaskReference:
        items = self._items.setdefault(kind, [])
        item = TaskReference(len(items) + 1, name, code)
        items.append(item)
        return item

    def get(self, kind: str, reference_id: int) -> Optional[TaskReference]:
        for item in self._items.get(kind, []):
            if item.reference_id == reference_id:
                return item
        return None

    def name_of(self, kind: str, reference_id: Optional[int]) -> str:
        """Наименование записи справочника по идентификатору."""
        if not reference_id:
            return ""
        item = self.get(kind, reference_id)
        return item.name if item else ""

    def list(self, kind: str) -> list[TaskReference]:
        return list(self._items.get(kind, []))

    def as_dict(self) -> dict:
        return {
            kind: [item.as_dict() for item in items]
            for kind, items in self._items.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceBook":
        book = cls()
        for kind, items in data.items():
            for raw in items:
                book.add(kind, raw.get("name", ""), raw.get("code", ""))
        return book


#: Виды справочников базы планирования.
REFERENCE_KINDS = (
    "task_type",     # вид задания
    "purpose",       # цель
    "rkp",           # радиоконтрольное предприятие
    "department",    # отдел
    "group_pa",      # группа подразделения
    "basis",         # основание
    "carrier",       # носитель результатов
)


# ---------------------------------------------------------------------------
# Список фиксированных частот и действия над обнаруженными сигналами
# ---------------------------------------------------------------------------

#: Действие: измерить параметры сигнала при обнаружении на контролируемой частоте.
ACTION_MEASURE_SIGNAL = "measure_signal"

#: Действие: только зафиксировать факт превышения линии порога.
ACTION_DETECT_ONLY = "detect_only"

#: Действие: записать фонограмму демодулированного аудиосигнала.
ACTION_RECORD_PHONOGRAM = "record_phonogram"

#: Действия, доступные на контролируемой частоте.
FREQUENCY_ACTIONS = (ACTION_DETECT_ONLY, ACTION_MEASURE_SIGNAL, ACTION_RECORD_PHONOGRAM)


@dataclass
class FixedFrequency:
    """Контролируемая частота с условиями выполнения измерений.

    Соответствует записи списка фиксированных частот: частота, полоса
    сигнала, профиль сканирования, действие при обнаружении и примечание.
    """

    frequency_hz: float
    bandwidth_hz: float = 0.0
    action: str = ACTION_DETECT_ONLY
    profile: Optional[FrequencyControlProfile] = None
    comment: str = ""
    from_assignment: bool = False

    def validate(self) -> list[str]:
        """Замечания к записи списка частот."""
        warnings: list[str] = []
        if self.frequency_hz <= 0:
            warnings.append(f"Частота {self.frequency_hz} Гц недопустима")
        if self.action not in FREQUENCY_ACTIONS:
            warnings.append(
                f"Неизвестное действие «{self.action}» — допустимы: "
                + ", ".join(FREQUENCY_ACTIONS)
            )
        if self.profile is not None:
            for message in self.profile.validate():
                warnings.append(f"Частота {self.frequency_hz:g} Гц: {message}")
        return warnings

    def as_dict(self) -> dict:
        return {
            "frequency_hz": self.frequency_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "action": self.action,
            "comment": self.comment,
            "from_assignment": self.from_assignment,
            "profile": self.profile.as_dict() if self.profile else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FixedFrequency":
        profile = data.get("profile")
        return cls(
            frequency_hz=float(data.get("frequency_hz", 0.0)),
            bandwidth_hz=float(data.get("bandwidth_hz", 0.0)),
            action=data.get("action", ACTION_DETECT_ONLY),
            profile=FrequencyControlProfile.from_dict(profile) if profile else None,
            comment=data.get("comment", ""),
            from_assignment=bool(data.get("from_assignment", False)),
        )


class FixedFrequencyList:
    """Задание на контроль фиксированных частот.

    Второй уровень трёхуровневой модели выполнения измерений: оператор
    составляет список контролируемых частот и задаёт для каждой условие
    действия при обнаружении сигнала. Отлаженное задание сохраняется в файл
    и затем запускается планировщиком (третий уровень) либо вручную.
    """

    FORMAT = "openradioinspector-fixed-frequency-list"
    VERSION = 1

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.frequencies: list[FixedFrequency] = []

    def add(self, item: FixedFrequency) -> FixedFrequency:
        self.frequencies.append(item)
        return item

    def extend(self, items) -> None:
        for item in items:
            self.add(item)

    def find(self, frequency_hz: float, tolerance_hz: float = 0.0) -> Optional[FixedFrequency]:
        """Запись списка для частоты с учётом допуска."""
        for item in self.frequencies:
            if abs(item.frequency_hz - frequency_hz) <= tolerance_hz:
                return item
        return None

    def validate(self) -> list[str]:
        """Замечания ко всему списку частот."""
        warnings: list[str] = []
        seen: set[float] = set()
        for item in self.frequencies:
            for message in item.validate():
                warnings.append(message)
            if item.frequency_hz in seen:
                warnings.append(
                    f"Частота {item.frequency_hz:g} Гц задана в списке повторно"
                )
            seen.add(item.frequency_hz)
        if not self.frequencies:
            warnings.append("Список контролируемых частот пуст")
        return warnings

    def measurement_frequencies(self) -> list[FixedFrequency]:
        """Частоты, на которых при обнаружении измеряются параметры сигнала."""
        return [
            item
            for item in self.frequencies
            if item.action == ACTION_MEASURE_SIGNAL
        ]

    def as_dict(self) -> dict:
        return {
            "format": self.FORMAT,
            "version": self.VERSION,
            "name": self.name,
            "frequencies": [item.as_dict() for item in self.frequencies],
        }

    def save(self, path: str | Path) -> Path:
        """Сохранение задания в файл."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "FixedFrequencyList":
        """Загрузка задания из файла."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        result = cls(payload.get("name", ""))
        result.frequencies = [
            FixedFrequency.from_dict(item) for item in payload.get("frequencies", [])
        ]
        return result


# ---------------------------------------------------------------------------
# Плановое задание
# ---------------------------------------------------------------------------


@dataclass
class ControlTask:
    """Плановое задание радиоконтроля.

    Задание состоит из описательной части (название, цель, вид работы,
    основание выполнения, кто и где выполняет), даты, времени и длительности
    выполнения и задания на выполнение измерений — файла задания на
    контроль фиксированных частот или файла панорам.

    Состав полей повторяет запись базы данных планирования: справочные
    величины вида задания, цели, предприятия, отдела, группы, основания и
    носителя хранятся идентификаторами справочников, служебные даты и
    результаты — отдельными полями.
    """

    name: str
    purpose: str = ""
    work_type: str = ""
    basis: str = ""
    operator: str = ""
    place: str = ""
    equipment: str = ""
    task_file: Optional[str] = None
    panorama_file: Optional[str] = None
    start_at: Optional[datetime] = None
    duration_s: float = 0.0
    status: str = STATUS_DRAFT
    outcome: str = ""
    created_by: str = ""
    completed_cycles: int = 0
    profiles: list[FrequencyControlProfile] = field(default_factory=list)

    # -- справочные поля базы планирования ---------------------------------
    task_type_id: Optional[int] = None
    purpose_id: Optional[int] = None
    rkp_id: Optional[int] = None
    department_id: Optional[int] = None
    group_pa_id: Optional[int] = None
    basis_id: Optional[int] = None
    carrier_id: Optional[int] = None

    # -- служебные даты и операции ------------------------------------------
    created_at: Optional[datetime] = None
    prepared_at: Optional[datetime] = None
    real_started_at: Optional[datetime] = None
    real_stopped_at: Optional[datetime] = None
    break_at: Optional[datetime] = None
    break_reason: str = ""
    created_by_operator: str = ""
    prepared_by_operator: str = ""

    # -- состав и результаты ------------------------------------------------
    device_names: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    observers: list[str] = field(default_factory=list)
    extra_info: str = ""
    result_code: str = ""
    result_text: str = ""
    process_position: str = ""
    mobility: str = ""
    next_times: list[datetime] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    protocol_file: str = ""
    sleep_interval_s: float = 0.0

    # -- состояние ---------------------------------------------------------

    def validate(self) -> list[str]:
        """Проверка задания перед постановкой в очередь."""
        warnings: list[str] = []
        if not self.task_file and not self.panorama_file:
            warnings.append(
                "Не прикреплён файл задания или панорам — задание не может быть "
                "выполнено и остаётся в статусе «Сформировано»"
            )
        if self.start_at is None:
            warnings.append("Не определено время запуска задания")
        if self.duration_s <= 0:
            warnings.append("Не определена длительность выполнения задания")
        for index, profile in enumerate(self.profiles, start=1):
            for message in profile.validate():
                warnings.append(f"Профиль частоты {index}: {message}")
        return warnings

    def can_queue(self) -> bool:
        """Можно ли поставить задание в очередь на выполнение."""
        return bool(self.task_file or self.panorama_file) and self.start_at is not None

    def enqueue(self) -> "ControlTask":
        """Постановка задания в очередь на выполнение."""
        if not self.can_queue():
            raise ValueError(
                "Задание не может быть поставлено в очередь: не выбран файл "
                "задания или не определено время запуска"
            )
        self.status = STATUS_QUEUED
        return self

    def start(self) -> "ControlTask":
        """Перевод задания в состояние выполнения."""
        if self.status != STATUS_QUEUED:
            raise ValueError(
                f"Задание в статусе «{self.status}» не может быть запущено"
            )
        self.status = STATUS_RUNNING
        return self

    def complete(self) -> "ControlTask":
        """Нормальное завершение задания."""
        self.status = STATUS_COMPLETED
        return self

    def abort(self, reason: str = OUTCOME_STOPPED_BY_OPERATOR) -> "ControlTask":
        """Снятие задания с выполнения."""
        self.status = STATUS_ABORTED
        self.outcome = reason
        return self

    def ends_at(self) -> Optional[datetime]:
        """Момент планового завершения задания."""
        if self.start_at is None:
            return None
        return self.start_at + timedelta(seconds=self.duration_s)

    def is_due(self, moment: Optional[datetime] = None) -> bool:
        """Наступило ли время выполнения задания."""
        if self.status != STATUS_QUEUED or self.start_at is None:
            return False
        moment = moment or datetime.now(timezone.utc)
        return moment >= self.start_at

    def outcome_kind(self) -> str:
        """Категория дополнительного статуса: «ошибка», «предупреждение» или пусто."""
        if self.outcome in ERROR_OUTCOMES:
            return "ошибка"
        if self.outcome in WARNING_OUTCOMES:
            return "предупреждение"
        return ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "work_type": self.work_type,
            "basis": self.basis,
            "operator": self.operator,
            "place": self.place,
            "equipment": self.equipment,
            "task_file": self.task_file,
            "panorama_file": self.panorama_file,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "duration_s": self.duration_s,
            "status": self.status,
            "outcome": self.outcome,
            "created_by": self.created_by,
            "completed_cycles": self.completed_cycles,
            "profiles": [profile.as_dict() for profile in self.profiles],
            "task_type_id": self.task_type_id,
            "purpose_id": self.purpose_id,
            "rkp_id": self.rkp_id,
            "department_id": self.department_id,
            "group_pa_id": self.group_pa_id,
            "basis_id": self.basis_id,
            "carrier_id": self.carrier_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "prepared_at": self.prepared_at.isoformat() if self.prepared_at else None,
            "real_started_at": (
                self.real_started_at.isoformat() if self.real_started_at else None
            ),
            "real_stopped_at": (
                self.real_stopped_at.isoformat() if self.real_stopped_at else None
            ),
            "break_at": self.break_at.isoformat() if self.break_at else None,
            "break_reason": self.break_reason,
            "created_by_operator": self.created_by_operator,
            "prepared_by_operator": self.prepared_by_operator,
            "device_names": list(self.device_names),
            "device_ids": list(self.device_ids),
            "observers": list(self.observers),
            "extra_info": self.extra_info,
            "result_code": self.result_code,
            "result_text": self.result_text,
            "process_position": self.process_position,
            "mobility": self.mobility,
            "next_times": [
                moment.isoformat() for moment in self.next_times
            ],
            "attachments": list(self.attachments),
            "protocol_file": self.protocol_file,
            "sleep_interval_s": self.sleep_interval_s,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ControlTask":
        start = data.get("start_at")
        task = cls(
            name=data.get("name", ""),
            purpose=data.get("purpose", ""),
            work_type=data.get("work_type", ""),
            basis=data.get("basis", ""),
            operator=data.get("operator", ""),
            place=data.get("place", ""),
            equipment=data.get("equipment", ""),
            task_file=data.get("task_file"),
            panorama_file=data.get("panorama_file"),
            start_at=datetime.fromisoformat(start) if start else None,
            duration_s=float(data.get("duration_s", 0.0)),
            status=data.get("status", STATUS_DRAFT),
            outcome=data.get("outcome", ""),
            created_by=data.get("created_by", ""),
            completed_cycles=int(data.get("completed_cycles", 0)),
            task_type_id=data.get("task_type_id"),
            purpose_id=data.get("purpose_id"),
            rkp_id=data.get("rkp_id"),
            department_id=data.get("department_id"),
            group_pa_id=data.get("group_pa_id"),
            basis_id=data.get("basis_id"),
            carrier_id=data.get("carrier_id"),
            created_at=_parse_moment(data.get("created_at")),
            prepared_at=_parse_moment(data.get("prepared_at")),
            real_started_at=_parse_moment(data.get("real_started_at")),
            real_stopped_at=_parse_moment(data.get("real_stopped_at")),
            break_at=_parse_moment(data.get("break_at")),
            break_reason=data.get("break_reason", ""),
            created_by_operator=data.get("created_by_operator", ""),
            prepared_by_operator=data.get("prepared_by_operator", ""),
            device_names=list(data.get("device_names", [])),
            device_ids=list(data.get("device_ids", [])),
            observers=list(data.get("observers", [])),
            extra_info=data.get("extra_info", ""),
            result_code=data.get("result_code", ""),
            result_text=data.get("result_text", ""),
            process_position=data.get("process_position", ""),
            mobility=data.get("mobility", ""),
            next_times=[
                moment
                for moment in (
                    _parse_moment(item) for item in data.get("next_times", [])
                )
                if moment is not None
            ],
            attachments=list(data.get("attachments", [])),
            protocol_file=data.get("protocol_file", ""),
            sleep_interval_s=float(data.get("sleep_interval_s", 0.0)),
        )
        task.profiles = [
            FrequencyControlProfile.from_dict(item) for item in data.get("profiles", [])
        ]
        return task


class TaskBook:
    """База данных планирования.

    Хранит задания в JSON-файле; состав полей соответствует организационно-
    административной части задания и параметрам выполнения.
    """

    FORMAT = "openradioinspector-task-book"
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tasks: list[ControlTask] = []

    def load(self) -> "TaskBook":
        """Загрузка заданий из файла."""
        if not self.path.exists():
            return self
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.tasks = [ControlTask.from_dict(item) for item in payload.get("tasks", [])]
        return self

    def save(self) -> Path:
        """Сохранение заданий в файл."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": self.FORMAT,
            "version": self.VERSION,
            "tasks": [task.as_dict() for task in self.tasks],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.path

    def add(self, task: ControlTask) -> ControlTask:
        self.tasks.append(task)
        return task

    def get(self, name: str) -> Optional[ControlTask]:
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    def queue(self) -> list[ControlTask]:
        """Задания, ожидающие выполнения."""
        return [task for task in self.tasks if task.status == STATUS_QUEUED]

    def due(self, moment: Optional[datetime] = None) -> list[ControlTask]:
        """Задания, время которых наступило."""
        return [task for task in self.tasks if task.is_due(moment)]

    def filter(self, **criteria) -> list[ControlTask]:
        """Выборка заданий по значениям полей."""
        result = []
        for task in self.tasks:
            if all(getattr(task, key, None) == value for key, value in criteria.items()):
                result.append(task)
        return result
