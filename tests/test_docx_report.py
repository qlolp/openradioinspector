"""Проверка собственного DOCX writer протоколов."""

from __future__ import annotations

import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from radioinspector.cli import main
from radioinspector.docx_report import W_NS, write_docx_report
from radioinspector.panorama import PanoramaStore, ScanSettings, generate_synthetic_trace
from radioinspector.reporting import DEFAULT_TEMPLATE, ProtocolTemplate, ReportContext


class DocxReportTests(unittest.TestCase):
    def test_document_has_valid_package_and_report_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "protocol.docx"
            context = ReportContext().set("operator", "Иванов").set("place", "Лаборатория")
            context.set("occupied_bandwidth_method", "β/2").set("occupied_bandwidth_khz", 125)
            result = write_docx_report(
                ProtocolTemplate("protocol", DEFAULT_TEMPLATE, "Стандартный протокол"),
                context,
                output,
            )
            self.assertEqual(result, output)
            self.assertFalse(output.with_name(output.name + ".tmp").exists())
            with zipfile.ZipFile(output) as package:
                names = set(package.namelist())
                self.assertIn("word/document.xml", names)
                self.assertIn("word/styles.xml", names)
                self.assertIn("word/header1.xml", names)
                self.assertIn("word/footer1.xml", names)
                self.assertIn("word/_rels/document.xml.rels", names)
                document = package.read("word/document.xml").decode()
                styles = package.read("word/styles.xml").decode()
                footer = package.read("word/footer1.xml").decode()
                relationships = package.read("word/_rels/document.xml.rels").decode()
                settings = package.read("word/settings.xml").decode()
            self.assertIn("Иванов", document)
            self.assertIn("Лаборатория", document)
            self.assertIn("125", document)
            self.assertIn("Содержание", document)
            self.assertIn("<w:tbl>", document)
            self.assertIn("w:headerReference", document)
            self.assertIn("w:footerReference", document)
            self.assertIn("w:updateFields", settings)
            self.assertIn("PAGE", footer)
            self.assertIn("PAGEREF _ori_heading_", document)
            self.assertIn("w:bookmarkStart", document)
            self.assertIn("Heading1", styles)
            document_root = ET.fromstring(document)
            cell_texts = [
                node.text
                for node in document_root.iter(f"{{{W_NS}}}t")
                if node.text is not None
            ]
            self.assertNotIn("---", cell_texts)
            self.assertNotIn("--------------", cell_texts)
            root = ET.fromstring(relationships)
            ids = [item.attrib["Id"] for item in root]
            self.assertEqual(len(ids), len(set(ids)))
            for target in ["styles.xml", "header1.xml", "footer1.xml", "settings.xml"]:
                self.assertIn(f'Target="{target}"', relationships)

    def test_markdown_alignment_delimiters_are_not_table_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "aligned.docx"
            template = ProtocolTemplate(
                "protocol",
                "# Report\n## Data\n| A | B |\n| :--- | ---: |\n| 1 | 2 |",
                "Тестовый протокол",
            )
            write_docx_report(template, ReportContext(), output)
            with zipfile.ZipFile(output) as package:
                document = package.read("word/document.xml").decode()
            root = ET.fromstring(document)
            texts = [node.text for node in root.iter(f"{{{W_NS}}}t")]
            self.assertIn("A", texts)
            self.assertIn("1", texts)
            self.assertNotIn(":---", texts)
            self.assertNotIn("---:", texts)
            self.assertIn("PAGEREF _ori_heading_", document)

    def test_empty_outer_table_cells_keep_their_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "empty-cells.docx"
            template = ProtocolTemplate(
                "protocol",
                "# Report\n| A | B | C |\n| --- | --- | --- |\n||10|20|\n|30|40||",
                "Тестовый протокол",
            )
            write_docx_report(template, ReportContext(), output)
            with zipfile.ZipFile(output) as package:
                root = ET.fromstring(package.read("word/document.xml"))
            rows = [
                [
                    "".join(cell.itertext())
                    for cell in row.findall(f"{{{W_NS}}}tc")
                ]
                for row in root.iter(f"{{{W_NS}}}tr")
            ]
            self.assertEqual(rows[0], ["A", "B", "C"])
            self.assertEqual(rows[1], ["", "10", "20"])
            self.assertEqual(rows[2], ["30", "40", ""])

    def test_concurrent_writes_use_independent_staging_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "shared.docx"
            template = ProtocolTemplate("protocol", DEFAULT_TEMPLATE, "Стандартный протокол")
            barrier = threading.Barrier(2)
            original_replace = Path.replace

            def synchronized_replace(source: Path, destination: Path) -> Path:
                if source.parent == root and source.suffix == ".tmp":
                    barrier.wait(timeout=10)
                return original_replace(source, destination)

            def write(operator: str) -> Path:
                context = ReportContext().set("operator", operator)
                return write_docx_report(template, context, output)

            with mock.patch.object(Path, "replace", synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(write, ("Первый", "Второй")))
            self.assertEqual(results, [output, output])
            self.assertEqual(list(root.glob("*.tmp")), [])
            with zipfile.ZipFile(output) as package:
                for name in package.namelist():
                    if name.endswith((".xml", ".rels")):
                        ET.fromstring(package.read(name))
                document = package.read("word/document.xml").decode()
            self.assertTrue("Первый" in document or "Второй" in document)

    def test_invalid_xml_characters_are_removed_from_all_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "control.docx"
            context = ReportContext().set("operator", "Иванов\x00\x0b\ud800 & Co")
            write_docx_report(
                ProtocolTemplate("protocol", DEFAULT_TEMPLATE, "Стандартный протокол"),
                context,
                output,
            )
            with zipfile.ZipFile(output) as package:
                for name in package.namelist():
                    if name.endswith((".xml", ".rels")):
                        with self.subTest(part=name):
                            ET.fromstring(package.read(name))
                document = package.read("word/document.xml").decode()
            self.assertIn("Иванов", document)
            self.assertIn(" &amp; Co", document)
            self.assertNotIn("\x00", document)
            self.assertNotIn("\x0b", document)
            self.assertNotIn("\ud800", document)

    def test_docx_format_requires_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "sample.pan"
            settings = ScanSettings(
                start_hz=30e6,
                stop_hz=90e6,
                bandwidth_hz=100e3,
                step_hz=125e3,
                points=801,
            )
            with PanoramaStore(database) as store:
                store.append(generate_synthetic_trace(settings, seed=1))
            code = main(["report", "--database", str(database), "--format", "docx"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
