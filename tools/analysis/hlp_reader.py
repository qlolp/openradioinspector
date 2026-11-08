"""Чтение текстов из файлов справки WinHelp (`.hlp`).

Разбор построен по описанию формата, используемому программой helpdeco
(документация X-Ways «Windows Help File Format»): заголовок файла, внутренний
каталог в виде B+-дерева, внутренний файл `|SYSTEM` с параметрами сжатия и
кодировки, внутренний файл `|TOPIC` с блоками тем и внутренний файл
`|Phrases` с таблицей фраз.

Поддерживается сжатие блоков тем (HLP-LZ77) и сжатие текста тем ссылками на
таблицу фраз.

Зависимостей нет: только стандартная библиотека.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

HLP_MAGIC = 0x00035F3F
SYSTEM_MAGIC = 0x036C
BTREE_MAGIC = 0x293B

#: Типы записей тем (RecordType в TOPICLINK).
RECORD_TOPIC_HEADER = 0x02
RECORD_TOPIC_TEXT = 0x20
RECORD_TOPIC_TABLE = 0x23

#: Кодировки, определяемые записью CHARSET внутреннего файла |SYSTEM.
CHARSETS = {
    1251: "cp1251",
    0x19: "cp1251",
    218: "koi8-r",
    0x1F: "koi8-r",
}


class HlpError(RuntimeError):
    """Файл не разобран."""


def decompress_lz77(data: bytes, max_size: int) -> bytes:
    """Распаковка по алгоритму HLP-LZ77.

    Кольцевой буфер на 4096 байт, изначально заполненный пробелами. Каждый
    бит управляющего байта задаёт либо литерал, либо копию из буфера.
    """
    ring = bytearray(b" " * 4096)
    out = bytearray()
    pos = 0
    current = 0
    size = len(data)
    while pos < size and len(out) < max_size:
        control = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= max_size:
                break
            if not (control >> bit) & 1:
                if pos >= size:
                    return bytes(out)
                value = data[pos]
                pos += 1
                out.append(value)
                ring[current] = value
                current = (current + 1) & 0xFFF
            else:
                if pos + 1 >= size:
                    return bytes(out)
                m0 = data[pos]
                m1 = data[pos + 1]
                pos += 2
                length = (m1 >> 4) + 3
                rel = ((m1 & 0x0F) << 8) | m0
                start = (current - 1 - rel) & 0xFFF
                for step in range(length):
                    if len(out) >= max_size:
                        break
                    value = ring[(start + step) & 0xFFF]
                    out.append(value)
                    ring[current] = value
                    current = (current + 1) & 0xFFF
    return bytes(out)


@dataclass
class Topic:
    """Тема справки."""

    title: str
    text: str
    block: int = 0
    macros: str = ""

    @property
    def lines(self) -> list[str]:
        return [line.strip() for line in self.text.splitlines() if line.strip()]


@dataclass
class HlpFile:
    """Разобранный файл справки."""

    path: Path
    title: str = ""
    minor: int = 0
    major: int = 0
    flags: int = 0
    charset: str = "cp1251"
    compressed: bool = False
    topic_block_size: int = 4096
    topics: list[Topic] = field(default_factory=list)
    internal_files: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def topic_count(self) -> int:
        return len(self.topics)

    @property
    def text_size(self) -> int:
        return sum(len(t.text) for t in self.topics)


def _read_directory(data: bytes) -> dict[str, int]:
    """Внутренний каталог: имя внутреннего файла → смещение."""
    magic, dir_start, _free, _size = struct.unpack_from("<4I", data, 0)
    if magic != HLP_MAGIC:
        raise HlpError("не сигнатура WinHelp")
    reserved, used, _flags = struct.unpack_from("<IIB", data, dir_start)
    tree = data[dir_start + 9: dir_start + 9 + used]

    bt_magic, _bt_flags, page_size = struct.unpack_from("<3H", tree, 0)
    if bt_magic != BTREE_MAGIC:
        raise HlpError("не B+-дерево каталога")
    root_page, = struct.unpack_from("<h", tree, 26)
    total_pages, levels = struct.unpack_from("<2h", tree, 30)

    pages = [
        tree[38 + i * page_size: 38 + (i + 1) * page_size]
        for i in range(total_pages)
    ]

    def read_index(page: bytes) -> list[tuple[str, int]]:
        n_entries, = struct.unpack_from("<h", page, 2)
        entries = []
        off = 6
        for _ in range(n_entries):
            end = page.index(b"\0", off)
            name = page[off:end].decode("cp1251", "replace")
            off = end + 1
            page_number, = struct.unpack_from("<h", page, off)
            off += 2
            entries.append((name, page_number))
        return entries

    def read_leaf(page: bytes) -> list[tuple[str, int]]:
        n_entries, = struct.unpack_from("<h", page, 2)
        entries = []
        off = 6
        for _ in range(n_entries):
            end = page.index(b"\0", off)
            name = page[off:end].decode("cp1251", "replace")
            off = end + 1
            file_offset, = struct.unpack_from("<i", page, off)
            off += 4
            entries.append((name, file_offset))
        return entries

    page_index = root_page
    for _ in range(max(0, levels - 1)):
        entries = read_index(pages[page_index])
        page_index = entries[0][1] if entries else page_index

    result: dict[str, int] = {}
    visited: set[int] = set()
    while 0 <= page_index < len(pages) and page_index not in visited:
        visited.add(page_index)
        page = pages[page_index]
        for name, offset in read_leaf(page):
            result[name] = offset
        next_page, = struct.unpack_from("<h", page, 6)
        if next_page < 0:
            break
        page_index = next_page
    return result


def _internal_file(data: bytes, offset: int) -> bytes:
    """Содержимое внутреннего файла по смещению его FILEHEADER."""
    reserved, used, _flags = struct.unpack_from("<IIB", data, offset)
    return data[offset + 9: offset + 9 + used]


def _parse_system(system: bytes) -> dict:
    """Параметры сжатия, кодировки и заголовок из |SYSTEM."""
    magic, minor, major, _gen, flags = struct.unpack_from("<HHHih", system, 0)
    if magic != SYSTEM_MAGIC:
        raise HlpError(f"не SYSTEM: magic {magic:#x}")
    info: dict = {"minor": minor, "major": major, "flags": flags, "title": "", "charset": None}
    if minor > 16:
        compressed = flags in (4, 8)
        info["compressed"] = compressed
        info["topic_block_size"] = 4096 if flags == 4 else (2048 if flags == 8 else 4096)
        off = 10
        while off + 4 <= len(system):
            rec_type, rec_size = struct.unpack_from("<HH", system, off)
            off += 4
            payload = system[off:off + rec_size]
            off += rec_size
            if rec_type == 1:
                info["title"] = payload.split(b"\0")[0].decode("cp1251", "replace")
            elif rec_type == 3:
                info["contents_offset"] = struct.unpack_from("<i", payload, 0)[0]
            elif rec_type == 4:
                info["macro"] = payload.split(b"\0")[0].decode("cp1251", "replace")
            elif rec_type == 11 and rec_size >= 2:
                charset, = struct.unpack_from("<H", payload, 0)
                info["charset"] = charset
            if rec_type == 0:
                break
    else:
        info["compressed"] = False
        info["topic_block_size"] = 2048
        info["title"] = system[10:].split(b"\0")[0].decode("cp1251", "replace")
    return info


def _parse_phrases(data: bytes) -> list[bytes]:
    """Таблица фраз из внутреннего файла |Phrases."""
    if len(data) < 8:
        return []
    num_phrases, one_hundred = struct.unpack_from("<2H", data, 0)
    decompressed_size, = struct.unpack_from("<i", data, 4)
    if one_hundred != 0x0100 or num_phrases == 0:
        return []
    offsets = []
    base = 8
    for i in range(num_phrases + 1):
        value, = struct.unpack_from("<H", data, base + 2 * i)
        offsets.append(value)
    payload = data[base + 2 * (num_phrases + 1):]
    array = decompress_lz77(payload, decompressed_size)
    # Смещения фраз отсчитываются от конца области заголовка и таблицы
    # смещений: первая фраза начинается с нуля распакованного массива.
    origin = offsets[0]
    phrases = []
    for i in range(num_phrases):
        start = offsets[i] - origin
        end = offsets[i + 1] - origin
        if start < 0 or end > len(array) or end < start:
            phrases.append(b"")
            continue
        phrases.append(array[start:end].split(b"\0")[0])
    return phrases


def _decode_link_data2(raw: bytes, phrases: list[bytes]) -> bytes:
    """Распаковка текста темы со ссылками на таблицу фраз."""
    if not phrases:
        return raw
    out = bytearray()
    pos = 0
    size = len(raw)
    while pos < size:
        value = raw[pos]
        pos += 1
        if value == 0 or value >= 15:
            out.append(value)
            continue
        if pos >= size:
            break
        combined = value * 256 - 256 + raw[pos]
        pos += 1
        number = combined // 2
        if 0 <= number < len(phrases):
            out += phrases[number]
        if combined % 2:
            out.append(0x20)
    return bytes(out)


def _iter_topic_links(buffer: bytes, first_link: int) -> Iterator[tuple[int, int, int, bytes, bytes]]:
    """Обход связей тем внутри распакованного буфера блока."""
    offset = first_link
    guard = 0
    while 0 <= offset < len(buffer) and guard < 50000:
        guard += 1
        if offset + 21 > len(buffer):
            break
        block_size, data_len2, _prev, _next, data_len1 = struct.unpack_from(
            "<5I", buffer, offset
        )
        record_type = buffer[offset + 20]
        if block_size <= 21 or offset + block_size > len(buffer):
            break
        data2_len = max(0, min(data_len2, block_size - data_len1))
        data1 = buffer[offset + 21: offset + 21 + max(0, data_len1 - 11)]
        data2 = buffer[offset + data_len1: offset + data_len1 + data2_len]
        yield offset, record_type, data_len1, data1, data2
        offset += block_size


def read_hlp(path: str | Path) -> HlpFile:
    """Читает файл справки и извлекает тексты тем."""
    path = Path(path)
    data = path.read_bytes()
    result = HlpFile(path=path)
    try:
        result.internal_files = _read_directory(data)
    except HlpError as exc:
        result.errors.append(f"каталог: {exc}")
        return result

    def internal(name_suffix: str) -> bytes | None:
        for name, offset in result.internal_files.items():
            if name.upper().endswith(name_suffix):
                return _internal_file(data, offset)
        return None

    system_data = internal("SYSTEM")
    if system_data is None:
        result.errors.append("внутренний файл |SYSTEM не найден")
        return result
    try:
        info = _parse_system(system_data)
    except HlpError as exc:
        result.errors.append(f"system: {exc}")
        return result

    result.title = info.get("title", "")
    result.minor = info["minor"]
    result.major = info["major"]
    result.flags = info["flags"]
    result.compressed = info["compressed"]
    result.topic_block_size = info["topic_block_size"]
    charset = info.get("charset")
    if charset in CHARSETS:
        result.charset = CHARSETS[charset]

    phrases_data = internal("PHRASES")
    phrases = _parse_phrases(phrases_data) if phrases_data else []

    topic_data = internal("TOPIC")
    if topic_data is None:
        result.errors.append("внутренний файл |TOPIC не найден")
        return result

    block_size = result.topic_block_size
    decompress_size = 16384 if result.compressed else block_size - 12
    offset = 0
    number = 0
    pending_title = ""
    pending_macros = ""
    while offset + 12 <= len(topic_data):
        header = topic_data[offset:offset + 12]
        first_link, = struct.unpack_from("<I", header, 4)
        payload = topic_data[offset + 12: offset + block_size]
        if len(payload) < 12:
            break
        buffer = decompress_lz77(payload, decompress_size) if result.compressed else payload[:decompress_size]
        for link_off, record_type, _len1, _data1, data2 in _iter_topic_links(buffer, first_link - 12):
            if record_type == RECORD_TOPIC_HEADER:
                decoded = _decode_link_data2(data2, phrases)
                parts = decoded.split(b"\0")
                pending_title = parts[0].decode(result.charset, "replace").strip()
                pending_macros = (
                    parts[1].decode(result.charset, "replace").strip()
                    if len(parts) > 1
                    else ""
                )
            elif record_type in (RECORD_TOPIC_TEXT, RECORD_TOPIC_TABLE):
                raw = data2
                if len(raw) == 0:
                    raw = _data1
                text = _decode_link_data2(raw, phrases).decode(result.charset, "replace")
                text = _clean_text(text)
                if text.strip():
                    first_line = next(
                        (line.strip() for line in text.splitlines() if line.strip()), ""
                    )
                    title = pending_title if len(pending_title) > 2 else ""
                    if not title or len(title) > 120:
                        title = first_line[:120]
                    result.topics.append(
                        Topic(
                            title=title,
                            text=text,
                            block=number,
                            macros=pending_macros,
                        )
                    )
        offset += block_size
        number += 1
    return result


def _clean_text(text: str) -> str:
    """Убирает служебные символы и приводит переводы строк."""
    out = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 and ch not in ("\r", "\n", "\t"):
            continue
        if code == 0x7F:
            continue
        out.append(ch)
    return "".join(out).replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def read_all(paths: list[str | Path]) -> list[HlpFile]:
    """Читает несколько файлов справки."""
    return [read_hlp(p) for p in paths]


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Чтение тем из файла справки .hlp")
    parser.add_argument("hlp", nargs="+", help="файлы справки")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--limit", type=int, default=0, help="сколько тем показать (0 — все)")
    args = parser.parse_args()

    for path in args.hlp:
        hlp = read_hlp(path)
        if args.json:
            print(json.dumps({
                "path": str(hlp.path),
                "title": hlp.title,
                "version": f"{hlp.minor}.{hlp.major}",
                "charset": hlp.charset,
                "compressed": hlp.compressed,
                "topics": [{"title": t.title, "text": t.text} for t in hlp.topics],
            }, ensure_ascii=False, indent=1))
            continue
        print(f"=== {hlp.path.name} ===")
        print(f"  заголовок: {hlp.title}")
        print(f"  версия: {hlp.minor}.{hlp.major} | кодировка: {hlp.charset} | тем: {hlp.topic_count}")
        for topic in hlp.topics[: args.limit or None]:
            print(f"  — {topic.title}")
            for line in topic.lines[:6]:
                print(f"      {line}")
        if hlp.errors:
            print(f"  ошибки: {hlp.errors}")


if __name__ == "__main__":
    main()
