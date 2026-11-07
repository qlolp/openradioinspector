"""Проверка собственного DOCX writer протоколов."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from radioinspector.cli import main
from radioinspector.docx_report import write_docx_report
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
            self.assertIn("Heading1", styles)
            root = ET.fromstring(relationships)
            ids = [item.attrib["Id"] for item in root]
            self.assertEqual(len(ids), len(set(ids)))
            for target in ["styles.xml", "header1.xml", "footer1.xml", "settings.xml"]:
                self.assertIn(f'Target="{target}"', relationships)

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
