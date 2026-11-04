"""Список сигналов, превысивших линию порога.

Реализует раздел 7 «Список сигналов, превысивших линию порога»
руководства оператора Книги 6.

Правила формирования списка (§7.2):

* полоса занимаемых частот сигнала определяется как разница между конечной
  и начальной частотой превышения порога;
* частота сигнала определяется как середина частотного интервала превышения
  порога;
* найденный сигнал считается совпадающим с уже существующим в списке, если
  его частота попадает в интервал частот существующего сигнала и полоса
  занимаемых частот нового сигнала не превышает пятикратного значения полосы
  существующего сигнала; иначе формируется новая строка списка;
* для снижения вероятности ложной тревоги вводится ограничение на мощность
  превышения линии порога (по умолчанию 0,5–20 ркВт в зависимости от
  аппаратуры).

Сопоставление с базой данных частотных присвоений (§7.3): частота сигнала
из списка должна попасть в область, ограниченную половиной полосы занимаемых
частот вокруг центральной частоты записи в базе присвоений, а различие полос
занимаемых частот не должно превышать четырёхкратного размера.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from .threshold import ThresholdLine, exceeded_intervals, power_over_threshold
from .trace import Trace

#: Максимальное отношение полосы нового сигнала к полосе существующего,
#: при котором сигналы считаются совпадающими (§7.2).
BANDWIDTH_MATCH_RATIO = 5.0

#: Максимальное отношение полос сигнала из списка и записи базы присвоений,
#: при котором они считаются совпадающими (§7.3).
ASSIGNMENT_MATCH_RATIO = 4.0

#: Порог мощности превышения линии порога по умолчанию, Вт.
#: Исходный комплекс использует величины порядка 0,5–20 ркВт.
DEFAULT_POWER_OVER_THRESHOLD_PW = 1.0e-12


@dataclass
class SignalEvent:
    """Отдельное событие превышения линии порога."""

    timestamp: datetime
    frequency_hz: float
    bandwidth_hz: float
    power_over_threshold_w: float
    level_db: float
    spectrum: list[float] = field(default_factory=list)
    frequencies: list[float] = field(default_factory=list)
    threshold_levels: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "frequency_hz": self.frequency_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "power_over_threshold_w": self.power_over_threshold_w,
            "level_db": self.level_db,
        }


@dataclass
class SignalRecord:
    """Строка списка сигналов.

    Поля ``frequency_hz`` и ``bandwidth_hz`` усредняются по всем событиям
    сигнала; для фиксированных сигналов (загруженных из базы присвоений или
    отредактированных вручную) они не изменяются.
    """

    frequency_hz: float
    bandwidth_hz: float
    first_seen: datetime
    events: list[SignalEvent] = field(default_factory=list)
    fixed: bool = False
    from_assignment: bool = False
    assignment_id: Optional[int] = None
    level_db: float = float("nan")
    average_power_over_threshold_w: float = 0.0
    cycles: int = 0

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def detection_periodicity(self) -> str:
        """Периодичность обнаружения: «число превышений / число циклов»."""
        if not self.events:
            return f"0/{self.cycles}"
        return f"{len(self.events)}/{self.cycles}"

    @property
    def is_cut_spectrum(self) -> bool:
        """Признак изрезанности спектра сигнала.

        Если линия порога пересекает спектр одного сигнала несколько раз, за
        один цикл сканирования регистрируется несколько узкополосных сигналов
        и число обнаружений превышает число циклов сканирования.
        """
        return self.cycles > 0 and len(self.events) > self.cycles

    def lower_edge_hz(self) -> float:
        return self.frequency_hz - self.bandwidth_hz / 2.0

    def upper_edge_hz(self) -> float:
        return self.frequency_hz + self.bandwidth_hz / 2.0

    def contains_frequency(self, frequency_hz: float) -> bool:
        """Попадает ли частота в интервал частот сигнала."""
        return self.lower_edge_hz() <= frequency_hz <= self.upper_edge_hz()

    def as_row(self) -> dict:
        """Строка списка в виде словаря (колонки §7.2)."""
        return {
            "frequency_hz": self.frequency_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "first_seen": self.first_seen.isoformat(),
            "detection_periodicity": self.detection_periodicity,
            "power_over_threshold_w": self.average_power_over_threshold_w,
            "event_count": self.event_count,
            "cycles": self.cycles,
            "cut_spectrum": self.is_cut_spectrum,
            "fixed": self.fixed,
            "from_assignment": self.from_assignment,
        }


class SignalList:
    """Список сигналов, превысивших линию порога."""

    def __init__(self) -> None:
        self._records: list[SignalRecord] = []
        self.cycles = 0

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def __getitem__(self, index: int) -> SignalRecord:
        return self._records[index]

    @property
    def records(self) -> list[SignalRecord]:
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()

    # -- сопоставление -----------------------------------------------------

    def find_matching(self, frequency_hz: float, bandwidth_hz: float) -> Optional[SignalRecord]:
        """Поиск существующей записи, соответствующей новому сигналу.

        Критерий совпадения (§7.2): частота попадает в интервал частот
        существующего сигнала и новая полоса не превышает пятикратного
        значения полосы существующего сигнала.
        """
        for record in self._records:
            if not record.contains_frequency(frequency_hz):
                continue
            if record.bandwidth_hz <= 0:
                continue
            if bandwidth_hz <= BANDWIDTH_MATCH_RATIO * record.bandwidth_hz:
                return record
        return None

    # -- добавление --------------------------------------------------------

    def update_from_trace(
        self,
        trace: Trace,
        line: ThresholdLine,
        timestamp: Optional[datetime] = None,
        power_threshold_w: float = DEFAULT_POWER_OVER_THRESHOLD_PW,
    ) -> list[SignalRecord]:
        """Обрабатывает панораму и регистрирует сигналы, превысившие порог.

        Возвращает список записей, обновлённых или созданных при обработке
        этой панорамы.
        """
        timestamp = timestamp or datetime.now(timezone.utc)
        touched: list[SignalRecord] = []
        step = trace.step or 0.0
        self.cycles += 1
        cycle = self.cycles

        for interval in exceeded_intervals(trace, line):
            f_start, f_stop = interval
            # Ширина занимаемых частот определяется как разница между конечной
            # и начальной частотой превышения порога. Если превышение
            # захватывает единственную точку измерения, ширина принимается
            # равной шагу перестройки частоты — иначе сигнал вырождался бы в
            # точку и не сопоставлялся бы с другими записями.
            bandwidth = max(f_stop - f_start, step)
            if bandwidth <= 0:
                continue
            frequency = 0.5 * (f_start + f_stop)

            excess_w = power_over_threshold(trace, line, interval)
            if excess_w < power_threshold_w:
                continue

            segment = trace.slice(f_start, f_stop)
            level = float(np.max(segment.levels)) if segment.size else float("nan")

            event = SignalEvent(
                timestamp=timestamp,
                frequency_hz=frequency,
                bandwidth_hz=bandwidth,
                power_over_threshold_w=excess_w,
                level_db=level,
                frequencies=segment.frequencies.tolist(),
                spectrum=segment.levels.tolist(),
                threshold_levels=line.evaluate(segment).tolist(),
            )

            record = self.find_matching(frequency, bandwidth)
            if record is None:
                record = SignalRecord(
                    frequency_hz=frequency,
                    bandwidth_hz=bandwidth,
                    first_seen=timestamp,
                    events=[event],
                    level_db=level,
                    average_power_over_threshold_w=excess_w,
                    cycles=cycle,
                )
                self._records.append(record)
            else:
                record.events.append(event)
                record.cycles = cycle
                if not record.fixed:
                    # Частота и полоса уточняются по всем событиям сигнала.
                    total = len(record.events)
                    record.frequency_hz += (frequency - record.frequency_hz) / total
                    record.bandwidth_hz += (bandwidth - record.bandwidth_hz) / total
                    record.level_db = max(record.level_db, level)
                    record.average_power_over_threshold_w = (
                        record.average_power_over_threshold_w * (total - 1) + excess_w
                    ) / total
            touched.append(record)

        return touched

    # -- сопоставление с базой присвоений ---------------------------------

    def highlight_assignments(self, assignments) -> list[SignalRecord]:
        """Помечает сигналы, совпадающие с записями базы частотных присвоений.

        Критерий (§7.3): попадание частоты сигнала в область, ограниченную
        половиной полосы занимаемых частот вокруг центральной частоты записи,
        и различие полос не более чем в четыре раза.
        """
        matched: list[SignalRecord] = []
        for record in self._records:
            for assignment in assignments:
                half_bw = assignment.bandwidth_hz / 2.0
                if not (assignment.frequency_hz - half_bw <= record.frequency_hz
                        <= assignment.frequency_hz + half_bw):
                    continue
                if record.bandwidth_hz <= 0 or assignment.bandwidth_hz <= 0:
                    continue
                ratio = max(
                    record.bandwidth_hz / assignment.bandwidth_hz,
                    assignment.bandwidth_hz / record.bandwidth_hz,
                )
                if ratio > ASSIGNMENT_MATCH_RATIO:
                    continue
                record.from_assignment = True
                record.assignment_id = assignment.id
                matched.append(record)
                break
        return matched

    def load_assignments(
        self, assignments, replace: bool = False
    ) -> list[SignalRecord]:
        """Загружает частоты из базы присвоений в список сигналов.

        Загруженные сигналы помечаются как фиксированные: их частота и полоса
        не изменяются при добавлении новых событий.
        """
        if replace:
            self.clear()
        loaded: list[SignalRecord] = []
        now = datetime.now(timezone.utc)
        for assignment in assignments:
            record = SignalRecord(
                frequency_hz=assignment.frequency_hz,
                bandwidth_hz=assignment.bandwidth_hz,
                first_seen=now,
                fixed=True,
                from_assignment=True,
                assignment_id=assignment.id,
            )
            self._records.append(record)
            loaded.append(record)
        return loaded

    # -- редактирование ----------------------------------------------------

    def edit_signal(self, index: int, frequency_hz: float, bandwidth_hz: float) -> SignalRecord:
        """Ручная коррекция частоты и полосы сигнала.

        Отредактированный сигнал становится фиксированным: его частота и полоса
        при добавлении новых записей не изменяются.
        """
        record = self._records[index]
        record.frequency_hz = frequency_hz
        record.bandwidth_hz = bandwidth_hz
        record.fixed = True
        return record

    def remove(self, index: int) -> SignalRecord:
        record = self._records.pop(index)
        return record

    def clear_signals(self) -> None:
        self.clear()

    # -- сериализация ------------------------------------------------------

    def to_rows(self) -> list[dict]:
        return [record.as_row() for record in self._records]

    def save(self, path: str | Path) -> Path:
        """Сохранение списка сигналов в файл JSON."""
        path = Path(path)
        payload = {
            "format": "openradioinspector-signal-list",
            "version": 1,
            "cycles": self.cycles,
            "signals": [
                {
                    **record.as_row(),
                    "first_seen": record.first_seen.isoformat(),
                    "level_db": record.level_db,
                    "assignment_id": record.assignment_id,
                    "events": [event.as_dict() for event in record.events],
                }
                for record in self._records
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SignalList":
        """Загрузка списка сигналов из файла JSON."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        result = cls()
        for item in payload.get("signals", []):
            record = SignalRecord(
                frequency_hz=item["frequency_hz"],
                bandwidth_hz=item["bandwidth_hz"],
                first_seen=datetime.fromisoformat(item["first_seen"]),
                fixed=bool(item.get("fixed", False)),
                from_assignment=bool(item.get("from_assignment", False)),
                assignment_id=item.get("assignment_id"),
                level_db=item.get("level_db", float("nan")),
                average_power_over_threshold_w=item.get("power_over_threshold_w", 0.0),
                cycles=int(item.get("cycles", 0)),
            )
            for event in item.get("events", []):
                record.events.append(
                    SignalEvent(
                        timestamp=datetime.fromisoformat(event["timestamp"]),
                        frequency_hz=event["frequency_hz"],
                        bandwidth_hz=event["bandwidth_hz"],
                        power_over_threshold_w=event.get("power_over_threshold_w", 0.0),
                        level_db=event.get("level_db", float("nan")),
                    )
                )
            result._records.append(record)
        if "cycles" in payload:
            result.cycles = int(payload["cycles"])
        else:
            # Файл создан до появления счётчика циклов: принимаем, что за один
            # цикл сигнал обнаруживается не более одного раза.
            result.cycles = max(
                (len(record.events) for record in result._records), default=0
            )
            for record in result._records:
                record.cycles = result.cycles
        return result

    def add_event(
        self,
        frequency_hz: float,
        bandwidth_hz: float,
        timestamp: Optional[datetime] = None,
        power_over_threshold_w: float = 0.0,
        level_db: float = float("nan"),
    ) -> SignalRecord:
        """Ручная регистрация события обнаружения сигнала."""
        timestamp = timestamp or datetime.now(timezone.utc)
        event = SignalEvent(
            timestamp=timestamp,
            frequency_hz=frequency_hz,
            bandwidth_hz=bandwidth_hz,
            power_over_threshold_w=power_over_threshold_w,
            level_db=level_db,
        )
        record = self.find_matching(frequency_hz, bandwidth_hz)
        if record is None:
            record = SignalRecord(
                frequency_hz=frequency_hz,
                bandwidth_hz=bandwidth_hz,
                first_seen=timestamp,
                events=[event],
                level_db=level_db,
                average_power_over_threshold_w=power_over_threshold_w,
            )
            self._records.append(record)
        else:
            record.events.append(event)
            if not record.fixed:
                total = len(record.events)
                record.frequency_hz += (frequency_hz - record.frequency_hz) / total
                record.bandwidth_hz += (bandwidth_hz - record.bandwidth_hz) / total
        return record
