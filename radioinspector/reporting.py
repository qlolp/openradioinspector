"""Формирование протоколов измерений.

Исходный комплекс формирует выходные документы произвольной формы: оператор
подготавливает шаблон, размещает в нём ключевые слова и помещает файл в
определённую директорию; при формировании отчёта программа выбирает требуемую
форму документа и подставляет измеренные значения.

Модуль реализует тот же механизм в двух синтаксисах.

``{{ключ}}``
    Синтаксис OpenRadioInspector. Значение подставляется в порядке первого
    появления в шаблоне; незаполненное поле заменяется на ``н/д`` (нет данных).

``#КлючевоеСлово#``
    Синтаксис исходного комплекса, воспроизведённый дословно: ключевое слово
    состоит из латинских символов, цифр и ``_``, обрамлено символами ``#``,
    регистр значим. Если результата нет, ключевое слово удаляется и на его
    место вставляется пустая строка.

Для таблиц результатов применяются маркеры ``#D1#`` … ``#1D#``,
``#D2#`` … ``#2D#`` и ``#D3#`` … ``#3D#``: между ними размещается шаблон
строки, который повторяется для каждой записи измерений.

Полный словарь ключевых слов — в модуле :mod:`radioinspector.keywords`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .keywords import CATALOG, KEYWORD_RE, TABLE_MARKERS

#: Шаблон подстановки значения (синтаксис OpenRadioInspector).
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

#: Значение, подставляемое вместо отсутствующих данных.
MISSING = "н/д"

#: Шаблон блока таблицы: маркер начала, содержимое, маркер окончания.
TABLE_RE = re.compile(
    r"#(" + "|".join(TABLE_MARKERS) + r")#(?P<body>.*?)#("
    + "|".join(TABLE_MARKERS.values())
    + r")#",
    re.DOTALL,
)


@dataclass
class ReportContext:
    """Набор значений для подстановки в шаблон протокола."""

    values: dict = field(default_factory=dict)

    def set(self, key: str, value: Any) -> "ReportContext":
        self.values[key] = value
        return self

    def update(self, mapping: Mapping[str, Any]) -> "ReportContext":
        self.values.update(mapping)
        return self

    def render(self, template: str) -> str:
        """Подстановка значений в шаблон (оба синтаксиса)."""
        def _replace(match: re.Match) -> str:
            key = match.group(1)
            if key in self.values and self.values[key] is not None:
                return str(self.values[key])
            return MISSING

        text = PLACEHOLDER_RE.sub(_replace, template)
        return self.render_keywords(text)

    def render_keywords(self, template: str) -> str:
        """Подстановка значений по синтаксису ``#КлючевоеСлово#``.

        Отсутствующие значения заменяются пустой строкой — как в исходном
        комплексе, где ключевое слово без результата удаляется из отчёта.
        """

        def _replace(match: re.Match) -> str:
            key = match.group(1)
            value = self.values.get(key)
            if value is None:
                return ""
            return str(value)

        return KEYWORD_RE.sub(_replace, template)

    def missing_keys(self, template: str) -> list[str]:
        """Перечень ключей шаблона, для которых нет значения."""
        keys: list[str] = []
        for key in PLACEHOLDER_RE.findall(template) + KEYWORD_RE.findall(template):
            if key not in keys:
                keys.append(key)
        return [
            key
            for key in keys
            if key not in self.values or self.values[key] is None
        ]


def render_template(
    template: str,
    context: ReportContext,
    tables: Optional[Mapping[str, list]] = None,
) -> str:
    """Подстановка значений и разворачивание таблиц в шаблон протокола.

    Параметр ``tables`` задаёт записи для маркеров ``#D1#``, ``#D2#`` и
    ``#D3#``: между маркерами размещается шаблон строки, который повторяется
    для каждой записи. Если записей нет, блок таблицы удаляется целиком.
    """
    tables = tables or {}

    def _table(match: re.Match) -> str:
        start = match.group(1)
        body = match.group("body")
        rows = tables.get(start) or []
        if not rows:
            return ""
        rendered = []
        for row in rows:
            merged = dict(context.values)
            merged.update(row)
            rendered.append(
                ReportContext(values=merged).render_keywords(body).rstrip("\n")
            )
        return "\n".join(rendered)

    text = TABLE_RE.sub(_table, template)
    return context.render(text)



class ProtocolTemplate:
    """Шаблон протокола измерений."""

    def __init__(self, name: str, template: str, description: str = "") -> None:
        self.name = name
        self.template = template
        self.description = description

    @classmethod
    def from_file(cls, path: str | Path) -> "ProtocolTemplate":
        path = Path(path)
        return cls(
            name=path.stem,
            template=path.read_text(encoding="utf-8"),
            description="Шаблон протокола, загруженный из файла",
        )

    def render(self, context: ReportContext) -> str:
        return context.render(self.template)

    def keys(self) -> list[str]:
        """Перечень ключей шаблона в порядке первого появления."""
        seen: list[str] = []
        for key in PLACEHOLDER_RE.findall(self.template) + KEYWORD_RE.findall(
            self.template
        ):
            if key not in seen:
                seen.append(key)
        return seen

    def unknown_keys(self) -> list[str]:
        """Ключи шаблона, отсутствующие в словаре исходного комплекса."""
        return [key for key in self.keys() if key not in CATALOG]



class ProtocolLibrary:
    """Каталог шаблонов протоколов."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[ProtocolTemplate]:
        templates: list[ProtocolTemplate] = []
        for path in sorted(self.directory.glob("*.md")):
            templates.append(ProtocolTemplate.from_file(path))
        return templates

    def save(self, template: ProtocolTemplate) -> Path:
        path = self.directory / f"{template.name}.md"
        path.write_text(template.template, encoding="utf-8")
        return path


def build_context(
    measurements: Optional[dict] = None,
    store=None,
    signals: Optional[list] = None,
    operator: str = "",
    place: str = "",
    extra: Optional[Mapping[str, Any]] = None,
) -> ReportContext:
    """Формирует контекст подстановки из результатов измерений."""
    context = ReportContext()
    context.set("date", datetime.now(timezone.utc).strftime("%d.%m.%Y"))
    context.set("time", datetime.now(timezone.utc).strftime("%H:%M:%S"))
    context.set("datetime", datetime.now(timezone.utc).isoformat())
    context.set("operator", operator)
    context.set("place", place)

    if store is not None:
        settings = store.read_settings()
        if settings is not None:
            context.set("device", settings.device)
            context.set("antenna", settings.antenna)
            context.set("tract", settings.tract)
            context.set("freq_start_mhz", settings.start_hz / 1e6)
            context.set("freq_stop_mhz", settings.stop_hz / 1e6)
            context.set("bandwidth_khz", settings.bandwidth_hz / 1e3)
            context.set("step_khz", settings.step_hz / 1e3)
            context.set("points", settings.points)
        context.set("panorama_count", store.count)
        max_trace = store.max_trace()
        if max_trace is not None:
            context.set("max_level_db", f"{max_trace.peak:.1f}")

    if measurements:
        ob = measurements.get("occupied_bandwidth_beta2") or {}
        if ob:
            context.set("occupied_bandwidth_khz", ob.get("bandwidth_hz", 0.0) / 1e3)
            context.set("occupied_bandwidth_method", ob.get("method", ""))
        bl = measurements.get("bandwidth_at_level") or {}
        if bl:
            context.set("bandwidth_ndb_khz", bl.get("bandwidth_hz", 0.0) / 1e3)
        fs = measurements.get("field_strength") or {}
        if fs:
            context.set("field_strength_db", fs.get("value", ""))
        cf = measurements.get("center_frequency_centroid") or {}
        if cf:
            context.set("center_frequency_mhz", cf.get("value", 0.0) / 1e6)

    if signals is not None:
        context.set("signal_count", len(signals))
        context.set(
            "signal_table",
            "\n".join(
                f"| {i + 1} | {s['frequency_hz'] / 1e6:.4f} | "
                f"{s['bandwidth_hz'] / 1e3:.1f} | {s['event_count']} | "
                f"{s['first_seen']} |"
                for i, s in enumerate(signals)
            ),
        )

    if extra:
        context.update(extra)
    return context


DEFAULT_TEMPLATE = """# ПРОТОКОЛ измерений параметров радиоизлучений

Дата проведения измерений: {{date}}
Время: {{time}}
Место проведения измерений: {{place}}
Оператор: {{operator}}

## Условия измерений

Средство измерений: {{device}}
Антенна: {{antenna}}
Антенный тракт: {{tract}}
Полоса сканирования: {{freq_start_mhz}}–{{freq_stop_mhz}} МГц
Полоса пропускания: {{bandwidth_khz}} кГц
Шаг перестройки: {{step_khz}} кГц
Число точек: {{points}}
Записей в базе данных панорам: {{panorama_count}}

## Результаты измерений

Ширина полосы занимаемых частот (метод {{occupied_bandwidth_method}}):
{{occupied_bandwidth_khz}} кГц

Ширина полосы по уровню −N дБ: {{bandwidth_ndb_khz}} кГц

Напряжённость поля: {{field_strength_db}} дБ

Оценка центральной частоты: {{center_frequency_mhz}} МГц

## Обнаруженные сигналы

Всего сигналов, превысивших линию порога: {{signal_count}}

| № | Частота, МГц | Полоса, кГц | Число обнаружений | Время первого обнаружения |
|---|--------------|------------|-------------------|---------------------------|
{{signal_table}}

## Примечание

Программное обеспечение OpenRadioInspector не является аттестованным
средством измерений. Погрешность результатов определяется погрешностью
измерительного тракта и выбранной методикой измерения.
"""
