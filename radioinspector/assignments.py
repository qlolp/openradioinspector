"""База данных частотных присвоений.

Реализует раздел 3 «База данных частотных присвоений» руководства оператора
Книги 6. Структура записи воспроизводит оригинальную:

передатчики
    1. Наименование передатчика
    2. «Владелец» — выбирается из списка операторов
    3. «Тип диапазона частот» — выбирается из списка диапазонов
       (например, GSM 900, телевидение, радиовещание)
    4. Частота передачи
    5. Частота приема
    6. Полоса необходимых (занимаемых) частот
    7. Класс излучений
    8. Максимальная мощность передатчика
    9. «Лицензия»
    10. «Антенна»
    11. Высота подвеса антенны
    12. Азимут главного лепестка антенны
    13. Долгота
    14. Широта
    15. Адрес установки
    16. Комментарии

владелец
    1. Наименование
    2. Данные о регистрации
    3. Постановка на учет
    4. E-Mail
    5. Internet
    6. Общие телефоны
    7. Общий факс
    8. Юридический адрес
    9. Почтовый адрес
    10. ИНН
    11. Банковские реквизиты
    12. Комментарии

контактные лица (для таблицы «владелец»)
    1. Контактное лицо
    2. Должность
    3. Телефоны
    4. Факс
    5. Адрес
    6. Комментарии

тип диапазона частот
    1. Имя диапазона
    2. Частоты передачи от __ и до __
    3. Частоты приема от __ и до __
    4. Класс передачи
    5. Шаг сетки частот
    6. Комментарии

лицензия (разрешение на использование радиочастот)
    1. Страницы (отсканированные рисунки в формате JPG)
    2. Количество страниц
    3. Комментарий

антенна
    1. Наименование антенны
    2. Диаграмма направленности (горизонтальная)
    3. Диаграмма направленности (вертикальная)
    4. Уровень бокового лепестка
    5. Поляризация
    6. Коэффициент усиления
    7. Входное сопротивление
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS transmitter (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    owner_id            INTEGER REFERENCES owner(id),
    band_type_id        INTEGER REFERENCES band_type(id),
    tx_frequency_hz     REAL,
    rx_frequency_hz     REAL,
    bandwidth_hz        REAL DEFAULT 0,
    emission_class      TEXT,
    max_power_w         REAL,
    license_id          INTEGER REFERENCES license(id),
    antenna_id          INTEGER REFERENCES antenna(id),
    antenna_height_m    REAL,
    main_bearing_deg    REAL,
    longitude           REAL,
    latitude            REAL,
    address             TEXT,
    comments            TEXT
);

CREATE TABLE IF NOT EXISTS owner (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    registration_data   TEXT,
    registration_flag   TEXT,
    email               TEXT,
    internet            TEXT,
    phones              TEXT,
    fax                TEXT,
    legal_address       TEXT,
    postal_address      TEXT,
    inn                 TEXT,
    bank_details        TEXT,
    comments            TEXT
);

CREATE TABLE IF NOT EXISTS contact (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            INTEGER REFERENCES owner(id),
    name                TEXT NOT NULL,
    position            TEXT,
    phones              TEXT,
    fax                TEXT,
    address             TEXT,
    comments            TEXT
);

CREATE TABLE IF NOT EXISTS band_type (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    tx_start_hz         REAL,
    tx_stop_hz          REAL,
    rx_start_hz         REAL,
    rx_stop_hz          REAL,
    transmission_class  TEXT,
    grid_step_hz        REAL,
    comments            TEXT
);

CREATE TABLE IF NOT EXISTS license (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pages               BLOB,
    page_count          INTEGER DEFAULT 0,
    comments            TEXT
);

CREATE TABLE IF NOT EXISTS antenna (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    pattern_h           TEXT,
    pattern_v           TEXT,
    side_lobe_db        REAL,
    polarization        TEXT,
    gain_db             REAL,
    input_impedance     REAL
);

CREATE TABLE IF NOT EXISTS signal_group (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    description         TEXT
);

CREATE INDEX IF NOT EXISTS idx_transmitter_tx ON transmitter(tx_frequency_hz);
"""


@dataclass
class Assignment:
    """Запись о зарегистрированном передатчике."""

    id: Optional[int]
    name: str
    tx_frequency_hz: float
    bandwidth_hz: float = 0.0
    rx_frequency_hz: Optional[float] = None
    owner_id: Optional[int] = None
    owner_name: str = ""
    band_type_id: Optional[int] = None
    band_type_name: str = ""
    emission_class: str = ""
    max_power_w: Optional[float] = None
    license_id: Optional[int] = None
    antenna_id: Optional[int] = None
    antenna_height_m: Optional[float] = None
    main_bearing_deg: Optional[float] = None
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    address: str = ""
    comments: str = ""

    @property
    def frequency_hz(self) -> float:
        return self.tx_frequency_hz


class AssignmentDatabase:
    """База данных частотных присвоений."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path) if self.path else ":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "AssignmentDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- владельцы ---------------------------------------------------------

    def add_owner(self, name: str, **fields) -> int:
        allowed = {
            "registration_data", "registration_flag", "email", "internet", "phones",
            "fax", "legal_address", "postal_address", "inn", "bank_details", "comments",
        }
        cols = {"name": name}
        cols.update({k: v for k, v in fields.items() if k in allowed})
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO owner({keys}) VALUES({marks})", tuple(cols.values())
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_contact(self, owner_id: int, name: str, **fields) -> int:
        allowed = {"position", "phones", "fax", "address", "comments"}
        cols = {"owner_id": owner_id, "name": name}
        cols.update({k: v for k, v in fields.items() if k in allowed})
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO contact({keys}) VALUES({marks})", tuple(cols.values())
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # -- диапазоны и антенны ----------------------------------------------

    def add_band_type(self, name: str, **fields) -> int:
        allowed = {
            "tx_start_hz", "tx_stop_hz", "rx_start_hz", "rx_stop_hz",
            "transmission_class", "grid_step_hz", "comments",
        }
        cols = {"name": name}
        cols.update({k: v for k, v in fields.items() if k in allowed})
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO band_type({keys}) VALUES({marks})", tuple(cols.values())
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_antenna(self, name: str, **fields) -> int:
        allowed = {
            "pattern_h", "pattern_v", "side_lobe_db", "polarization",
            "gain_db", "input_impedance",
        }
        cols = {"name": name}
        cols.update({k: v for k, v in fields.items() if k in allowed})
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO antenna({keys}) VALUES({marks})", tuple(cols.values())
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_license(self, page_count: int = 0, pages: Optional[bytes] = None, comments: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO license(pages, page_count, comments) VALUES(?,?,?)",
            (pages, page_count, comments),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_signal_group(self, name: str, description: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO signal_group(name, description) VALUES(?,?)", (name, description)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # -- передатчики -------------------------------------------------------

    def add_transmitter(self, assignment: Assignment) -> int:
        cur = self._conn.execute(
            "INSERT INTO transmitter(name, owner_id, band_type_id, tx_frequency_hz, "
            "rx_frequency_hz, bandwidth_hz, emission_class, max_power_w, license_id, "
            "antenna_id, antenna_height_m, main_bearing_deg, longitude, latitude, "
            "address, comments) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assignment.name, assignment.owner_id, assignment.band_type_id,
                assignment.tx_frequency_hz, assignment.rx_frequency_hz,
                assignment.bandwidth_hz, assignment.emission_class,
                assignment.max_power_w, assignment.license_id, assignment.antenna_id,
                assignment.antenna_height_m, assignment.main_bearing_deg,
                assignment.longitude, assignment.latitude, assignment.address,
                assignment.comments,
            ),
        )
        self._conn.commit()
        assignment.id = int(cur.lastrowid)
        return assignment.id

    def update_transmitter(self, assignment: Assignment) -> None:
        self._conn.execute(
            "UPDATE transmitter SET name=?, owner_id=?, band_type_id=?, tx_frequency_hz=?, "
            "rx_frequency_hz=?, bandwidth_hz=?, emission_class=?, max_power_w=?, "
            "license_id=?, antenna_id=?, antenna_height_m=?, main_bearing_deg=?, "
            "longitude=?, latitude=?, address=?, comments=? WHERE id=?",
            (
                assignment.name, assignment.owner_id, assignment.band_type_id,
                assignment.tx_frequency_hz, assignment.rx_frequency_hz,
                assignment.bandwidth_hz, assignment.emission_class,
                assignment.max_power_w, assignment.license_id, assignment.antenna_id,
                assignment.antenna_height_m, assignment.main_bearing_deg,
                assignment.longitude, assignment.latitude, assignment.address,
                assignment.comments, assignment.id,
            ),
        )
        self._conn.commit()

    def delete_transmitter(self, assignment_id: int) -> None:
        self._conn.execute("DELETE FROM transmitter WHERE id=?", (assignment_id,))
        self._conn.commit()

    def assignments(self, band: Optional[tuple[float, float]] = None) -> list[Assignment]:
        """Все записи базы, при необходимости ограниченные диапазоном частот."""
        query = (
            "SELECT t.*, o.name AS owner_name, b.name AS band_type_name "
            "FROM transmitter t "
            "LEFT JOIN owner o ON o.id = t.owner_id "
            "LEFT JOIN band_type b ON b.id = t.band_type_id"
        )
        params: tuple = ()
        if band is not None:
            query += " WHERE t.tx_frequency_hz BETWEEN ? AND ?"
            params = (band[0], band[1])
        query += " ORDER BY t.tx_frequency_hz"
        return [self._row_to_assignment(row) for row in self._conn.execute(query, params)]

    def get_assignment(self, assignment_id: int) -> Optional[Assignment]:
        row = self._conn.execute(
            "SELECT t.*, o.name AS owner_name, b.name AS band_type_name "
            "FROM transmitter t "
            "LEFT JOIN owner o ON o.id = t.owner_id "
            "LEFT JOIN band_type b ON b.id = t.band_type_id WHERE t.id=?",
            (assignment_id,),
        ).fetchone()
        return self._row_to_assignment(row) if row else None

    @staticmethod
    def _row_to_assignment(row: sqlite3.Row) -> Assignment:
        return Assignment(
            id=int(row["id"]),
            name=row["name"],
            tx_frequency_hz=float(row["tx_frequency_hz"] or 0.0),
            bandwidth_hz=float(row["bandwidth_hz"] or 0.0),
            rx_frequency_hz=row["rx_frequency_hz"],
            owner_id=row["owner_id"],
            owner_name=row["owner_name"] or "",
            band_type_id=row["band_type_id"],
            band_type_name=row["band_type_name"] or "",
            emission_class=row["emission_class"] or "",
            max_power_w=row["max_power_w"],
            license_id=row["license_id"],
            antenna_id=row["antenna_id"],
            antenna_height_m=row["antenna_height_m"],
            main_bearing_deg=row["main_bearing_deg"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            address=row["address"] or "",
            comments=row["comments"] or "",
        )

    # -- импорт и экспорт --------------------------------------------------

    def import_csv(self, path: str | Path) -> int:
        """Загрузка перечня передатчиков из CSV.

        Ожидаются колонки ``name``, ``frequency_hz`` (или ``tx_frequency_hz``),
        ``bandwidth_hz``; необязательные — ``owner``, ``latitude``,
        ``longitude``, ``max_power_w``, ``band_type``.
        """
        count = 0
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                freq = row.get("frequency_hz") or row.get("tx_frequency_hz")
                if not freq:
                    continue
                owner_id = None
                owner_name = (row.get("owner") or "").strip()
                if owner_name:
                    owner_id = self._owner_by_name(owner_name)
                    if owner_id is None:
                        owner_id = self.add_owner(owner_name)
                band_type_id = None
                band_name = (row.get("band_type") or "").strip()
                if band_name:
                    band_type_id = self._band_by_name(band_name)
                    if band_type_id is None:
                        band_type_id = self.add_band_type(band_name)
                self.add_transmitter(
                    Assignment(
                        id=None,
                        name=row.get("name") or f"Приёмник {freq}",
                        tx_frequency_hz=float(freq),
                        bandwidth_hz=float(row.get("bandwidth_hz") or 0.0),
                        owner_id=owner_id,
                        band_type_id=band_type_id,
                        max_power_w=float(row["max_power_w"]) if row.get("max_power_w") else None,
                        latitude=float(row["latitude"]) if row.get("latitude") else None,
                        longitude=float(row["longitude"]) if row.get("longitude") else None,
                    )
                )
                count += 1
        return count

    def export_csv(self, path: str | Path) -> Path:
        """Выгрузка перечня передатчиков в CSV."""
        path = Path(path)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "id", "name", "tx_frequency_hz", "bandwidth_hz", "owner",
                    "band_type", "emission_class", "max_power_w", "latitude",
                    "longitude", "address", "comments",
                ]
            )
            for item in self.assignments():
                writer.writerow(
                    [
                        item.id, item.name, item.tx_frequency_hz, item.bandwidth_hz,
                        item.owner_name, item.band_type_name, item.emission_class,
                        item.max_power_w, item.latitude, item.longitude,
                        item.address, item.comments,
                    ]
                )
        return path

    def _owner_by_name(self, name: str) -> Optional[int]:
        row = self._conn.execute("SELECT id FROM owner WHERE name=?", (name,)).fetchone()
        return int(row["id"]) if row else None

    def _band_by_name(self, name: str) -> Optional[int]:
        row = self._conn.execute("SELECT id FROM band_type WHERE name=?", (name,)).fetchone()
        return int(row["id"]) if row else None
