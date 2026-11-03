#!/usr/bin/env python3
"""Оценка эффективности использования диапазона и построение протокола.

Пример повторяет сценарий, описанный в документации исходного комплекса:
наблюдение диапазона сериями панорам, вычисление доли моментов наблюдения,
в которые сигнал превышал порог, разбиение наблюдения на интервалы
«включено/выключено» и формирование протокола по ключевым словам.

Показатель эффективности использования диапазона — доля моментов наблюдения
с превышением порога (n/N по времени). Это **не** загрузка диапазона по
каналам: обе величины вычисляются раздельно.

Запуск:

    python3 examples/example_band_efficiency.py --output /tmp/example-efficiency
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from radioinspector.occupancy import band_efficiency, duty_intervals  # noqa: E402
from radioinspector.panorama import (  # noqa: E402
    PanoramaStore,
    ScanSettings,
    generate_synthetic_trace,
)
from radioinspector.reporting import ReportContext, render_template  # noqa: E402

#: Параметры синтетического излучения: частота, ширина полосы, уровень.
SIGNAL = (102.5e6, 50e3, -40.0)


def build_store(path: pathlib.Path, cycles: int, interval_s: float) -> PanoramaStore:
    """Создаёт базу панорам с чередующимся присутствием сигнала."""
    settings = ScanSettings(
        start_hz=102e6,
        stop_hz=103e6,
        step_hz=100e3,
        points=11,
        device="Demo",
        antenna="Ant",
    )
    store = PanoramaStore(str(path), settings)
    moment = datetime.now(timezone.utc)
    for index in range(cycles):
        if index % 3 == 2:
            # каждая третья панорама — без сигнала в контролируемой полосе
            trace = generate_synthetic_trace(settings, seed=index)
        else:
            trace = generate_synthetic_trace(
                settings, seed=index, signals=[SIGNAL]
            )
        store.append(trace, recorded_at=moment)
        moment += timedelta(seconds=interval_s)
    return store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/example-efficiency")
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--interval", type=float, default=30.0, help="интервал, с")
    parser.add_argument("--threshold", type=float, default=-80.0, help="порог, дБ")
    args = parser.parse_args()

    out = pathlib.Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "efficiency.pan"
    if path.exists():
        path.unlink()

    with build_store(path, args.cycles, args.interval) as store:
        grid, times = store.level_grid()
        freqs = store.frequencies()

    result = band_efficiency(grid, times, freqs, args.threshold)
    print(f"Наблюдений: {result.measurements}")
    print(f"Превышений порога: {result.exceedances}")
    print(
        f"Эффективность использования диапазона: {result.efficiency * 100:.2f} % "
        f"(n={result.exceedances}, N={result.measurements})"
    )
    print(result.summary())

    levels = grid.max(axis=1)
    print("\nИнтервалы работы источника:")
    for interval in duty_intervals(levels, times, args.threshold):
        state = "вкл." if interval.active else "выкл."
        start = datetime.fromtimestamp(interval.started_at, tz=timezone.utc)
        end = datetime.fromtimestamp(interval.ended_at, tz=timezone.utc)
        print(
            f"  {state} {start.strftime('%H:%M:%S %d.%m')} — "
            f"{end.strftime('%H:%M:%S %d.%m')}, "
            f"длит. {interval.duration_s:.0f} с"
        )

    context = (
        ReportContext()
        .set("TrOwner", "ООО «Пример»")
        .set("MeasDate", datetime.now(timezone.utc).strftime("%d.%m.%Y"))
        .set("SbRng_Busy", f"{result.efficiency * 100:.2f}")
        .set("SbRng_Statistics", result.statistics())
    )
    template = (
        "Отчёт по результатам наблюдения диапазона\n"
        "Дата: #MeasDate#\n"
        "Оператор: #TrOwner#\n"
        "Эффективность использования диапазона: #SbRng_Busy# %\n"
        "#SbRng_Statistics#\n"
    )
    print()
    print(render_template(template, context))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
