"""Проверка SQL preflight автоматического завершения."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from radioinspector.contracts import TimeInterval, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore, _archive_time
from tests.test_investigations import candidate_payload
from tests.test_persistence import observation_payload
from tests.test_platform import BASE, session_payload


class CompletionPersistenceTests(unittest.TestCase):
    def test_matching_observations_uses_sql_threshold_and_expression_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteSessionStore(str(Path(temporary) / "completion.sqlite")
            service = SessionService(store=store)
            try:
                session = service.create_session(session_payload())
                spectrum_id, receiver_id = (item.id for item in session.streams)
                service.start_session(session.id, at=BASE)
                service.create_candidate(session.id, candidate_payload(spectrum_id, "sql-preflight"))
                investigation = service.enqueue_investigation(
                    session.id,
                    "sql-preflight",
                    {"id": "sql-preflight-investigation", "target_stream_id": receiver_id},
                )
                investigation = service.start_investigation(
                    session.id, investigation.id, tune=lambda *_args: {}
                )
                threshold = investigation.started_at + timedelta(seconds=5)
                for number in (0, 10, 11):
                    started = investigation.started_at + timedelta(seconds=number)
                    payload = observation_payload(session.id, receiver_id, number)
                    payload["frequency_hz"] = investigation.target_frequency_hz
                    payload["interval"] = to_primitive(
                        TimeInterval(started, started + timedelta(seconds=1))
                    )
                    service.ingest_observation(payload)
                statements: list[str] = []
                store._connection.set_trace_callback(statements.append)
                matching = service.matching_observation_ids(session.id, investigation.id)
                filtered = store.matching_observation_ids(
                    session.id, receiver_id, investigation.target_frequency_hz, threshold
                )
                plan = store._connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT id, started_at FROM observations
                    WHERE session_id = ? AND stream_id = ? AND frequency_hz = ?
                      AND (
                        CASE WHEN instr(started_at, '.') = 0
                            THEN replace(started_at, 'Z', '.000000Z')
                            ELSE started_at END
                      ) >= ?
                    """,
                    (session.id, receiver_id, investigation.target_frequency_hz, _archive_time(threshold)),
                ).fetchall()
                store._connection.set_trace_callback(None)
                self.assertEqual(len(matching), 3)
                self.assertEqual(len(filtered), 2)
                normalized = [" ".join(statement.split()) for statement in statements]
                self.assertTrue(any(">=" in statement for statement in normalized))
                self.assertTrue(
                    any("observations_target_time" in row[3] for row in plan),
                    f"Query plan does not use expression index: {plan}",
                )
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
