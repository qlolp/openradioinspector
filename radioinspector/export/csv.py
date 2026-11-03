"""Выгрузка результатов измерений в плоские таблицы CSV.

CSV-формат используется для внешней обработки: по условиям исходного
комплекса данные должны выгружаться в Microsoft Excel и Microsoft Word, а
также в текстовые файлы с разделителем «табуляция».
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

from ..panorama import PanoramaStore
from ..signals import SignalList
from ..trace import Trace


def export_trace_csv(trace: Trace, path: str | Path) -> Path:
    """Выгрузка амплитудно-частотной характеристики в CSV."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frequency_hz", f"level_{trace.unit}"])
        for freq, level in zip(trace.frequencies, trace.levels):
            writer.writerow([f"{freq:.3f}", f"{level:.4f}"])
    return path


def export_panoramas_csv(store: PanoramaStore, path: str | Path) -> Path:
    """Выгрузка сводки по панорамам в CSV."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "id",
                "recorded_at",
                "latitude",
                "longitude",
                "altitude_m",
                "speed",
                "heading",
                "start_hz",
                "stop_hz",
                "points",
                "max_level_db",
            ]
        )
        for record in store.iter_panoramas():
            writer.writerow(
                [
                    record.id,
                    record.recorded_at.isoformat(),
                    "" if record.latitude is None else f"{record.latitude:.6f}",
                    "" if record.longitude is None else f"{record.longitude:.6f}",
                    "" if record.altitude is None else f"{record.altitude:.1f}",
                    "" if record.speed is None else f"{record.speed:.2f}",
                    "" if record.heading is None else f"{record.heading:.1f}",
                    f"{record.trace.start:.3f}" if record.trace.size else "",
                    f"{record.trace.stop:.3f}" if record.trace.size else "",
                    record.trace.size,
                    f"{record.trace.peak:.3f}" if record.trace.size else "",
                ]
            )
    return path


def export_signals_csv(signals: SignalList, path: str | Path) -> Path:
    """Выгрузка списка обнаруженных сигналов в CSV."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "frequency_hz",
                "bandwidth_hz",
                "first_seen",
                "detection_periodicity",
                "power_over_threshold_w",
                "event_count",
                "fixed",
                "from_assignment",
            ]
        )
        for row in signals.to_rows():
            writer.writerow(
                [
                    f"{row['frequency_hz']:.3f}",
                    f"{row['bandwidth_hz']:.3f}",
                    row["first_seen"],
                    row["detection_periodicity"],
                    f"{row['power_over_threshold_w']:.6e}",
                    row["event_count"],
                    int(row["fixed"]),
                    int(row["from_assignment"]),
                ]
            )
    return path
