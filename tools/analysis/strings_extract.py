"""Извлечение строк из исполняемых модулей.

Строки интерфейса коммерческого комплекса хранятся в кодовой секции в двух
видах: как UTF-8 (Delphi 2009 и новее) и как UTF-16LE. Инструмент собирает
оба вида, отделяет осмысленные надписи от мусора и позволяет фильтровать их
по ключевым словам.

Примеры:

    python3 tools/analysis/strings_extract.py --exe InspectorRT.exe --count
    python3 tools/analysis/strings_extract.py --exe InspectorRT.exe --grep эталон
    python3 tools/analysis/strings_extract.py --exe InspectorRT.exe --cyrillic
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ASCII_RE = re.compile(rb"[\x20-\x7e]{4,}")
UTF8_CYRILLIC_RE = re.compile(rb"(?:[\x20-\x7e]|[\xd0-\xd1][\x80-\xbf]){6,}")
#: Символы, считаемые содержимым строки UTF-16LE: печатные ASCII,
#: кириллица, латиница-1 и типографские знаки.
UTF16_TEXT_RE = re.compile(
    "[\\u0020-\\u007e\\u00a0-\\u00ff\\u0400-\\u04ff\\u2010-\\u201f\\u2026\\u00ab\\u00bb]{4,}"
)


def ascii_strings(data: bytes, min_len: int = 4) -> list[str]:
    """ASCII-строки длиной не короче min_len."""
    return [m.group().decode("ascii", "ignore") for m in ASCII_RE.finditer(data)]


def utf16_strings(data: bytes, min_len: int = 4) -> list[str]:
    """Строки UTF-16LE с кириллицей и печатными символами.

    Данные читаются с выравниванием по обоим смещениям, поэтому часть
    найденных фрагментов — мусор от неверного выравнивания; отсев
    выполняется по доле букв в строке.
    """
    out: list[str] = []
    for shift in (0, 1):
        text = data[shift:].decode("utf-16-le", errors="ignore")
        for match in UTF16_TEXT_RE.finditer(text):
            value = match.group().strip()
            if len(value) < min_len:
                continue
            letters = sum(c.isalpha() for c in value)
            if letters >= len(value) * 0.6:
                out.append(value)
    return out


def cyrillic_strings(data: bytes, min_len: int = 6) -> list[str]:
    """Строки UTF-8 с кириллицей и разумной долей букв."""
    out = []
    for m in UTF8_CYRILLIC_RE.finditer(data):
        try:
            text = m.group().decode("utf-8")
        except UnicodeDecodeError:
            continue
        if re.search(r"[А-Яа-яЁё]", text):
            letters = sum(c.isalpha() for c in text)
            if letters >= len(text) * 0.7:
                out.append(text)
    return out


def collect(path: str | Path) -> dict[str, list[str]]:
    """Собранные строки файла по видам кодировки."""
    data = Path(path).read_bytes()
    return {
        "ascii": ascii_strings(data),
        "utf16": utf16_strings(data),
        "cyrillic": cyrillic_strings(data),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечение строк из PE-файла")
    parser.add_argument("--exe", required=True, help="путь к файлу")
    parser.add_argument("--grep", help="фильтр по подстроке")
    parser.add_argument("--cyrillic", action="store_true", help="только кириллица")
    parser.add_argument("--count", action="store_true", help="счётчики по видам")
    parser.add_argument("--limit", type=int, default=40, help="сколько строк показать")
    args = parser.parse_args()

    pool = collect(args.exe)
    if args.count:
        for kind, values in pool.items():
            print(f"{kind:<10} {len(values):>8}")
        return

    if args.cyrillic:
        candidates = pool["cyrillic"]
    else:
        candidates = pool["ascii"] + pool["utf16"] + pool["cyrillic"]

    if args.grep:
        needle = args.grep.lower()
        candidates = [s for s in candidates if needle in s.lower()]

    seen: set[str] = set()
    shown = 0
    for value in candidates:
        value = value.strip()
        if len(value) < 4 or value in seen:
            continue
        seen.add(value)
        if shown < args.limit:
            print(repr(value))
        shown += 1
    print(f"# уникальных строк: {shown}")


if __name__ == "__main__":
    main()
