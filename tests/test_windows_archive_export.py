"""Windows: явный NDJSON export с исходными единицами и атомарной записью."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from radioinspector.windows.analytics_app import AnalyticsWindowsApp
from radioinspector.windows.client import PlatformClient
from tests.test_archive_export import HttpNode
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


class WindowsArchiveExportTests(unittest.TestCase):
    def test_client_downloads_server_snapshot_with_raw_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            node = HttpNode(str(Path(temporary) / "node.sqlite")
            )
            try:
                session = node.service.create_session(session_payload())
                spectrum_id, receiver_id = (item.id for item in session.streams)
                node.service.ingest_sweeps([
                    sweep_payload(session.id, spectrum_id, number) for number in range(2)
                ])
                node.service.ingest_observation(
                    observation_payload(session.id, receiver_id)
                )
                client = PlatformClient(node.url)
                output = Path(temporary) / "windows-export.ndjson"
                result = client.export_archive(session.id, str(output))
                self.assertEqual((result["sweeps"], result["observations"]), (2, 1))
                text = output.read_text(encoding="utf-8")
                self.assertIn("openradioinspector-archive-ndjson", text)
                self.assertIn('"level_unit":"dBm"', text)
                self.assertIn('"level_unit":"s_meter_count"', text)
                self.assertEqual(result["snapshot"], "immutable")
            finally:
                node.stop()

    def test_export_action_captures_session_and_shows_result_or_error(self) -> None:
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Dialog:
            def asksaveasfilename(self, **_kwargs):
                return "/tmp/operator-export.ndjson"

        class Client:
            def __init__(self, error=False):
                self.calls = []
                self.error = error

            def export_archive(self, session_id, destination):
                self.calls.append((session_id, destination))
                if self.error:
                    from radioinspector.archive_ingest import ArchiveIngestError

                    raise ArchiveIngestError("incomplete_stream")
                return {"sweeps": 2, "observations": 1, "sha256": "abc123"}

        for with_error in (False, True):
            with self.subTest(with_error=with_error):
                app = AnalyticsWindowsApp.__new__(AnalyticsWindowsApp)
                app.client = Client(error=with_error)
                app.session_id = "session/current"
                app._action_running = False
                app._closed = False
                app.archive_export_var = Value()
                app.tk = type("Tk", (), {"filedialog": Dialog()})()
                actions = []
                app._submit_action = lambda _description, operation, callback: actions.append(
                    (operation, callback)
                )
                app.export_archive()
                self.assertEqual(len(actions), 1)
                operation, callback = actions[0]
                callback(operation())
                self.assertEqual(
                    app.client.calls,
                    [("session/current", "/tmp/operator-export.ndjson")],
                )
                if with_error:
                    self.assertIn("incomplete_stream", app.archive_export_var.value)
                    self.assertIn("Live-измерения", app.archive_export_var.value)
                else:
                    self.assertIn("2 sweeps", app.archive_export_var.value)
                    self.assertIn("abc123", app.archive_export_var.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
