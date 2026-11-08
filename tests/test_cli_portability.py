"""CLI resource lifetime and redirected UTF-8 output contracts."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.cli import main
from radioinspector.panorama import PanoramaStore


class CliPortabilityTests(unittest.TestCase):
    def test_devices_output_is_utf8_on_legacy_redirected_stream(self):
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="cp1252")
        with patch("sys.stdout", stream):
            self.assertEqual(main(["devices"]), 0)
        stream.flush()
        self.assertIn("транспорт", output.getvalue().decode("utf-8"))

    def test_demo_closes_database_after_success_and_failure(self):
        close = PanoramaStore.close
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                closed = []

                def record_close(store):
                    closed.append(store.path)
                    close(store)

                with patch.object(PanoramaStore, "close", record_close), patch("sys.stdout", io.StringIO()):
                    if fail:
                        with patch("radioinspector.cli._synthetic_panorama", side_effect=RuntimeError("injected")):
                            with self.assertRaisesRegex(RuntimeError, "injected"):
                                main(["demo", "--output", directory, "--cycles", "1"])
                    else:
                        self.assertEqual(main(["demo", "--output", directory, "--cycles", "1"]), 0)
                self.assertEqual(len(closed), 1)
                self.assertEqual(Path(closed[0]).parent, Path(directory))
