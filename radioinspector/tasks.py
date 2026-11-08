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
профиля. Фоновый runtime-only планировщик находится в ``task_scheduler.py``;
сетевой протокол удалённого контроля комплексов в объём не входит.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
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
# Плановое задание
# ---------------------------------------------------------------------------


@dataclass
class ControlTask:
    """Плановое задание радиоконтроля.

    Задание состоит из описательной части (название, цель, вид работы,
    основание выполнения, кто и где выполняет), даты, времени и длительности
    выполнения и задания на выполнение измерений — файла задания на
    контроль фиксированных частот или файла панорам.
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            parent = self.path.parent.lstat()
            if (stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) & 0o022):
                raise ValueError("Каталог заданий должен принадлежать пользователю и быть закрыт для чужой записи")
        payload = {
            "format": self.FORMAT,
            "version": self.VERSION,
            "tasks": [task.as_dict() for task in self.tasks],
        }
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), mode)
                output.write(json.dumps(payload, ensure_ascii=False, indent=2))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
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
