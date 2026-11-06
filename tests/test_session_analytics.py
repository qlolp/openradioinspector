"""Read-only аналитика архива memory/SQLite и HTTP."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from radioinspector.session_analytics import analytics_summary
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_archive_export import HttpNode
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


class SessionAnalyticsTests(unittest.TestCase):
    def populate(self, service: SessionService):
        session = service.create_session(session_payload())
        spectrum_id, receiver_id = session.streams[0].id, session.streams[1].id
        for number in (1, 0, 2):
            service.ingest_sweep(sweep_payload(session.id, spectrum_id, number))
        for number, squelch in ((1, False), (0, True), (2, None)):
            payload = observation_payload(session.id, receiver_id, number)
            payload["squelch_open"] = squelch
            service.ingest_observation(payload)
        return session, receiver_id

    def assert_summary(self, summary: dict, receiver_id: str) -> None:
        self.assertTrue(summary["archive_wide"])
        self.assertEqual(summary["sweeps"]["count"], 3)
        self.assertEqual(summary["sweeps"]["latest_peak"]["level_unit"], "dBm")
        self.assertEqual(summary["sweeps"]["latest_peak"]["frequency_hz"], 100_100_002)
        self.assertEqual(summary["observations"]["squelch_known"], 2)
        self.assertEqual(summary["observations"]["squelch_open"], 1)
        self.assertEqual(summary["observations"]["squelch_open_ratio"], 0.5)
        self.assertEqual(summary["observations"]["latest"]["stream_id"], receiver_id)

    def test_memory_and_sqlite_archive_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for persistent in (False, True):
                with self.subTest(persistent=persistent):
                    store = (
                        SQLiteSessionStore(str(Path(temporary) / "analytics.sqlite"))
                        if persistent else None
                    )
                    service = SessionService(store=store)
                    try:
                        session, receiver_id = self.populate(service)
                        self.assert_summary(
                            analytics_summary(service, session.id), receiver_id
                        )
                        filtered = analytics_summary(
                            service, session.id, stream_id=receiver_id
                        )
                        self.assertEqual(filtered["sweeps"]["count"], 0)
                        self.assertIsNone(filtered["sweeps"]["latest_peak"])
                        self.assertEqual(filtered["observations"]["count"], 3)
                    finally:
                        service.close()

    def test_http_summary_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            node = HttpNode(str(Path(temporary) / "node.sqlite"))
            try:
                session, receiver_id = self.populate(node.service)
                status, response = node.request(
                    f"/api/v1/sessions/{session.id}/analytics/summary"
                )
                self.assertEqual(status, 200)
                self.assert_summary(response["data"], receiver_id)
                status, capabilities = node.request("/api/v1/capabilities")
                self.assertTrue(capabilities["data"]["features"]["session_analytics"])
                self.assertTrue(
                    capabilities["data"]["features"]["session_analytics_archive_wide"]
                )
                status, missing = node.request(
                    "/api/v1/sessions/missing/analytics/summary"
                )
                self.assertEqual(status, 404)
                self.assertEqual(missing["error"]["code"], "session_not_found")
                status, foreign = node.request(
                    f"/api/v1/sessions/{session.id}/analytics/summary?stream_id=foreign"
                )
                self.assertEqual(status, 400)
                self.assertEqual(foreign["error"]["code"], "invalid_request")
            finally:
                node.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
