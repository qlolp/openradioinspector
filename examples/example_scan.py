#!/usr/bin/env python3
"""Программное сканирование диапазона частот с записью в базу данных панорам.

Пример показывает типичный сценарий работы комплекса:

1. формирование параметров сканирования;
2. открытие базы данных панорам с правилом продолжения записи;
3. последовательное выполнение циклов измерений;
4. автоматическое выделение сигналов по линии порога;
5. сохранение реестра обнаруженных сигналов и выгрузка в CEF.

Запуск:

    python3 examples/example_scan.py --output /tmp/example-scan --cycles 30
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import argparse
from datetime import datetime, timezone

from radioinspector.devices.base import SweepConfig
from radioinspector.devices.simulator import Emitter, SimulatedInstrument
from radioinspector.export import export_cef
from radioinspector.panorama import PanoramaLibrary, ScanSettings
from radioinspector.signals import SignalList
from radioinspector.threshold import ThresholdLine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/example-scan", help="каталог для базы данных")
    parser.add_argument("--cycles", type=int, default=30, help="число циклов измерений")
    parser.add_argument("--delay", type=float, default=0.0, help="пауза между циклами, с")
    args = parser.parse_args()

    # 1. Параметры сканирования. Их набор определяет имя файла базы данных.
    settings = ScanSettings(
        device="Sim",
        antenna="LOG-ANT",
        tract="CABLE-3m",
        start_hz=30e6,
        stop_hz=130e6,
        bandwidth_hz=100e3,
        demodulator="None",
        step_hz=125e3,
        attenuation_db=0,
        reference_level_db=0,
        points=801,
        detector="MaxPeak",
        video_bandwidth_hz=100e3,
        sweep_time_s=0.0,
        mode="range",
    )

    # 2. Открытие базы данных: при наличии файла запись продолжается.
    library = PanoramaLibrary(args.output)
    store, existed = library.open_for(settings)
    print(f"База данных: {store.path} ({'продолжена' if existed else 'создана'})")

    # 3. Реестр обнаруженных сигналов (файл создаётся при первом запуске).
    signals_path = store.path.with_suffix(".signals.json")
    signals = SignalList.load(signals_path) if signals_path.exists() else SignalList()

    # 4. Линия порога: горизонтальная на заданном уровне.
    threshold = ThresholdLine.horizontal(
        start_hz=settings.start_hz,
        stop_hz=settings.stop_hz,
        level_db=-60.0,
    )

    # 5. Циклы измерений.
    instrument = SimulatedInstrument(
        emitters=[
            Emitter(frequency_hz=104.25e6, bandwidth_hz=125e3, level_db=-35.0),
            Emitter(frequency_hz=107.9375e6, bandwidth_hz=125e3, level_db=-42.0),
        ]
    )
    config = SweepConfig(
        start_hz=settings.start_hz,
        stop_hz=settings.stop_hz,
        resolution_bandwidth_hz=settings.bandwidth_hz,
        video_bandwidth_hz=settings.video_bandwidth_hz,
        points=settings.points,
        attenuation_db=settings.attenuation_db,
        reference_level_db=settings.reference_level_db,
        detector=settings.detector,
        sweep_time_s=settings.sweep_time_s,
    )

    try:
        for index in range(args.cycles):
            trace = instrument.sweep(config)
            recorded_at = datetime.now(timezone.utc)
            record = store.append(
                trace,
                recorded_at=recorded_at,
                latitude=55.751244 + index * 1e-4,
                longitude=37.618423 + index * 1e-4,
            )
            signals.update_from_trace(trace, threshold, recorded_at)
            print(f"Цикл {index + 1:>4}: панорама #{record.id}, сигналов всего {len(signals)}")
            if args.delay:
                import time

                time.sleep(args.delay)
    finally:
        instrument.disconnect()

    # 6. Сохранение реестра и выгрузка данных.
    signals.save(signals_path)
    store.save_threshold(threshold)
    out_cef = store.path.with_suffix(".cef")
    export_cef(store, out_cef)
    print(f"Реестр сигналов: {signals_path}")
    print(f"Выгрузка CEF:    {out_cef}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
