"""Keep the HTTP source dataset intact while bounding its durable seed commits."""

import tempfile
import unittest
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_platform import BASE
from tests.test_sweep_context import seed_context_http_archive


class ContextHTTPFixtureTests(unittest.TestCase):
    def test_seed_retains_all_sources_and_events_in_two_full_measurement_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSessionStore(str(Path(directory) / "archive.sqlite"))
            service = SessionService(store=store, clock=lambda: BASE)
            transactions = []
            source_writes = 0

            def trace(statement):
                nonlocal source_writes
                if statement.startswith("BEGIN"):
                    source_writes = 0
                elif statement.lstrip().startswith("INSERT INTO sweeps ("):
                    source_writes += 1
                elif statement == "COMMIT":
                    transactions.append(source_writes)

            try:
                self.assertEqual(store._connection.execute("PRAGMA synchronous").fetchone()[0], 2)
                store._connection.set_trace_callback(trace)
                session = seed_context_http_archive(service)
                store._connection.set_trace_callback(None)
                other = next(item for item in service.list_sessions() if item.id != session.id)
                sources = service.list_sweeps(session.id, limit=None)
                foreign = service.list_sweeps(other.id, limit=None)
                self.assertEqual(
                    [item.id for item in sources],
                    ["sweep-0", "sweep-1", "sweep-2", "sweep-3", " exact/+ sweep ", "😀" * 128]
                    + [f"sweep-{number}" for number in range(10, 19)],
                )
                self.assertEqual([item.id for item in foreign], ["sweep-50"])
                self.assertEqual([len(item.levels) for item in sources + foreign], [4] * 6 + [16384] * 9 + [4])
                events = service.events()
                self.assertEqual([event.sequence for event in events], list(range(1, 19)))
                self.assertEqual(
                    [event.event_type for event in events],
                    ["session.created"] + ["sweep.received"] * 6
                    + ["session.created"] + ["sweep.received"] * 10,
                )
                self.assertEqual(events[7].session_id, other.id)
                self.assertEqual([event.payload["sweep"]["id"] for event in events if event.event_type == "sweep.received"],
                                 [item.id for item in sources[:6]] + ["sweep-50"] + [item.id for item in sources[6:]])
                self.assertEqual([count for count in transactions if count], [6, 10])
                self.assertEqual(transactions, [0, 6, 0, 10])
            finally:
                store._connection.set_trace_callback(None)
                service.close()
