import unittest
from datetime import timedelta

from radioinspector.contracts import TimeInterval
from tests import test_monitoring_guard as guard_tests
from tests.test_monitoring_detector import sweep


class PreparationTests(unittest.TestCase):
    setUp=guard_tests.GuardTests.setUp

    def test_high_cadence_baseline_spans_sixty_seconds_with_bounded_sources(self):
        for i in range(700):
            item=sweep(i,session=self.session.id,stream=self.session.streams[0].id)
            started=item.interval.started_at-timedelta(seconds=3.9*i)
            item.interval=TimeInterval(started,started+timedelta(seconds=.05));self.sessions.ingest_sweep(item)
        baseline=self.service.create_baseline(self.project['id'],dict(name='Fast',location='Fixture',confirm=True))
        self.assertLessEqual(len(baseline['source_refs']),256)
        from radioinspector.contracts import parse_time
        self.assertGreaterEqual((parse_time(baseline['ended_at'])-parse_time(baseline['started_at'])).total_seconds(),60)
        self.assertEqual(len(self.sessions.list_sweeps(self.session.id,limit=None)),700)
        self.assertAlmostEqual(baseline['sweep_period_seconds'],.1,places=4)

    def test_preparation_does_not_start_until_explicit_action(self):
        value=self.service.save_preparation(self.project['id'],dict(antenna_notes='Receive antenna',receive_path_confirmed=True,location_confirmed=True))
        self.assertTrue(value['preparation']['receive_path_confirmed']);self.assertEqual(self.workers.active_count,0)
        result=self.service.start_preparation(self.project['id'],{})
        self.assertNotEqual(result['project']['active_session_id'],self.session.id)
        self.assertEqual(result['project']['state'],'collecting')
        progress=self.service.preflight(self.project['id'])['baseline_collection']
        self.assertFalse(progress['ready']);self.assertEqual(progress['minimum_seconds'],60)
        self.service.stop_preparation(self.project['id'],{})
        self.assertEqual(self.workers.active_count,0)

    def test_gap_resets_three_of_five_history_and_is_quality_evidence(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        levels=[-100.]*9;levels[4]=-92
        for i in (20,21,40):self.sessions.ingest_sweep(sweep(i,levels,self.session.id,self.session.streams[0].id))
        result=self.service.analyze(self.project['id'])
        self.assertEqual(self.service.store.list('event',self.project['id']),[])
        self.assertEqual(result['quality_issues'],1)
        quality=self.service.store.list('quality',self.project['id'])
        self.assertEqual(quality[0]['sweep_id'],'sweep-40')

    def test_dense_changes_are_summarized_without_silently_losing_tail_peak(self):
        from radioinspector.contracts import Sweep
        from radioinspector.monitoring.detector import Detector,build_baseline
        def large(index,levels=None):
            reference=sweep(index)
            return Sweep(session_id='fixture',stream_id='analyzer',interval=reference.interval,
                frequencies_hz=tuple(100000000+10000*i for i in range(401)),levels=tuple(levels or [-100.]*401),
                level_unit='dBm',settings=reference.settings)
        baseline=build_baseline([large(i) for i in range(20)])
        levels=[-100.]*401
        for index in range(0,200,2):levels[index]=-80
        levels[-1]=-60
        regions=Detector(baseline,self.project['parameters']).step(large(20,levels))[0]
        self.assertEqual(len(regions),1);self.assertEqual(regions[0]['kind'],'dense_change')
        self.assertEqual(regions[0]['component_regions_count'],101)
        self.assertEqual(regions[0]['center_frequency_hz'],104000000)

    def test_signal_after_data_gap_gets_fresh_event_without_old_audio(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        levels=[-100.]*9;levels[4]=-92
        for i in range(20,23):self.sessions.ingest_sweep(sweep(i,levels,self.session.id,self.session.streams[0].id))
        self.service.analyze(self.project['id']);old=self.service.store.list('event',self.project['id'])[0]
        old['analysis']={'state':'observed','audio':{'id':'old-audio'}};self.service.store.put('event',old)
        for i in range(40,43):self.sessions.ingest_sweep(sweep(i,levels,self.session.id,self.session.streams[0].id))
        self.service.analyze(self.project['id'])
        events=self.service.store.list('event',self.project['id']);self.assertEqual(len(events),2)
        self.assertNotIn('analysis',next(x for x in events if x['id']!=old['id']))
        self.assertEqual(self.service.store.get('event',old['id'])['analysis'],old['analysis'])
