"""Requested modes constrain both manual and automatic completion."""
import unittest
from dataclasses import replace
from datetime import timedelta

from radioinspector.automatic_completion import AutomaticInvestigationCompletion
from radioinspector.contracts import CandidateState, InvestigationState
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_device_workers import simulated_session_payload
from tests.test_investigations import BASE, Clock, candidate_payload, observation


class ObservationModeTests(unittest.TestCase):
    def setup_service(self, sqlite, mode):
        self.clock = Clock()
        store = SQLiteSessionStore(':memory:') if sqlite else None
        service = SessionService(clock=self.clock, store=store)
        self.addCleanup(service.close)
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        source, receiver = (x.id for x in session.streams)
        candidate = service.create_candidate(session.id, candidate_payload(source))
        investigation = service.enqueue_investigation(session.id, candidate.id,
            {'target_stream_id': receiver, 'mode': mode})
        investigation = service.start_investigation(session.id, investigation.id, tune=lambda *args: {})
        wrong = replace(observation(session.id, receiver, 'wrong-mode', BASE + timedelta(seconds=1)),
                        mode='AM', decoded_mode=None)
        service.ingest_observation(wrong)
        self.clock.advance(5)
        return service, session, candidate, investigation, wrong

    def test_manual_rejects_other_mode_and_includes_only_matching_observations(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite):
                service, session, candidate, investigation, wrong = self.setup_service(sqlite, 'FM')
                self.assertEqual(service.matching_observation_ids(session.id, investigation.id, limit=1), ())
                with self.assertRaises(ValueError):
                    service.complete_investigation(session.id, investigation.id)
                self.assertEqual(service.get_candidate(session.id, candidate.id).state, CandidateState.INVESTIGATING)
                service.ingest_observation(replace(wrong, id='right-mode', mode='FM'))
                self.assertEqual(service.matching_observation_ids(session.id, investigation.id, limit=1), ('right-mode',))
                result = service.complete_investigation(session.id, investigation.id)
                self.assertEqual(result.result_observation_ids, ('right-mode',))
                self.assertEqual(result.state, InvestigationState.COMPLETED)

    def test_automatic_rejects_other_mode_then_completes(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite):
                service, session, candidate, investigation, wrong = self.setup_service(sqlite, 'FM')
                scheduler = AutomaticInvestigationCompletion(service, clock=self.clock, start_background=False)
                self.addCleanup(scheduler.close)
                scheduler.configure(session.id, {'enabled': True, 'minimum_observations': 1})
                self.assertIsNone(scheduler.run_once(session.id))
                self.assertEqual(service.get_investigation(session.id, investigation.id).state, InvestigationState.LISTENING)
                service.ingest_observation(replace(wrong, id='right-mode', mode='FM'))
                self.assertEqual(scheduler.run_once(session.id).result_observation_ids, ('right-mode',))

    def test_unspecified_mode_remains_unrestricted(self):
        for sqlite in (False, True):
            for automatic in (False, True):
                with self.subTest(sqlite=sqlite, automatic=automatic):
                    service, session, candidate, investigation, wrong = self.setup_service(sqlite, None)
                    if automatic:
                        scheduler = AutomaticInvestigationCompletion(service, clock=self.clock, start_background=False)
                        self.addCleanup(scheduler.close)
                        scheduler.configure(session.id, {'enabled': True})
                        result = scheduler.run_once(session.id)
                    else:
                        result = service.complete_investigation(session.id, investigation.id)
                    self.assertEqual(result.result_observation_ids, (wrong.id,))
