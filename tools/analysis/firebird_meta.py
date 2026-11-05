"""Лексическая выборка идентификаторов из баз FireBird.

Инструмент не разбирает формат ODS: он извлекает из файла базы имена таблиц,
колонок, генераторов и первичных ключей, которые FireBird хранит в служебных
страницах. Этого достаточно, чтобы получить перечень сущностей и проверить
их наличие, но не состав и типы колонок — для этого нужен сервер FireBird.

Примеры:

    python3 tools/analysis/firebird_meta.py --db "TASKPLAN.ITASKDB"
    python3 tools/analysis/firebird_meta.py --db "1 RECORD.IFRQDB" --prefix FREQ
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

IDENT_RE = re.compile(rb"[A-Za-z][A-Za-z0-9_$]{2,63}")

#: Служебные имена, которые не относятся к прикладной модели.
NOISE_PREFIXES = ("RDB$", "BI_GEN_", "GEN_", "PK_", "PROCEDURE", "PROCEDURES")


def identifiers(path: str | Path) -> list[str]:
    """Все уникальные идентификаторы, встречающиеся в файле."""
    data = Path(path).read_bytes()
    out = set()
    for match in IDENT_RE.finditer(data):
        out.add(match.group().decode("ascii", "ignore"))
    return sorted(out)


def classify(names: list[str]) -> dict[str, list[str]]:
    """Разделение имён по ролям: генераторы, ключи, объекты бизнес-логики."""
    groups = {"генераторы": [], "первичные ключи": [], "бизнес-логика": [], "прочее": []}
    for name in names:
        if name.startswith("RDB$") or name.startswith("NOISE"):
            continue
        if name.startswith("BI_"):
            groups["бизнес-логика"].append(name)
        elif name.startswith("GEN_"):
            groups["генераторы"].append(name)
        elif name.startswith("PK_"):
            groups["первичные ключи"].append(name)
        elif not name.startswith("PROCEDURE"):
            groups["прочее"].append(name)
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Идентификаторы базы FireBird")
    parser.add_argument("--db", required=True, help="путь к файлу базы")
    parser.add_argument("--prefix", help="показать только имена с префиксом")
    parser.add_argument("--all", action="store_true", help="показать все идентификаторы")
    args = parser.parse_args()

    names = identifiers(args.db)
    if args.prefix:
        names = [n for n in names if n.upper().startswith(args.prefix.upper())]
        for name in names:
            print(name)
        print(f"# найдено: {len(names)}")
        return

    groups = classify(names)
    for role, values in groups.items():
        print(f"\n=== {role} ({len(values)}) ===")
        for name in values:
            print(f"   {name}")

    if args.all:
        print("\n=== все идентификаторы ===")
        for name in names:
            print(f"   {name}")


if __name__ == "__main__":
    main()
