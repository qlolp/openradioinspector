"""Тесты инструментов разбора коммерческого дистрибутива.

Проверки не требуют наличия дистрибутива: часть тестов работает на
синтетических данных, а часть пропускается, если коммерческий диск
отсутствует в рабочей копии.

Запуск: ``python3 -m unittest tests.test_analysis_tools -v``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools" / "analysis"))

from firebird_meta import classify, identifiers  # noqa: E402
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
    """Каталог с дистрибутивом, если он присутствует в рабочей копии."""
    for candidate in REPO_ROOT.glob("CD Ligtht 32314"):
        if candidate.is_dir():
            return candidate
    return None


@unittest.skipUnless(locate_distribution(), "коммерческий диск отсутствует в рабочей копии")
class TestRealDistribution(unittest.TestCase):
    """Проверки на реальных файлах дистрибутива."""

    def setUp(self) -> None:
        self.root = locate_distribution()
        self.exe = next(self.root.rglob("InspectorRT.exe"), None)
        if self.exe is None:
            self.skipTest("InspectorRT.exe не найден")

    def test_forms_are_parsed(self):
        pe = PEFile(self.exe)
        forms = find_forms(pe)
        self.assertGreater(len(forms), 100)
        names = {name for name, _ in forms}
        self.assertIn("TFINSPECTORRCMF", names)
        self.assertIn("TFRSIGNALSLIST", names)

    def test_database_identifiers_include_taskplan(self):
        db = next(self.root.rglob("TASKPLAN.ITASKDB"), None)
        if db is None:
            self.skipTest("TASKPLAN.ITASKDB не найден")
        names = identifiers(db)
        self.assertIn("TASKPLAN", names)
        self.assertIn("GEN_TASKTYPEID", names)


if __name__ == "__main__":
    unittest.main()
