"""Тесты инструментов разбора коммерческого дистрибутива.

Проверки не требуют наличия дистрибутива: часть тестов работает на
синтетических данных, а необязательные исследования вынесены в tests.research_checks.

Запуск: ``python3 -m unittest tests.test_analysis_tools -v``
"""

from __future__ import annotations

import os

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools" / "analysis"))

from firebird_meta import classify, identifiers  # noqa: E402
from hlp_reader import HlpError, decompress_lz77, read_hlp  # noqa: E402
from msi_tables import CompoundFile  # noqa: E402
from pe_resources import PEFile, find_forms, parse_dfm  # noqa: E402
from strings_extract import ascii_strings, utf16_strings  # noqa: E402


def short_string(value: str) -> bytes:
    """Строка DFM с однобайтовой длиной."""
    raw = value.encode("cp1251")
    return bytes([len(raw)]) + raw


def build_dfm() -> bytes:
    """Синтетическая форма Delphi: форма с одним потомком."""
    parts = [b"TPF0"]
    parts.append(short_string("TForm1"))
    parts.append(short_string("Form1"))
    parts.append(short_string("Caption"))
    parts.append(b"\x06" + short_string("Hello"))
    parts.append(b"\x00")            # конец свойств формы
    parts.append(b"\x01")            # начало списка потомков
    parts.append(short_string("TLabel"))
    parts.append(short_string("Label1"))
    parts.append(short_string("Caption"))
    parts.append(b"\x06" + short_string("Подпись"))
    parts.append(b"\x00")            # конец свойств потомка
    parts.append(b"\x00")            # конец списка потомков
    parts.append(b"\x00")            # конец формы
    return b"".join(parts)


class TestDfmParser(unittest.TestCase):
    def test_components_and_properties_are_extracted(self):
        components = parse_dfm(build_dfm())
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0].cls, "TForm1")
        self.assertEqual(components[0].name, "Form1")
        self.assertIn(("Caption", "Hello"), components[0].props)
        self.assertEqual(components[1].cls, "TLabel")
        self.assertEqual(components[1].name, "Label1")
        self.assertIn(("Caption", "Подпись"), components[1].props)

    def test_non_dfm_blob_yields_nothing(self):
        self.assertEqual(parse_dfm(b"NOTDFM"), [])


class TestStringExtraction(unittest.TestCase):
    def test_ascii_and_utf16_are_separated(self):
        data = b"Guardant API\x00\x00" + "Линия порога".encode("utf-16-le")
        ascii_found = ascii_strings(data)
        utf16_found = utf16_strings(data)
        self.assertIn("Guardant API", ascii_found)
        self.assertIn("Линия порога", utf16_found)


class TestFirebirdIdentifiers(unittest.TestCase):
    def test_identifiers_are_collected_and_classified(self):
        data = b"RDB$RELATION PK_TASKPLAN GEN_TASKID BI_GEN_TASKID TASKNAMEID"
        names = identifiers_from_bytes(data)
        groups = classify(names)
        self.assertIn("PK_TASKPLAN", groups["первичные ключи"])
        self.assertIn("GEN_TASKID", groups["генераторы"])
        self.assertIn("BI_GEN_TASKID", groups["бизнес-логика"])
        self.assertIn("TASKNAMEID", groups["прочее"])
        self.assertNotIn("RDB$RELATION", groups["прочее"])


def identifiers_from_bytes(data: bytes) -> list[str]:
    """Идентификаторы из произвольных байтов — тот же разбор, что и в модуле."""
    import re

    pattern = re.compile(rb"[A-Za-z][A-Za-z0-9_$]{2,63}")
    return sorted({m.group().decode("ascii", "ignore") for m in pattern.finditer(data)})


def locate_distribution() -> Path | None:
    """Optional licensed material must be explicitly supplied outside the checkout."""
    value = os.environ.get("ORI_RESEARCH_DISTRIBUTION")
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or path == REPO_ROOT or REPO_ROOT in path.parents:
        raise ValueError("ORI_RESEARCH_DISTRIBUTION must be an existing directory outside the checkout")
    return path





class TestHlpDecompression(unittest.TestCase):
    """Распаковка HLP-LZ77."""

    def test_literals_are_emitted_as_is(self):
        # Управляющий байт 0x00: все восемь бит — литералы.
        compressed = bytes([0x00]) + b"ABCDEFGH"
        self.assertEqual(decompress_lz77(compressed, 64), b"ABCDEFGH")

    def test_match_copies_from_ring_buffer(self):
        # 0b00000010: бит 0 — литерал 'A', бит 1 — ссылка.
        # Ссылка: pos = 0, len = 3 → копируются три байта назад от текущей позиции.
        compressed = bytes([0b00000010, 0x41, 0x00, 0x00])
        self.assertEqual(decompress_lz77(compressed, 64), b"AAAA")

    def test_limit_is_respected(self):
        compressed = bytes([0b11111111, 0x41, 0x42, 0x00, 0x00])
        self.assertLessEqual(len(decompress_lz77(compressed, 3)), 3)

    def test_non_hlp_file_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.hlp"
            path.write_bytes(b"NOTAHLP" + b"\x00" * 64)
            hlp = read_hlp(path)
        self.assertEqual(hlp.topics, [])
        self.assertTrue(hlp.errors)




if __name__ == "__main__":
    unittest.main()
