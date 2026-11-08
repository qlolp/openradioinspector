"""Чтение установочного пакета MSI: строковый пул и состав потоков.

Имена таблиц MSI хранятся в compound file с искажёнными кодами символов,
поэтому инструмент не восстанавливает таблицы: он извлекает строковый пул
пакета и перечень потоков. Этого достаточно, чтобы получить перечень
компонентов установки, описаний и ключевых свойств.

Примеры:

    python3 tools/analysis/msi_tables.py --msi SetupRadioInspectorLight.msi --strings
    python3 tools/analysis/msi_tables.py --msi SetupRadioInspectorLight.msi --streams
"""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ENDOFCHAIN = 0xFFFFFFFE
NOSTREAM = 0xFFFFFFFF


class CompoundFile:
    """Минимальный читатель compound file (OLE2)."""

    def __init__(self, path: str | Path) -> None:
        self.data = Path(path).read_bytes()
        if self.data[:8] != SIGNATURE:
            raise ValueError("файл не является compound file")
        self.sector_size = 1 << struct.unpack_from("<H", self.data, 30)[0]
        self.mini_size = 1 << struct.unpack_from("<H", self.data, 32)[0]
        num_fat = struct.unpack_from("<I", self.data, 44)[0]
        self.dir_start = struct.unpack_from("<I", self.data, 48)[0]
        self.cutoff = struct.unpack_from("<I", self.data, 56)[0]
        minifat_start = struct.unpack_from("<I", self.data, 60)[0]
        difat = list(struct.unpack_from("<109I", self.data, 76))
        self.fat: list[int] = []
        for sect in difat[:num_fat]:
            self.fat.extend(struct.unpack_from(f"<{self.sector_size // 4}I", self.sect(sect), 0))
        self.minifat: list[int] = []
        for sect in self.chain(minifat_start):
            self.minifat.extend(struct.unpack_from(f"<{self.sector_size // 4}I", self.sect(sect), 0))
        self.entries = self.read_dir()
        root = next((e for e in self.entries if e["type"] == 5), None)
        self.ministream = b"".join(self.sect(s) for s in self.chain(root["start"])) if root else b""

    def sect(self, index: int) -> bytes:
        off = self.sector_size * (index + 1)
        return self.data[off:off + self.sector_size]

    def chain(self, start: int) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        sect = start
        while sect not in (ENDOFCHAIN, NOSTREAM) and sect < len(self.fat) and sect not in seen:
            seen.add(sect)
            out.append(sect)
            sect = self.fat[sect]
        return out

    def read_dir(self) -> list[dict]:
        out = []
        buf = b"".join(self.sect(s) for s in self.chain(self.dir_start))
        for off in range(0, len(buf), 128):
            raw = buf[off:off + 128]
            if len(raw) < 128:
                break
            name_len = struct.unpack_from("<H", raw, 64)[0]
            name = raw[:max(0, name_len - 2)].decode("utf-16-le", "ignore")
            out.append({
                "name": name,
                "type": raw[66],
                "start": struct.unpack_from("<I", raw, 116)[0],
                "size": struct.unpack_from("<Q", raw, 120)[0],
            })
        return out

    def stream_names(self) -> list[str]:
        """Имена потоков в исходном (искажённом) виде."""
        return [e["name"] for e in self.entries if e["type"] == 2]

    def read(self, name: str) -> bytes:
        """Содержимое потока по искажённому имени."""
        for entry in self.entries:
            if entry["type"] == 2 and entry["name"] == name:
                size, start = entry["size"], entry["start"]
                if size < self.cutoff:
                    out = bytearray()
                    sect = start
                    seen: set[int] = set()
                    while sect not in (ENDOFCHAIN, NOSTREAM) and sect not in seen:
                        seen.add(sect)
                        off = sect * self.mini_size
                        out += self.ministream[off:off + self.mini_size]
                        sect = self.minifat[sect] if sect < len(self.minifat) else ENDOFCHAIN
                    return bytes(out[:size])
                out = bytearray()
                for sect in self.chain(start):
                    out += self.sect(sect)
                return bytes(out[:size])
        raise KeyError(name)

    def text_streams(self) -> list[tuple[str, bytes]]:
        """Потоки с содержимым (без служебных каталогов)."""
        return [(e["name"], self.read_entry(e)) for e in self.entries if e["type"] == 2]

    def read_entry(self, entry: dict) -> bytes:
        """Содержимое потока по записи каталога."""
        size, start = entry["size"], entry["start"]
        if size < self.cutoff:
            out = bytearray()
            sect = start
            seen: set[int] = set()
            while sect not in (ENDOFCHAIN, NOSTREAM) and sect not in seen:
                seen.add(sect)
                off = sect * self.mini_size
                out += self.ministream[off:off + self.mini_size]
                sect = self.minifat[sect] if sect < len(self.minifat) else ENDOFCHAIN
            return bytes(out[:size])
        out = bytearray()
        for sect in self.chain(start):
            out += self.sect(sect)
        return bytes(out[:size])

    @staticmethod
    def _printable_ratio(blob: bytes) -> float:
        """Доля читаемых байт в потоке."""
        if not blob:
            return 0.0
        readable = sum(1 for b in blob if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
        return readable / len(blob)

    def string_pool(self) -> list[str]:
        """Строки установочного пакета.

        Имена потоков compound file искажены, поэтому пул и данные строк
        опознаются по содержимому: пул — поток с размером, кратным четырём,
        записи которого указывают внутрь самого большого потока. Если такой
        пул не найден, строки извлекаются как читаемые фрагменты.
        """
        streams = self.text_streams()
        if not streams:
            return []
        # Пул строк и данные связаны: сумма длин записей пула равна размеру
        # потока данных. Перебор пар по этому признаку находит настоящий
        # пул даже при искажённых именах потоков.
        for pool_name, pool_blob in streams:
            if not pool_blob or len(pool_blob) % 4:
                continue
            values = struct.unpack(f"<{len(pool_blob) // 4}I", pool_blob)
            lengths = [value & 0xFFFF for value in values]
            total = sum(lengths)
            if not values or not total:
                continue
            for data_name, data in streams:
                if data_name == pool_name or total != len(data):
                    continue
                out: list[str] = []
                offset = 0
                for length in lengths:
                    out.append(data[offset:offset + length].decode("utf-8", "replace"))
                    offset += length
                readable = sum(1 for value in out if value.strip())
                if readable >= len(out) * 0.5:
                    return out
        _, largest = max(streams, key=lambda item: len(item[1]))
        return readable_strings(largest)


def readable_strings(data: bytes, min_len: int = 4) -> list[str]:
    """Читаемые фрагменты потока: ASCII и UTF-16LE."""
    out = [m.group().decode("ascii", "ignore") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data):
        out.append(match.group().decode("utf-16-le", "replace"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Строки и потоки установочного пакета MSI")
    parser.add_argument("--msi", required=True, help="путь к файлу MSI")
    parser.add_argument("--streams", action="store_true", help="перечень потоков")
    parser.add_argument("--strings", action="store_true", help="строковый пул")
    parser.add_argument("--grep", help="фильтр по подстроке")
    parser.add_argument("--limit", type=int, default=60, help="сколько строк показать")
    args = parser.parse_args()

    cfb = CompoundFile(args.msi)
    if args.streams:
        for name, blob in cfb.text_streams():
            print(f"{len(blob):>9}  {name!r}")
        return
    strings = cfb.string_pool()
    if args.grep:
        needle = args.grep.lower()
        strings = [s for s in strings if needle in s.lower()]
    for value in strings[: args.limit]:
        if value.strip():
            print(repr(value))
    print(f"# строк в пуле: {len(strings)}")


if __name__ == "__main__":
    main()
