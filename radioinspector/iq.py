"""Расчёт требований к IQ-данным для анализа цифровых сигналов.

Исходный комплекс при выборе нескольких цифровых стандартов определяет
максимальную требуемую частоту дискретизации Fd и максимальную требуемую
длительность записи L, оценивает необходимый объём памяти M = Fd · L и при
недостатке памяти прибора ограничивает длительность записи величиной
L1 = M_доступное / Fd. Стандарты, требующие большей длительности, не
анализируются; при недостаточной частоте дискретизации анализ стандарта
невозможен вовсе.

Модуль реализует расчёт и проверку, а не демодуляцию: цифровые декодеры
(DECT, GSM, TETRA, APCO25, DMR) оригинала закрыты и не воспроизводимы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Сообщения исходного комплекса.
REASON_NO_REQUIREMENTS = "не заданы"
REASON_RATE = "недостаточная частота дискретизации"
REASON_LENGTH = "недостаточная длина IQ"


@dataclass(frozen=True)
class IqRequirement:
    """Требования к IQ-данным для одного цифрового стандарта."""

    standard: str
    min_sample_rate_hz: float
    min_duration_s: float
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "standard": self.standard,
            "min_sample_rate_hz": self.min_sample_rate_hz,
            "min_duration_s": self.min_duration_s,
            "note": self.note,
        }


#: Каталог требований.
#:
#: Длительность записи для каждого стандарта приведена по документу
#: «Требования к IQ (объёма памяти и частоты дискретизации) для анализа
#: цифровых сигналов (опция DTEST)». Частота дискретизации 2000 кГц
#: документирована для DECT; для остальных стандартов указана частота из
#: примера расчёта того же документа, где максимум по набору стандартов
#: определяется требованием DECT.
IQ_REQUIREMENTS: tuple[IqRequirement, ...] = (
    IqRequirement(
        "DECT",
        2_000_000.0,
        0.2,
        "Частота дискретизации указана в документации; при недостатке памяти "
        "стандарт требует наименьшей длительности записи",
    ),
    IqRequirement(
        "AnalogTV",
        2_000_000.0,
        0.1,
        "Анализ аналогового телевизионного сигнала",
    ),
    IqRequirement(
        "DMR",
        2_000_000.0,
        0.8,
        "Цифровой стандарт; декодер оригинала закрыт",
    ),
    IqRequirement(
        "APCO25",
        2_000_000.0,
        0.8,
        "Цифровой стандарт; декодер оригинала закрыт",
    ),
    IqRequirement(
        "GSM",
        2_000_000.0,
        0.98,
        "Цифровой стандарт; декодер оригинала закрыт",
    ),
    IqRequirement(
        "TETRA",
        2_000_000.0,
        1.076,
        "Цифровой стандарт; декодер оригинала закрыт",
    ),
    IqRequirement(
        "Bluetooth",
        2_000_000.0,
        0.5,
        "Частота дискретизации и длительность определены приблизительно",
    ),
)


@dataclass
class IqStandardVerdict:
    """Результат проверки требований одного стандарта."""

    standard: str
    allowed: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "standard": self.standard,
            "allowed": self.allowed,
            "reason": self.reason,
        }


@dataclass
class IqPlan:
    """План записи и демодуляции IQ по набору стандартов."""

    required_rate_hz: float = 0.0
    required_duration_s: float = 0.0
    required_words: float = 0.0
    allowed_duration_s: float = 0.0
    allowed_rate_hz: float = 0.0
    possible: bool = False
    reason: str = ""
    standards: list[IqStandardVerdict] = field(default_factory=list)

    @property
    def allowed_standards(self) -> list[str]:
        """Стандарты, анализ которых возможен при данных параметрах прибора."""
        return [item.standard for item in self.standards if item.allowed]

    @property
    def rejected_standards(self) -> list[str]:
        """Стандарты, анализ которых невозможен."""
        return [item.standard for item in self.standards if not item.allowed]

    def as_dict(self) -> dict:
        return {
            "required_rate_hz": self.required_rate_hz,
            "required_duration_s": self.required_duration_s,
            "required_words": self.required_words,
            "allowed_duration_s": self.allowed_duration_s,
            "allowed_rate_hz": self.allowed_rate_hz,
            "possible": self.possible,
            "reason": self.reason,
            "standards": [item.as_dict() for item in self.standards],
        }


def plan_iq_analysis(
    requirements: list[IqRequirement],
    sample_rate_hz: float,
    memory_words: float,
    resample: bool = True,
) -> IqPlan:
    """Рассчитывает возможность анализа цифровых сигналов.

    ``sample_rate_hz`` — частота дискретизации, доступная на приборе;
    ``memory_words`` — объём памяти прибора для записи IQ в словах;
    ``resample`` — выполнять ли ресемплинг к требуемой частоте, когда
    прибор даёт большую частоту дискретизации.
    """
    plan = IqPlan()
    if not requirements:
        plan.reason = f"Требования к IQ {REASON_NO_REQUIREMENTS}"
        return plan

    plan.required_rate_hz = max(item.min_sample_rate_hz for item in requirements)
    plan.required_duration_s = max(item.min_duration_s for item in requirements)
    plan.required_words = plan.required_rate_hz * plan.required_duration_s
    plan.allowed_rate_hz = sample_rate_hz

    if sample_rate_hz < plan.required_rate_hz:
        plan.reason = REASON_RATE
        plan.standards = [
            IqStandardVerdict(item.standard, False, REASON_RATE)
            for item in requirements
        ]
        return plan

    # При большей частоте прибора запись ведётся с требуемой частотой —
    # как в исходном комплексе, где выполняется ресемплинг.
    effective_rate = plan.required_rate_hz if resample else sample_rate_hz
    plan.allowed_rate_hz = effective_rate

    available_duration = memory_words / effective_rate if effective_rate else 0.0
    plan.allowed_duration_s = min(available_duration, plan.required_duration_s)

    for item in requirements:
        if item.min_sample_rate_hz > sample_rate_hz:
            plan.standards.append(
                IqStandardVerdict(item.standard, False, REASON_RATE)
            )
        elif item.min_duration_s > plan.allowed_duration_s + 1e-9:
            plan.standards.append(
                IqStandardVerdict(item.standard, False, REASON_LENGTH)
            )
        else:
            plan.standards.append(IqStandardVerdict(item.standard, True))

    plan.possible = any(item.allowed for item in plan.standards)
    if not plan.possible:
        plan.reason = REASON_LENGTH
    return plan


def requirements_for(standards: list[str]) -> list[IqRequirement]:
    """Требования по именам стандартов; неизвестные имена игнорируются."""
    wanted = {name.strip().upper() for name in standards if name.strip()}
    return [item for item in IQ_REQUIREMENTS if item.standard.upper() in wanted]


def format_plan(plan: IqPlan) -> str:
    """Текстовое представление плана записи IQ."""
    lines = [
        f"Требуемая частота дискретизации: {plan.required_rate_hz / 1e3:.1f} кГц",
        f"Требуемая длительность записи: {plan.required_duration_s:.3f} с",
        f"Требуемый объём памяти: {plan.required_words / 1e6:.3f} млн слов IQ",
        f"Допустимая длительность записи: {plan.allowed_duration_s:.3f} с",
    ]
    for item in plan.standards:
        mark = "+" if item.allowed else "-"
        suffix = f" ({item.reason})" if item.reason else ""
        lines.append(f"  {mark} {item.standard}{suffix}")
    return "\n".join(lines)
