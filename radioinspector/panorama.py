"""База данных панорам.

Реализует раздел 9 «Правила формирования базы данных панорам» руководства
оператора Книги 6:

* база данных панорам хранит все однотипные результаты измерений — с
  одинаковыми начальной и конечной частотой сканирования, шагом перестройки
  и полосой пропускания;
* при смене ключевых параметров настройки средства измерений формируется
  новая база данных панорам; если файл с новым именем уже существует,
  используется старая база и запись продолжается с последней позиции;
* графики максимальных, минимальных и усреднённых значений при продолжении
  записи рассчитываются только по новым результатам измерений, а при загрузке
  базы данных в постобработке — по всем сохранённым данным.

Имя файла формируется из параметров сканирования по схеме, принятой в
оригинале (для контроля диапазона частот):

    Dev<прибор> Ant<антенна> Tract<тракт> From<начало> To<конец> BW<полоса>
    Demod<демодулятор> Step<шаг> Att<аттенюатор> RLev<опорный уровень>
    Points<точки> Det<детектор> VBW<полоса видео> SweepT<время>.pan

Хранилище — SQLite: открытый формат, читаемый любыми сторонними средствами,
что сохраняет принцип исходного комплекса («обработка результатов измерений
силами сторонних разработчиков»).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

from .trace import Trace

#: Префикс расширения файла базы данных панорам.
PANORAMA_SUFFIX = ".pan"

#: Схема таблиц базы данных панорам.
SCHEMA = """
CREATE TABLE IF NOT EXISTS panorama_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS panorama (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   TEXT    NOT NULL,
    latitude      REAL,
    longitude     REAL,
    altitude      REAL,
    speed         REAL,
    heading       REAL,
    levels        BLOB    NOT NULL,
    start_hz      REAL    NOT NULL,
    stop_hz       REAL    NOT NULL,
    step_hz       REAL    NOT NULL,
    unit          TEXT    NOT NULL DEFAULT 'dBm'
);

CREATE TABLE IF NOT EXISTS threshold_line (
    line_index  INTEGER PRIMARY KEY,
    frequencies BLOB    NOT NULL,
    levels      BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_event (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    panorama_id        INTEGER NOT NULL,
    recorded_at        TEXT    NOT NULL,
    frequency_hz       REAL    NOT NULL,
    bandwidth_hz       REAL    NOT NULL,
    power_over_w       REAL    NOT NULL DEFAULT 0,
    level_db           REAL,
    FOREIGN KEY (panorama_id) REFERENCES panorama(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_panorama_recorded ON panorama(recorded_at);
CREATE INDEX IF NOT EXISTS idx_event_frequency ON signal_event(frequency_hz);
"""


@dataclass
class ScanSettings:
    """Параметры задания на сканирование.

    Набор полей соответствует элементам имени файла базы данных панорам.
    """

    device: str = "Sim"
    antenna: str = "Ant"
    tract: str = "Tract"
    start_hz: float = 0.0
    stop_hz: float = 0.0
    bandwidth_hz: float = 0.0
    demodulator: str = "None"
    step_hz: float = 0.0
    attenuation_db: float = 0.0
    reference_level_db: float = 0.0
    points: int = 0
    detector: str = "MaxPeak"
    video_bandwidth_hz: float = 0.0
    sweep_time_s: float = 0.0
    #: Режим работы: ``range`` — контроль диапазона частот, ``direct`` —
    #: исследование одного сигнала (в оригинале используются разные
    #: подкаталоги и разные наборы параметров в имени файла).
    mode: str = "range"
    centre_hz: float = 0.0
    span_hz: float = 0.0

    def key(self) -> tuple:
        """Ключ, однозначно определяющий набор параметров сканирования."""
        return (
            self.mode,
            round(self.start_hz, 6),
            round(self.stop_hz, 6),
            round(self.step_hz, 6),
            round(self.bandwidth_hz, 6),
            round(self.centre_hz, 6),
            round(self.span_hz, 6),
            self.device,
            self.antenna,
            self.tract,
            self.demodulator,
            self.detector,
            int(self.points),
            round(self.attenuation_db, 3),
            round(self.reference_level_db, 3),
            round(self.video_bandwidth_hz, 6),
            round(self.sweep_time_s, 6),
        )

    def filename(self) -> str:
        """Имя файла базы данных панорам по параметрам сканирования.

        Для контроля диапазона частот используются ``From``/``To``, для
        исследования одного сигнала — ``Cnt``/``Span``.
        """
        common = (
            f"Dev{self.device} Ant{self.antenna} Tract{self.tract} "
            f"BW{_fmt(self.bandwidth_hz / 1000.0)} Demod{self.demodulator} "
            f"Step{_fmt(self.step_hz / 1000.0)} Att{_fmt_int(self.attenuation_db)} "
            f"RLev{_fmt_int(self.reference_level_db)} Points{self.points} "
            f"Det{self.detector} VBW{_fmt(self.video_bandwidth_hz / 1000.0)} "
            f"SweepT{_fmt(self.sweep_time_s * 1000.0)}"
        )
        if self.mode == "direct":
            common += f" Cnt{_fmt(self.centre_hz / 1e6)} Span{_fmt(self.span_hz / 1e6)}"
        else:
            common += f" From{_fmt(self.start_hz / 1e6)} To{_fmt(self.stop_hz / 1e6)}"
        return common + PANORAMA_SUFFIX

    def slug(self) -> str:
        """Безопасное имя файла без пробелов и запятых."""
        return (
            self.filename()
            .replace(" ", "_")
            .replace(",", "_")
            .replace(";", "_")
            .replace("/", "_")
        )


def _fmt(value: float) -> str:
    """Форматирование дробного числа в стиле оригинальных имён файлов.

    В исходном комплексе параметры полосы, шага и времени записи записываются
    с десятичной точкой (``BW100.0``, ``Step100.0``, ``SweepT0.0``), а частоты
    границ диапазона — с точностью до десятых (``From65.7``, ``To145.7``).
    """
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    if not text or text == "-0":
        return "0.0"
    if "." not in text:
        text += ".0"
    return text


def _fmt_int(value: float) -> str:
    """Форматирование целочисленного параметра (аттенюатор, опорный уровень)."""
    return str(int(round(value)))


@dataclass
class PanoramaRecord:
    """Запись панорамы: снимок спектра с привязкой ко времени и координатам."""

    recorded_at: datetime
    trace: Trace
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    speed: Optional[float] = None
    heading: Optional[float] = None
    id: Optional[int] = None

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class PanoramaStore:
    """Хранилище базы данных панорам на основе SQLite."""

    def __init__(self, path: str | Path, settings: Optional[ScanSettings] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        # Соединение используется из потоков веб-сервера, поэтому проверка
        # принадлежности к потоку отключена; обращения сериализуются
        # собственным замком.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        if settings is not None:
            try:
                existing = self.read_settings()
                if existing is not None and existing.key() != settings.key():
                    raise ValueError("Несовместимые настройки (settings) существующей базы панорам")
                if existing is None and self.count:
                    raise ValueError("Нельзя назначить settings непустой базе без метаданных")
                self._write_settings(settings)
            except Exception:
                self._conn.close()
                raise

    # -- жизненный цикл ----------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> "PanoramaStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- метаданные --------------------------------------------------------

    def _write_settings(self, settings: ScanSettings) -> None:
        payload = {
            "device": settings.device,
            "antenna": settings.antenna,
            "tract": settings.tract,
            "start_hz": settings.start_hz,
            "stop_hz": settings.stop_hz,
            "step_hz": settings.step_hz,
            "bandwidth_hz": settings.bandwidth_hz,
            "demodulator": settings.demodulator,
            "attenuation_db": settings.attenuation_db,
            "reference_level_db": settings.reference_level_db,
            "points": settings.points,
            "detector": settings.detector,
            "video_bandwidth_hz": settings.video_bandwidth_hz,
            "sweep_time_s": settings.sweep_time_s,
            "mode": settings.mode,
            "centre_hz": settings.centre_hz,
            "span_hz": settings.span_hz,
        }
        with self._lock:
            self._conn.executemany(
                "INSERT INTO panorama_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v)) for k, v in payload.items()],
            )
            self._conn.commit()

    def read_settings(self) -> Optional[ScanSettings]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM panorama_meta").fetchall()
        if not rows:
            return None
        data = {row["key"]: json.loads(row["value"]) for row in rows}
        return ScanSettings(**data)

    @property
    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM panorama").fetchone()
        return int(row["c"])

    # -- запись ------------------------------------------------------------

    def append(
        self,
        trace: Trace,
        recorded_at: Optional[datetime] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        altitude: Optional[float] = None,
        speed: Optional[float] = None,
        heading: Optional[float] = None,
    ) -> PanoramaRecord:
        """Дописывает панораму в конец базы данных."""
        recorded_at = recorded_at or datetime.now(timezone.utc)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO panorama(recorded_at, latitude, longitude, altitude, speed, "
                "heading, levels, start_hz, stop_hz, step_hz, unit) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    recorded_at.isoformat(),
                    latitude,
                    longitude,
                    altitude,
                    speed,
                    heading,
                    trace.levels.astype("<f8").tobytes(),
                    float(trace.frequencies[0]) if trace.size else 0.0,
                    float(trace.frequencies[-1]) if trace.size else 0.0,
                    float(trace.step) if trace.size > 1 else 0.0,
                    trace.unit,
                ),
            )
            self._conn.commit()
        return PanoramaRecord(
            recorded_at=recorded_at,
            trace=trace,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            speed=speed,
            heading=heading,
            id=int(cur.lastrowid),
        )

    # -- чтение ------------------------------------------------------------

    def _trace_from_row(self, row: sqlite3.Row) -> Trace:
        levels = np.frombuffer(row["levels"], dtype="<f8")
        start = float(row["start_hz"])
        stop = float(row["stop_hz"])
        step = float(row["step_hz"])
        n = levels.size
        if n == 0:
            return Trace(np.array([]), np.array([]), row["unit"])
        if n == 1:
            frequencies = np.array([start])
        else:
            frequencies = start + np.arange(n) * step
            if stop > start and abs(frequencies[-1] - stop) > step * 0.5:
                frequencies[-1] = stop
        return Trace(frequencies, levels, row["unit"])

    def iter_panoramas(self) -> Iterable[PanoramaRecord]:
        """Последовательное чтение панорам в порядке записи."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM panorama ORDER BY id").fetchall()
        for row in rows:
            yield PanoramaRecord(
                recorded_at=datetime.fromisoformat(row["recorded_at"]),
                trace=self._trace_from_row(row),
                latitude=row["latitude"],
                longitude=row["longitude"],
                altitude=row["altitude"],
                speed=row["speed"],
                heading=row["heading"],
                id=int(row["id"]),
            )

    def get(self, index: int) -> PanoramaRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM panorama ORDER BY id LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
        if row is None:
            raise IndexError(index)
        return PanoramaRecord(
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            trace=self._trace_from_row(row),
            latitude=row["latitude"],
            longitude=row["longitude"],
            altitude=row["altitude"],
            speed=row["speed"],
            heading=row["heading"],
            id=int(row["id"]),
        )

    def latest(self) -> Optional[PanoramaRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM panorama ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return PanoramaRecord(
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            trace=self._trace_from_row(row),
            latitude=row["latitude"],
            longitude=row["longitude"],
            altitude=row["altitude"],
            speed=row["speed"],
            heading=row["heading"],
            id=int(row["id"]),
        )

    # -- агрегированные графики -------------------------------------------

    def max_trace(self) -> Optional[Trace]:
        """График накопленных максимумов по всей базе данных."""
        return self._aggregate("max")

    def min_trace(self) -> Optional[Trace]:
        """График накопленных минимумов по всей базе данных."""
        return self._aggregate("min")

    def average_trace(self) -> Optional[Trace]:
        """График усреднённых значений по всей базе данных."""
        return self._aggregate("average")

    def _aggregate(self, method: str) -> Optional[Trace]:
        traces = [rec.trace for rec in self.iter_panoramas() if rec.trace.size]
        if not traces:
            return None
        size = min(tr.size for tr in traces)
        stack = np.vstack([tr.levels[:size] for tr in traces])
        freqs = traces[0].frequencies[:size]
        if method == "max":
            levels = stack.max(axis=0)
        elif method == "min":
            levels = stack.min(axis=0)
        else:
            levels = stack.mean(axis=0)
        return Trace(freqs, levels, traces[0].unit)

    def level_grid(self) -> tuple[np.ndarray, np.ndarray]:
        """Двумерная сетка «время × частота» для построения спектрограммы.

        Возвращает матрицу уровней (строки — панорамы, столбцы — частоты) и
        массив моментов времени записи.
        """
        records = [rec for rec in self.iter_panoramas() if rec.trace.size]
        if not records:
            return np.zeros((0, 0)), np.zeros((0,))
        size = min(rec.trace.size for rec in records)
        grid = np.vstack([rec.trace.levels[:size] for rec in records])
        times = np.array([rec.recorded_at.timestamp() for rec in records])
        return grid, times

    def frequencies(self) -> np.ndarray:
        for rec in self.iter_panoramas():
            if rec.trace.size:
                return rec.trace.frequencies
        return np.array([])

    # -- выборка данных ----------------------------------------------------

    def select(
        self,
        start_hz: Optional[float] = None,
        stop_hz: Optional[float] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        latitude_range: Optional[tuple[float, float]] = None,
        longitude_range: Optional[tuple[float, float]] = None,
    ) -> list[PanoramaRecord]:
        """Выборка подмножества данных по частоте, времени и координатам.

        Соответствует операции «выборка» из исходной базы данных панорам с
        использованием параметров: частота, время, географические координаты.
        """
        result: list[PanoramaRecord] = []
        for rec in self.iter_panoramas():
            if start_hz is not None and rec.trace.stop < start_hz:
                continue
            if stop_hz is not None and rec.trace.start > stop_hz:
                continue
            if since is not None and rec.recorded_at < since:
                continue
            if until is not None and rec.recorded_at > until:
                continue
            if latitude_range and rec.latitude is not None:
                lo, hi = latitude_range
                if not (lo <= rec.latitude <= hi):
                    continue
            if longitude_range and rec.longitude is not None:
                lo, hi = longitude_range
                if not (lo <= rec.longitude <= hi):
                    continue
            result.append(rec)
        return result

    def extract_subset(
        self,
        target_path: str | Path,
        start_hz: Optional[float] = None,
        stop_hz: Optional[float] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> "PanoramaStore":
        """Сохранение подмножества данных в отдельную базу данных панорам."""
        subset = PanoramaStore(target_path, self.read_settings())
        for rec in self.select(start_hz, stop_hz, since, until):
            trace = rec.trace
            if start_hz is not None or stop_hz is not None:
                trace = trace.slice(
                    start_hz if start_hz is not None else trace.start,
                    stop_hz if stop_hz is not None else trace.stop,
                )
            subset.append(
                trace,
                recorded_at=rec.recorded_at,
                latitude=rec.latitude,
                longitude=rec.longitude,
                altitude=rec.altitude,
                speed=rec.speed,
                heading=rec.heading,
            )
        return subset

    # -- линии порога ------------------------------------------------------

    def save_threshold(self, line) -> None:
        """Сохранение линии порога в базу данных панорам."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO threshold_line(line_index, frequencies, levels) VALUES(?,?,?) "
                "ON CONFLICT(line_index) DO UPDATE SET frequencies=excluded.frequencies, "
                "levels=excluded.levels",
                (
                    int(line.index),
                    line.frequencies.astype("<f8").tobytes(),
                    line.levels.astype("<f8").tobytes(),
                ),
            )
            self._conn.commit()

    def load_threshold(self, index: int = 0):
        """Загрузка линии порога из базы данных панорам."""
        from .threshold import ThresholdLine

        with self._lock:
            row = self._conn.execute(
                "SELECT frequencies, levels FROM threshold_line WHERE line_index=?", (index,)
            ).fetchone()
        if row is None:
            return None
        return ThresholdLine(
            np.frombuffer(row["frequencies"], dtype="<f8"),
            np.frombuffer(row["levels"], dtype="<f8"),
            index,
        )

    def threshold_indices(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT line_index FROM threshold_line ORDER BY line_index"
            ).fetchall()
        return [int(row["line_index"]) for row in rows]

    # -- события сигналов ---------------------------------------------------

    def add_signal_event(
        self,
        panorama_id: int,
        frequency_hz: float,
        bandwidth_hz: float,
        power_over_w: float = 0.0,
        level_db: float = float("nan"),
        recorded_at: Optional[datetime] = None,
    ) -> None:
        """Регистрация события превышения порога с привязкой к панораме."""
        recorded_at = recorded_at or datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                "INSERT INTO signal_event(panorama_id, recorded_at, frequency_hz, "
                "bandwidth_hz, power_over_w, level_db) VALUES(?,?,?,?,?,?)",
                (
                    panorama_id,
                    recorded_at.isoformat(),
                    frequency_hz,
                    bandwidth_hz,
                    power_over_w,
                    level_db,
                ),
            )
            self._conn.commit()

    def signal_events(self, frequency_hz: Optional[float] = None) -> list[sqlite3.Row]:
        """События превышения порога, при необходимости по частоте."""
        with self._lock:
            if frequency_hz is None:
                return self._conn.execute(
                    "SELECT * FROM signal_event ORDER BY id"
                ).fetchall()
            return self._conn.execute(
                "SELECT * FROM signal_event WHERE ABS(frequency_hz - ?) <= ? ORDER BY id",
                (frequency_hz, frequency_hz / 2.0),
            ).fetchall()


class PanoramaLibrary:
    """Каталог баз данных панорам.

    Реализует правило формирования имён файлов и продолжения записи: при
    изменении параметров сканирования выбирается существующий файл с
    соответствующим именем либо создаётся новый.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, settings: ScanSettings) -> Path:
        """Путь к файлу базы данных для заданных параметров сканирования."""
        return self.root / settings.slug()

    def resolve(self, settings: ScanSettings) -> tuple[Path, bool]:
        """Возвращает путь к базе и признак «продолжение существующей записи»."""
        path = self.path_for(settings)
        return path, path.exists()

    def open_for(self, settings: ScanSettings) -> tuple[PanoramaStore, bool]:
        """Открывает базу данных для записи, продолжая существующую при наличии."""
        path, existed = self.resolve(settings)
        return PanoramaStore(path, settings), existed

    def list_stores(self) -> list[Path]:
        return sorted(self.root.glob(f"*{PANORAMA_SUFFIX}"))


def default_library_root() -> Path:
    """Каталог по умолчанию для хранения баз данных панорам."""
    return Path.home() / "OpenRadioInspector" / "DataBase" / "Range control"


def generate_synthetic_trace(
    settings: ScanSettings,
    seed: int = 0,
    signals: Optional[Sequence[tuple[float, float, float]]] = None,
    noise_db: float = -100.0,
) -> Trace:
    """Генерация синтетической панорамы для демонстрации и тестов.

    Параметр ``signals`` задаёт тройки (частота в Гц, ширина полоса в Гц,
    уровень в дБ).
    """
    rng = np.random.default_rng(seed)
    points = settings.points or 801
    freqs = np.linspace(settings.start_hz, settings.stop_hz, points)
    levels = np.full(points, noise_db) + rng.normal(0.0, 1.5, points)
    for freq, width, level in signals or []:
        half = max(width / 2.0, settings.step_hz)
        mask = np.abs(freqs - freq) <= half
        profile = np.clip(1.0 - (np.abs(freqs[mask] - freq) / half) ** 2, 0.0, 1.0)
        levels[mask] = level + 12.0 * np.log10(np.maximum(profile, 1e-6))
    return Trace(freqs, levels, "dBm")
