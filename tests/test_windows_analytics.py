"""On-demand аналитика Windows без archive-wide запроса в routine polling."""

from __future__ import annotations

import threading
import unittest

from radioinspector.windows.analytics_app import AnalyticsWindowsApp, analytics_text
from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.scheduler_app import SchedulerWindowsApp
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload


class WindowsAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = PlatformClient(f"http://{host}:{port}")
        self.session = self.client.create_session(simulated_session_payload())

    def tearDown(self) -> None:
        self.server.automatic_detection.close()
        self.server.automatic_handoff.close()
        self.server.automatic_completion.close()
        self.server.control_scheduler.close()
        self.server.worker_manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.session_service.close()

    def test_snapshot_does_not_call_archive_analytics(self) -> None:
        calls = []
        original = self.client.analytics_summary

        def counted(session_id):
            calls.append(session_id)
            return original(session_id)

        self.client.analytics_summary = counted
        snapshot = self.client.snapshot(self.session["id"])
        self.assertEqual(snapshot.session["id"], self.session["id"])
        self.assertEqual(calls, [])
        self.assertFalse(hasattr(snapshot, "analytics"))

        summary = self.client.analytics_summary(self.session["id"])
        self.assertEqual(calls, [self.session["id"]])
        self.assertTrue(summary["archive_wide"])
        self.assertEqual(summary["sweeps"]["level_units"], [])
        self.assertEqual(summary["observations"]["level_units"], [])

    def test_explicit_action_captures_session_and_formats_result_or_error(self) -> None:
        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Client:
            def __init__(self, error=False):
                self.calls = []
                self.error = error

            def analytics_summary(self, session_id):
                self.calls.append(session_id)
                if self.error:
                    raise PlatformClientError("old server")
                return {
                    "sweeps": {"count": 2, "level_units": ["dBm"]},
                    "observations": {
                        "count": 1,
                        "level_units": ["s_meter_count"],
                        "squelch_open_ratio": 1.0,
                    },
                }

        for with_error in (False, True):
            with self.subTest(with_error=with_error):
                app = AnalyticsWindowsApp.__new__(AnalyticsWindowsApp)
                app.client = Client(error=with_error)
                app.session_id = "session/current"
                app._action_running = False
                app._closed = False
                app._analytics_session_id = None
                app.analytics_var = Value()
                actions = []

                def submit(description, operation, callback):
                    app._action_running = True
                    actions.append((description, operation, callback))

                app._submit_action = submit
                app.refresh_analytics()
                app.refresh_analytics()
                self.assertEqual(len(actions), 1)
                _description, operation, callback = actions[0]
                callback(operation())
                self.assertEqual(app.client.calls, ["session/current"])
                if with_error:
                    self.assertIn("Live-измерения", app.analytics_var.value)
                    self.assertIn("old server", app.analytics_var.value)
                else:
                    self.assertIn("2 sweeps dBm", app.analytics_var.value)
                    self.assertIn("1 observations s_meter_count", app.analytics_var.value)

    def test_analytics_text_preserves_peak_and_source_units(self) -> None:
        text = analytics_text({
            "sweeps": {
                "count": 1,
                "level_units": ["dBm"],
                "latest_peak": {
                    "frequency_hz": 100100000,
                    "level": -42.0,
                    "level_unit": "dBm",
                },
            },
            "observations": {"count": 0, "level_units": []},
        })
        self.assertIn("100100000 Гц", text)
        self.assertIn("dBm", text)
        self.assertNotIn("s_meter_count ·", text)

    def test_installed_scheduler_inherits_on_demand_analytics(self) -> None:
        self.assertTrue(issubclass(SchedulerWindowsApp, AnalyticsWindowsApp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
