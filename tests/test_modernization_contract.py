"""A literal shared fixture anchors UTC, integer Hz and IEEE-754 fidelity."""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from radioinspector.contracts import Sweep, ReceiverObservation
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.session_service import SessionService


class ModernizationContractTests(unittest.TestCase):
    def test_shared_sources_preserve_exact_values_across_archive_reopen(self):
        path = Path(__file__).resolve().parents[1] / 'contracts/fixtures/measurement-v1.json'
        self.assertTrue(path.is_file(), 'Shared modernization measurement fixture is missing')
        payload = json.loads(path.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temporary:
            archive = str(Path(temporary) / 'archive.sqlite')
            service = SessionService(store=SQLiteSessionStore(archive))
            try:
                session = service.create_session(payload['session'])
                service.ingest_sweep(payload['sweep'])
                service.ingest_observation(payload['observation'])
            finally:
                service.close()
            restored = SessionService(store=SQLiteSessionStore(archive))
            try:
                sweep = restored.list_sweeps(session.id)[0]
                observation = restored.list_observations(session.id)[0]
                self.assertEqual(sweep, Sweep.from_dict(payload['sweep']))
                self.assertEqual(observation, ReceiverObservation.from_dict(payload['observation']))
                self.assertEqual(sweep.frequencies_hz, (100000001, 100000002, 100000003, 100000004))
                self.assertEqual(sweep.interval.started_at.microsecond, 123456)
                self.assertEqual(sweep.interval.started_at.hour, 12)
                self.assertEqual(struct.pack('>d', sweep.levels[1]).hex(), '8000000000000000')
                self.assertEqual(struct.pack('>d', sweep.levels[2]).hex(), 'bff0000000000001')
                self.assertEqual(observation.mode, 'FM')
                self.assertEqual((observation.level, observation.level_unit), (137.0, 's_meter_count'))
            finally:
                restored.close()
