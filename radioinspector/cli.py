"""Командный интерфейс OpenRadioInspector.

Примеры
--------

Сгенерировать демонстрационную базу данных панорам::

    python -m radioinspector demo --output ./demo

Выполнить сканирование диапазона частот генератором измерений::

    python -m radioinspector scan --sim --start 30e6 --stop 90e6 \
        --output ./data --cycles 20

Оценить загрузку диапазона по сохранённой базе::

    python -m radioinspector occupancy --database ./data/<файл>.pan --threshold -80

Сформировать протокол измерений::

    python -m radioinspector report --database ./data/<файл>.pan --format docx --output protocol.docx
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import __version__
from .assignments import Assignment, AssignmentDatabase
from .devices.registry import open_instrument
from .devices.base import SweepConfig
from .export import export_cef, export_panoramas_csv, export_signals_csv, export_trace_csv
from .jsonio import dumps as json_dumps
from .keywords import CATALOG, keyword_group
from .geoloc import (
    Bearing,
    MeasurementPoint,
    build_coverage_map,
    triangulate_bearings,
)
from .interference import detect_interference
from .measurements import measure_signal
from .occupancy import efficiency_from_store, occupancy_from_store
from .panorama import PanoramaLibrary, PanoramaStore, ScanSettings
from .docx_report import write_docx_report
from .reporting import DEFAULT_TEMPLATE, ProtocolTemplate, build_context
from .signals import SignalList
from .tasks import TaskBook
from .threshold import ThresholdLine
from .trace import Trace
from .units import convert


def _print_json(payload: dict) -> None:
    print(json_dumps(payload, indent=2))


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> int:
    """Генерация демонстрационной базы данных панорам."""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    settings = ScanSettings(
        device="Sim",
        antenna="LOG-ANT",
        tract="CABLE-3m",
        start_hz=30e6,
        stop_hz=130e6,
        bandwidth_hz=100e3,
        step_hz=125e3,
        points=801,
        detector="MaxPeak",
        reference_level_db=0.0,
        video_bandwidth_hz=100e3,
        sweep_time_s=0.0,
    )

    library = PanoramaLibrary(output)
    store, _existed = library.open_for(settings)

    with store:
        emitters = [
            (102.5e6, 200e3, -35.0),
            (104.3e6, 25e3, -55.0),
            (107.9e6, 180e3, -42.0),
            (45.2e6, 12e3, -70.0),
        ]
        base_time = datetime.now(timezone.utc) - timedelta(minutes=args.cycles)
        rng = np.random.default_rng(7)

        for cycle in range(args.cycles):
            trace = _synthetic_panorama(settings, emitters, rng, cycle)
            latitude = 55.75 + cycle * 0.0008
            longitude = 37.62 + cycle * 0.0011
            store.append(
                trace,
                recorded_at=base_time + timedelta(seconds=cycle * 3),
                latitude=latitude,
                longitude=longitude,
                altitude=150.0,
                speed=8.0,
                heading=45.0,
            )

        # Линия порога по умолчанию: горизонтальная прямая на уровне 2/3
        # вертикальной сетки. Уровень сетки берётся по накопленному графику
        # максимумов за весь сеанс наблюдения.
        max_trace = store.max_trace()
        assert max_trace is not None
        line = ThresholdLine.default_for(max_trace)
        store.save_threshold(line)

        signals = SignalList()
        for record in store.iter_panoramas():
            signals.update_from_trace(record.trace, line, record.recorded_at)
        signals.save(output / "signals.json")

        print(f"База данных панорам: {store.path}")
        print(f"Записей: {store.count}, сигналов в списке: {len(signals)}")
        return 0


def _synthetic_panorama(settings, emitters, rng, cycle: int) -> Trace:
    points = settings.points
    freqs = np.linspace(settings.start_hz, settings.stop_hz, points)
    levels = np.full(points, -100.0) + rng.normal(0.0, 1.8, points)
    for freq, width, level in emitters:
        # Медленная амплитудная модуляция излучений.
        modulation = 1.0 if (cycle + int(freq / 1e6)) % 3 else 0.0
        if modulation == 0.0 and width > 100e3:
            continue
        half = max(width / 2.0, settings.step_hz)
        mask = np.abs(freqs - freq) <= half
        profile = np.clip(1.0 - (np.abs(freqs[mask] - freq) / half) ** 2, 0.0, 1.0)
        levels[mask] = level + 12.0 * np.log10(np.maximum(profile, 1e-6))
    return Trace(freqs, levels, "dBm")


def cmd_scan(args: argparse.Namespace) -> int:
    """Сканирование диапазона частот с сохранением в базу данных панорам."""
    connection = args.sim and "sim://" or args.connection
    instrument = open_instrument(connection)

    settings = ScanSettings(
        device=getattr(instrument, "device_name", "Unknown"),
        antenna=args.antenna,
        tract=args.tract,
        start_hz=args.start,
        stop_hz=args.stop,
        bandwidth_hz=args.bandwidth,
        step_hz=(args.stop - args.start) / max(1, args.points - 1),
        points=args.points,
        detector=args.detector,
        reference_level_db=args.reference_level,
        video_bandwidth_hz=args.bandwidth,
    )

    library = PanoramaLibrary(args.output)
    store, existed = library.open_for(settings)
    print(f"База данных: {store.path} ({'продолжение записи' if existed else 'новая'})")

    config = SweepConfig(
        start_hz=args.start,
        stop_hz=args.stop,
        resolution_bandwidth_hz=args.bandwidth,
        video_bandwidth_hz=args.bandwidth,
        points=args.points,
        reference_level_db=args.reference_level,
        detector=args.detector,
    )

    with instrument:
        for cycle in range(args.cycles):
            trace = instrument.sweep(config)
            store.append(trace)
            print(
                f"Цикл {cycle + 1}/{args.cycles}: максимум {trace.peak:.1f} дБ, "
                f"точек {trace.size}"
            )
            if args.delay:
                import time

                time.sleep(args.delay)

    print(f"Записано панорам: {store.count}")
    return 0


def cmd_occupancy(args: argparse.Namespace) -> int:
    """Оценка загрузки диапазона частот."""
    with PanoramaStore(args.database) as store:
        result = occupancy_from_store(
            store, args.threshold, start_hz=args.start, stop_hz=args.stop
        )
    payload = result.as_dict()
    efficiency = None
    if args.efficiency:
        with PanoramaStore(args.database) as store:
            efficiency = efficiency_from_store(
                store, args.threshold, start_hz=args.start, stop_hz=args.stop
            )
        payload["band_efficiency"] = efficiency.as_dict()
    if args.json:
        _print_json(payload)
    else:
        print(f"Диапазон: {result.start_hz / 1e6:.3f}–{result.stop_hz / 1e6:.3f} МГц")
        print(f"Порог: {result.threshold_db:.1f} дБ")
        print(f"Каналов: {result.channel_count}, занято: {result.occupied_channels}")
        print(f"Загрузка диапазона: {result.band_occupancy * 100:.2f} %")
        if efficiency is not None:
            print(
                f"Эффективность использования диапазона: "
                f"{efficiency.efficiency * 100:.2f} % "
                f"(n={efficiency.exceedances}, N={efficiency.measurements})"
            )
            if efficiency.observation_time_s:
                print(
                    f"Время наблюдения: {efficiency.observation_time_s:.0f} с, "
                    f"интервал между измерениями: средний "
                    f"{efficiency.mean_interval_s:.3f} с, минимальный "
                    f"{efficiency.min_interval_s:.3f} с, максимальный "
                    f"{efficiency.max_interval_s:.3f} с"
                )
            if args.verbose:
                print()
                print(efficiency.summary())
    if args.output:
        Path(args.output).write_text(json_dumps(payload, indent=2), encoding="utf-8")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    """Измерение параметров сигнала по сохранённой базе данных."""
    with PanoramaStore(args.database) as store:
        record = store.latest()
        if record is None:
            print("База данных пуста", file=sys.stderr)
            return 1
        trace = record.trace
        if args.start or args.stop:
            start = args.start if args.start else trace.start
            stop = args.stop if args.stop else trace.stop
            if start < trace.start or stop > trace.stop:
                print(
                    f"Запрошенный диапазон {start / 1e6:.3f}–{stop / 1e6:.3f} МГц "
                    f"выходит за пределы измеренного диапазона "
                    f"{trace.start / 1e6:.3f}–{trace.stop / 1e6:.3f} МГц",
                    file=sys.stderr,
                )
                return 2
            trace = trace.slice(start, stop)
        result = measure_signal(
            trace,
            beta=args.beta,
            n_db=args.n_db,
            field_method=args.method,
            norm_hz=args.norm,
        )
    if args.json:
        _print_json(result)
    else:
        ob = result["occupied_bandwidth_beta2"]
        bl = result["bandwidth_at_level"]
        fs = result["field_strength"]
        cf = result["center_frequency_centroid"]
        print(f"Полоса β/2 ({ob['method']}): {ob['bandwidth_hz'] / 1e3:.3f} кГц")
        print(f"Полоса по уровню {bl['method']}: {bl['bandwidth_hz'] / 1e3:.3f} кГц")
        print(f"Напряжённость поля: {fs['value']:.1f} дБ")
        if math.isfinite(cf["value"]):
            print(f"Центральная частота: {cf['value'] / 1e6:.6f} МГц")
        for block in (ob, bl, fs, cf):
            for warning in block.get("warnings", []):
                print(f"  ! {warning}", file=sys.stderr)
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    """Анализ списка обнаруженных сигналов."""
    signals = SignalList.load(args.file)
    rows = signals.to_rows()
    if args.json:
        _print_json(rows)
    else:
        print(f"{'№':>3} {'Частота, МГц':>14} {'Полоса, кГц':>12} {'Обнаружений':>12}  Время")
        for index, row in enumerate(rows, 1):
            print(
                f"{index:>3} {row['frequency_hz'] / 1e6:>14.6f} "
                f"{row['bandwidth_hz'] / 1e3:>12.2f} {row['event_count']:>12} "
                f"  {row['first_seen']}"
            )
    if args.export:
        export_signals_csv(signals, args.export)
        print(f"Выгружено: {args.export}")
    return 0


def cmd_assignments(args: argparse.Namespace) -> int:
    """Работа с базой данных частотных присвоений."""
    with AssignmentDatabase(args.database) as db:
        if args.import_csv:
            count = db.import_csv(args.import_csv)
            print(f"Загружено передатчиков: {count}")
        if args.export_csv:
            db.export_csv(args.export_csv)
            print(f"Выгружено: {args.export_csv}")
        if args.list:
            for item in db.assignments():
                print(
                    f"{item.tx_frequency_hz / 1e6:>12.6f} МГц  "
                    f"{item.bandwidth_hz / 1e3:>10.1f} кГц  {item.name}"
                )
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """Построение карты распределения напряжённости поля."""
    with PanoramaStore(args.database) as store:
        points = [
            MeasurementPoint(
                latitude=rec.latitude,
                longitude=rec.longitude,
                level_db=rec.trace.peak,
            )
            for rec in store.iter_panoramas()
            if rec.latitude is not None and rec.longitude is not None
        ]
    if not points:
        print("В базе нет измерений с координатами", file=sys.stderr)
        return 1
    coverage = build_coverage_map(
        points, rows=args.rows, cols=args.cols, aggregation=args.aggregation
    )
    if args.geojson:
        Path(args.geojson).write_text(
            json_dumps(coverage.to_geojson(), indent=2), encoding="utf-8"
        )
        print(f"Карта сохранена: {args.geojson}")
    else:
        print(f"Ячеек: {len(coverage.cells)}")
        print(
            f"Уровень: от {coverage.min_level_db:.1f} до {coverage.max_level_db:.1f} дБ"
        )
    return 0


def cmd_df(args: argparse.Namespace) -> int:
    """Амплитудная пеленгация по введённым пеленгам."""
    bearings: list[Bearing] = []
    for item in args.bearing:
        parts = [p.strip() for p in item.split(",")]
        if len(parts) < 3:
            print(f"Некорректный пеленг: {item}", file=sys.stderr)
            return 1
        lat, lon, azimuth = float(parts[0]), float(parts[1]), float(parts[2])
        declination = float(parts[3]) if len(parts) > 3 else args.declination
        bearings.append(Bearing(lat, lon, azimuth, declination))

    fix = triangulate_bearings(bearings)
    if fix is None:
        print("Недостаточно пеленгов: требуется не менее трёх", file=sys.stderr)
        return 1
    print(f"Координаты источника: {fix.latitude:.6f}, {fix.longitude:.6f}")
    print(f"Максимальное отклонение пеленгов: {fix.residual_deg:.2f}°")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Формирование протокола измерений."""
    with PanoramaStore(args.database) as store:
        record = store.latest()
        trace = record.trace if record else None
        measurements = measure_signal(trace) if trace is not None else {}
        signals = []
        signal_file = Path(args.database).with_name("signals.json")
        if signal_file.exists():
            signals = SignalList.load(signal_file).to_rows()
        context = build_context(
            measurements=measurements,
            store=store,
            signals=signals,
            operator=args.operator,
            place=args.place,
        )

    if args.template:
        template = ProtocolTemplate.from_file(args.template)
    else:
        template = ProtocolTemplate("protocol", DEFAULT_TEMPLATE, "Стандартный протокол")

    if args.format == "docx":
        if not args.output:
            print("Для формата docx требуется --output", file=sys.stderr)
            return 2
        path = write_docx_report(template, context, args.output)
    else:
        text = template.render(context)
        if args.output:
            path = Path(args.output)
            path.write_text(text, encoding="utf-8")
        else:
            print(text)
            return 0
    print(f"Протокол сохранён: {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Выгрузка данных в форматы обмена."""
    with PanoramaStore(args.database) as store:
        if args.format == "cef":
            path = export_cef(store, args.output, location_name=args.place)
        elif args.format == "csv":
            path = export_panoramas_csv(store, args.output)
        else:
            print(f"Неизвестный формат: {args.format}", file=sys.stderr)
            return 1
    print(f"Выгружено: {path}")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    """Преобразование величины между единицами измерения."""
    value = convert(args.value, args.from_unit, args.to_unit, args.impedance)
    print(f"{value:.6g} {args.to_unit}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Запуск веб-интерфейса."""
    from .web.server import serve

    serve(
        host=args.host,
        port=args.port,
        database=args.database,
        session_database=args.session_database,
        archive_max_bytes=args.archive_max_bytes,
    )
    return 0


def cmd_keywords(args: argparse.Namespace) -> int:
    """Словарь ключевых слов шаблонов протоколов измерений."""
    if args.json:
        _print_json({"keywords": CATALOG})
        return 0
    groups: dict[str, list[str]] = {}
    for key in sorted(CATALOG):
        groups.setdefault(keyword_group(key), []).append(key)
    for group, keys in groups.items():
        print(f"\n{group} ({len(keys)})")
        for key in keys:
            print(f"  {key:<24} {CATALOG[key]}")
    print(f"\nВсего ключевых слов: {len(CATALOG)}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """База данных планирования: перечень заданий и состояние очереди."""
    book = TaskBook(args.database).load()
    if args.json:
        _print_json({"tasks": [task.as_dict() for task in book.tasks]})
        return 0
    if not book.tasks:
        print("База данных планирования пуста")
        return 0
    print(f"Заданий: {len(book.tasks)}, в очереди: {len(book.queue())}")
    for task in book.tasks:
        start = task.start_at.strftime("%d.%m.%Y %H:%M") if task.start_at else "—"
        line = f"{task.name:<24} {task.status:<24} старт {start}"
        if task.outcome:
            line += f"  [{task.outcome_kind()}: {task.outcome}]"
        print(line)
        for message in task.validate():
            print(f"    ! {message}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    """Перечень поддерживаемых средств измерений."""
    from .devices.registry import describe_devices

    for item in describe_devices():
        print(f"{item['key']:<10} {item['title']}")
        print(f"{'':<10} транспорт: {item['transport']}; приборы: {item['notes']}")
    return 0


# --------------------------------------------------------------------------
# Разбор аргументов
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radio-inspector",
        description=(
            "OpenRadioInspector — комплекс программ для исследования "
            "радиочастотного спектра и измерения параметров высокочастотных "
            "излучений"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="сгенерировать демонстрационные данные")
    p.add_argument("--output", default="./demo-data")
    p.add_argument("--cycles", type=int, default=40)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("scan", help="сканирование диапазона частот")
    p.add_argument("--connection", default="sim://", help="строка подключения прибора")
    p.add_argument("--sim", action="store_true", help="использовать генератор измерений")
    p.add_argument("--start", type=float, default=30e6)
    p.add_argument("--stop", type=float, default=90e6)
    p.add_argument("--bandwidth", type=float, default=100e3)
    p.add_argument("--points", type=int, default=801)
    p.add_argument("--cycles", type=int, default=10)
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--antenna", default="Ant")
    p.add_argument("--tract", default="Tract")
    p.add_argument("--detector", default="MaxPeak")
    p.add_argument("--reference-level", type=float, default=0.0)
    p.add_argument("--output", default="./data")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("occupancy", help="оценка загрузки диапазона")
    p.add_argument("--database", required=True)
    p.add_argument("--threshold", type=float, default=-80.0)
    p.add_argument("--start", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--output")
    p.add_argument(
        "--verbose",
        action="store_true",
        help="вывести сводку наблюдения в формате исходного комплекса",
    )
    p.add_argument(
        "--efficiency",
        action="store_true",
        help="дополнительно вычислить эффективность использования диапазона "
        "(доля моментов наблюдения с превышением порога)",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_occupancy)

    p = sub.add_parser("measure", help="измерение параметров сигнала")
    p.add_argument("--database", required=True)
    p.add_argument("--start", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--beta", type=float, default=1.0)
    p.add_argument("--n-db", type=float, default=30.0)
    p.add_argument(
        "--method", default="median", choices=["median", "mean", "max"]
    )
    p.add_argument(
        "--norm",
        type=float,
        help="норма на контрольную полосу занимаемых частот, Гц: "
        "для проверки соответствия норме с допуском 10 %%",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("signals", help="список обнаруженных сигналов")
    p.add_argument("--file", required=True)
    p.add_argument("--export")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("assignments", help="база данных частотных присвоений")
    p.add_argument("--database", default=":memory:")
    p.add_argument("--import-csv")
    p.add_argument("--export-csv")
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_assignments)

    p = sub.add_parser("coverage", help="карта распределения напряжённости поля")
    p.add_argument("--database", required=True)
    p.add_argument("--rows", type=int, default=40)
    p.add_argument("--cols", type=int, default=40)
    p.add_argument(
        "--aggregation", default="mean", choices=["mean", "min", "max"]
    )
    p.add_argument("--geojson")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("df", help="амплитудная пеленгация")
    p.add_argument(
        "--bearing",
        action="append",
        required=True,
        help="широта,долгота,магнитный азимут[,склонение]",
    )
    p.add_argument("--declination", type=float, default=0.0)
    p.set_defaults(func=cmd_df)

    p = sub.add_parser("report", help="формирование протокола")
    p.add_argument("--database", required=True)
    p.add_argument("--format", choices=["md", "docx"], default="md")
    p.add_argument("--template")
    p.add_argument("--output")
    p.add_argument("--operator", default="")
    p.add_argument("--place", default="")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="выгрузка данных")
    p.add_argument("--database", required=True)
    p.add_argument("--format", default="cef", choices=["cef", "csv"])
    p.add_argument("--output", required=True)
    p.add_argument("--place", default="Unknown")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("convert", help="преобразование единиц измерения")
    p.add_argument("value", type=float)
    p.add_argument("--from", dest="from_unit", required=True)
    p.add_argument("--to", dest="to_unit", required=True)
    p.add_argument("--impedance", type=float, default=50.0)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("serve", help="веб-интерфейс")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--database", help="база спектральных панорам (*.pan)")
    p.add_argument(
        "--session-database",
        help="SQLite-архив общих сеансов и измерений; без него используется память",
    )
    p.add_argument(
        "--archive-max-bytes", type=int,
        help="положительный лимит логического объёма SQLite в байтах; требует "
             "--session-database; без опции лимита нет, автоматического удаления нет",
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "keywords", help="словарь ключевых слов шаблонов протоколов измерений"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_keywords)

    p = sub.add_parser("tasks", help="база данных планирования заданий")
    p.add_argument("--database", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("devices", help="перечень поддерживаемого оборудования")
    p.set_defaults(func=cmd_devices)

    return parser


def main(argv: list[str] | None = None) -> int:
    # CLI output is UTF-8 even when redirected on a legacy Windows locale.
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is cmd_serve and args.archive_max_bytes is not None:
        if args.archive_max_bytes <= 0:
            parser.error("--archive-max-bytes должен быть положительным целым числом")
        if not args.session_database:
            parser.error("--archive-max-bytes требует --session-database")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
