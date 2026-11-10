"""SQLite history stays complete while the working registry stays bounded."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radioinspector.contracts import CandidateState, InvestigationState
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_device_workers import simulated_session_payload
from tests.test_investigations import Clock, candidate_payload


class BoundedSessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'archive.sqlite')
        self.service = self.open()
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.source, self.target = [stream.id for stream in self.session.streams]

    def open(self):
        return SessionService(store=SQLiteSessionStore(self.path), clock=Clock(), history_limit=32)

    def tearDown(self):
        self.service.close()

    def churn(self, count=350):
        for index in range(count):
            candidate = self.service.create_candidate(self.session.id, candidate_payload(self.source, f'candidate-{index}'))
            investigation = self.service.enqueue_investigation(self.session.id, candidate.id,
                {'id': f'investigation-{index}', 'target_stream_id': self.target})
            self.service.cancel_investigation(self.session.id, investigation.id)

    def test_startup_does_not_materialize_archive_history(self):
        self.churn(3)
        self.service.close()
        store = SQLiteSessionStore(self.path)
        try:
            with patch.object(store, 'load_sessions', side_effect=AssertionError('unbounded session history load')), \
                 patch.object(store, 'load_candidates', side_effect=AssertionError('unbounded candidate history load')), \
                 patch.object(store, 'load_investigations', side_effect=AssertionError('unbounded investigation history load')):
                self.service = SessionService(store=store, clock=Clock(), history_limit=32)
        except BaseException:
            store.close()
            raise
        self.assertEqual(len(self.service.list_candidates(self.session.id)), 3)
        self.assertEqual(len(self.service.list_investigations(self.session.id)), 3)

    def test_terminal_churn_is_bounded_without_losing_archive_reads(self):
        self.churn()
        self.assertLessEqual(len(self.service._candidates), 256)
        self.assertLessEqual(len(self.service._investigations), 256)
        self.assertLessEqual(len(self.service._investigation_sessions), 256)
        self.assertEqual(len(self.service.list_candidates(self.session.id)), 350)
        self.assertEqual(len(self.service.list_investigations(self.session.id)), 350)
        with self.assertRaises(ValueError):
            self.service.signal_list_export(self.session.id, 'history')
        self.assertEqual(self.service.health()['candidates'], 350)
        before = (len(self.service._candidates), len(self.service._investigations))
        for index in range(350):
            self.assertEqual(self.service.get_candidate(self.session.id, f'candidate-{index}').id, f'candidate-{index}')
            self.assertEqual(self.service.get_investigation(self.session.id, f'investigation-{index}').state, InvestigationState.CANCELLED)
        self.assertEqual((len(self.service._candidates), len(self.service._investigations)), before)

    def test_signal_list_export_reads_evicted_candidates(self):
        self.churn(200)
        artifact = self.service.signal_list_export(self.session.id, 'history')
        self.assertEqual(artifact['exported'], 200)

    def test_restart_reads_only_active_graph_and_bounded_event_tail(self):
        self.service.create_candidate(self.session.id, candidate_payload(self.source, 'pending'))
        self.service.enqueue_investigation(self.session.id, 'pending', {'id': 'pending-job', 'target_stream_id': self.target, 'priority': 9})
        self.churn()
        self.service.close()
        self.service = self.open()
        self.assertLessEqual(len(self.service._candidates), 256)
        self.assertLessEqual(len(self.service._investigations), 256)
        self.assertIn('pending-job', self.service._investigations)
        self.assertIn('pending', self.service._candidates)
        self.assertEqual(self.service.next_investigation(self.session.id).id, 'pending-job')
        self.assertEqual(len(self.service._events), 32)
        self.service.stop_session(self.session.id)
        self.assertEqual(self.service.get_investigation(self.session.id, 'pending-job').state, InvestigationState.CANCELLED)

    def test_open_graph_survives_history_churn_and_runtime_recovery(self):
        self.service.create_candidate(self.session.id, candidate_payload(self.source, 'active'))
        self.service.enqueue_investigation(self.session.id, 'active',
            {'id': 'active-job', 'target_stream_id': self.target})
        self.service.start_investigation(self.session.id, 'active-job', tune=lambda *_: {})
        self.churn()
        self.assertIn('active-job', self.service._investigations)
        self.assertIn('active', self.service._candidates)
        self.assertIn(self.session.id, self.service._sessions)
        self.service.close()
        self.service = self.open()
        job = self.service.get_investigation(self.session.id, 'active-job')
        self.assertEqual(job.state, InvestigationState.FAILED)
        self.assertIn('перезапуском', job.error)
        self.assertEqual(self.service.get_candidate(self.session.id, 'active').state, CandidateState.QUEUED)
        self.assertIsNone(self.service.next_investigation(self.session.id))

    def test_pending_manual_queue_larger_than_tail_is_pinned(self):
        for index in range(140):
            candidate_id = f'pending-{index:03d}'
            self.service.create_candidate(self.session.id, candidate_payload(self.source, candidate_id))
            self.service.enqueue_investigation(self.session.id, candidate_id, {
                'id': f'queued-{index:03d}', 'target_stream_id': self.target, 'priority': index % 7,
            })
        self.churn(200)
        self.service.close()
        self.service = self.open()
        self.assertEqual(self.service.next_investigation(self.session.id).id, 'queued-006')
        self.assertEqual(len(self.service.list_investigations(self.session.id, state='queued')), 140)
        self.assertTrue(all(f'pending-{index:03d}' in self.service._candidates for index in range(140)))
        self.service.stop_session(self.session.id)
        self.assertEqual(self.service.list_investigations(self.session.id, state='queued'), [])
        self.assertLessEqual(len(self.service._investigations), 256)
        self.assertLessEqual(len(self.service._candidates), 256)

    def test_evicted_ids_cannot_overwrite_archive(self):
        self.churn()
        with self.assertRaises(ValueError):
            self.service.create_candidate(self.session.id, candidate_payload(self.source, 'candidate-0'))
        self.service.create_candidate(self.session.id, candidate_payload(self.source, 'fresh'))
        with self.assertRaises(ValueError):
            self.service.enqueue_investigation(self.session.id, 'fresh', {'id': 'investigation-0', 'target_stream_id': self.target})
        self.assertEqual(self.service.get_investigation(self.session.id, 'investigation-0').candidate_id, 'candidate-0')

    def test_session_history_is_bounded_and_duplicate_streams_remain_rejected(self):
        for index in range(350):
            payload = simulated_session_payload()
            payload['id'] = f'history-{index}'
            for stream_index, stream in enumerate(payload['streams']):
                stream['id'] = f'stream-{stream_index}-{index}'
            item = self.service.create_session(payload)
            self.service.start_session(item.id)
            if index % 2:
                self.service.stop_session(item.id)
        self.assertLessEqual(len(self.service._sessions), 257)
        self.assertEqual(len(self.service.list_sessions()), 351)
        self.assertEqual(self.service.get_session('history-0').state.value, 'running')
        payload = simulated_session_payload()
        payload['id'] = 'duplicate-stream'
        payload['streams'][0]['id'] = 'stream-0-0'
        with self.assertRaises(ValueError):
            self.service.create_session(payload)
        self.assertEqual(self.service.health()['sessions'], 351)


if __name__ == '__main__':
    unittest.main()
