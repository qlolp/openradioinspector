"""Экспорт результатов измерений в формате CEF (ITU-R SM.1809).

Рекомендация ITU-R SM.1809 «Standard data exchange format for frequency band
registrations and measurements at monitoring stations» определяет построчный
ASCII-формат, производный от радиомониторингового формата RMDF. Файл состоит
из двух разделов:

* раздел «Заголовок» — статические сведения о задании мониторинга: место
  проведения измерений, временная информация и ключевые параметры;
* раздел «Данные» — результаты измерений за период наблюдения: каждое
  сканирование занимает отдельную строку, первым полем которой является время
  начала сканирования в формате ``ЧЧ:ММ:СС`` (UTC), далее — измеренные
  уровни по каждой частотной точке, разделённые запятыми.

Рекомендация также указывает, что программное обеспечение автоматизированных
систем контроля за использованием спектра должно предоставлять возможность
хранения своих данных либо их преобразования в этот формат.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from ..panorama import PanoramaStore, ScanSettings
from ..trace import Trace

#: Версия формата CEF.
CEF_VERSION = "2.0"

#: Имя файла формата по рекомендации.
CEF_FILE_TYPE = "CMF"


@dataclass
class CefHeader:
    """Поля заголовка формата CEF."""

    location_name: str = "Unknown"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    freq_start_khz: float = 0.0
    freq_stop_khz: float = 0.0
    level_units: str = "dBuV"
    antenna_type: str = "LPD"
    antenna_gain_db: Optional[float] = None
    filter_bandwidth_khz: Optional[float] = None
    data_points: int = 0
    scan_time_s: float = 0.0
    timestamp: Optional[str] = None
    operator: str = ""
    remarks: str = ""
    extra: dict = field(default_factory=dict)

    def to_lines(self) -> list[str]:
        """Представление заголовка в виде строк файла."""
        stamp = self.timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"FileType: {CEF_FILE_TYPE}", f"FileVersion: {CEF_VERSION}"]
        lines.append(f"LocationName: {self.location_name}")
        if self.latitude is not None:
            lines.append(f"Latitude: {self.latitude:.6f}")
        if self.longitude is not None:
            lines.append(f"Longitude: {self.longitude:.6f}")
        lines.append(f"FreqStart: {self.freq_start_khz:.3f}")
        lines.append(f"FreqStop: {self.freq_stop_khz:.3f}")
        lines.append(f"LevelUnits: {self.level_units}")
        lines.append(f"AntennaType: {self.antenna_type}")
        if self.antenna_gain_db is not None:
            lines.append(f"AntennaGain: {self.antenna_gain_db:.2f}")
        if self.filter_bandwidth_khz is not None:
            lines.append(f"FilterBandwidth: {self.filter_bandwidth_khz:.3f}")
        lines.append(f"DataPoints: {self.data_points}")
        lines.append(f"ScanTime: {self.scan_time_s:.6f}")
        lines.append(f"Date: {stamp}")
        if self.operator:
            lines.append(f"Operator: {self.operator}")
        if self.remarks:
            lines.append(f"Remarks: {self.remarks}")
        for key, value in self.extra.items():
            lines.append(f"{key}: {value}")
        return lines


class CefWriter:
    """Построчная запись данных измерений в формат CEF."""

    def __init__(self, header: CefHeader) -> None:
        self.header = header
        self._lines: list[str] = []

    def add_scan(self, recorded_at: datetime, trace: Trace) -> None:
        """Добавляет строку данных одного сканирования."""
        stamp = recorded_at.astimezone(timezone.utc).strftime("%H:%M:%S")
        values = ",".join(f"{level:.1f}" for level in trace.levels)
        self._lines.append(f"{stamp},{values}")

    def write(self, path: str | Path) -> Path:
        """Записывает файл CEF."""
        path = Path(path)
        body = self.header.to_lines() + [""] + self._lines
        path.write_text("\n".join(body) + "\n", encoding="ascii")
        return path

    @property
    def scan_count(self) -> int:
        return len(self._lines)


def export_cef(
    store: PanoramaStore,
    path: str | Path,
    location_name: str = "Unknown",
    level_units: str = "dBuV",
    operator: str = "",
    remarks: str = "",
) -> Path:
    """Выгружает базу данных панорам в файл формата CEF."""
    settings = store.read_settings()
    header = CefHeader(
        location_name=location_name,
        latitude=None,
        longitude=None,
        freq_start_khz=(settings.start_hz / 1000.0) if settings else 0.0,
        freq_stop_khz=(settings.stop_hz / 1000.0) if settings else 0.0,
        level_units=level_units,
        filter_bandwidth_khz=(
            settings.bandwidth_hz / 1000.0 if settings and settings.bandwidth_hz else None
        ),
        data_points=settings.points if settings else 0,
        operator=operator,
        remarks=remarks,
    )
    writer = CefWriter(header)
    for record in store.iter_panoramas():
        writer.add_scan(record.recorded_at, record.trace)
    return writer.write(path)
