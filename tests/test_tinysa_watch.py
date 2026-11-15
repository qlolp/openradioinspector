"""No USB: bounded multi-band learning, detection and durable event provenance."""
import copy
import unittest
from datetime import timedelta
from tests.test_monitoring_detector import sweep
from radioinspector.tinysa.watch import SpectrumWatch, validate_plan


def plan():
    return dict(bands=[dict(name='Test', start_hz=100000000, stop_hz=100080000, rbw_hz=10000, points=9)],
                margin_db=6, impulse_db=12, confirmations=3, window_sweeps=5,
                baseline_sweeps=20, baseline_seconds=60, allowed=[])


class WatchTests(unittest.TestCase):
    def ready(self):
        watch=SpectrumWatch(validate_plan(plan()))
        for i in range(20): watch.observe(sweep(i),0)
        self.assertEqual(watch.status()['bands'][0]['state'],'watching')
        return watch

    def test_shared_contract_fixture(self):
        import json
        from pathlib import Path
        fixture=json.loads((Path(__file__).resolve().parents[1]/'contracts/fixtures/tinysa-watch-v1.json').read_text())
        watch=SpectrumWatch(validate_plan(fixture['request']['plan']))
        for i in range(fixture['baseline']['count']):watch.observe(sweep(i,fixture['baseline']['levels']),0)
        events=[]
        for i in range(20,23):events+=watch.observe(sweep(i,fixture['changed']['levels']),0)
        self.assertEqual(len(events),1)
        self.assertEqual({k:events[0][k] for k in fixture['expected_event']},fixture['expected_event'])

    def test_learning_requires_count_and_time_and_preserves_original(self):
        watch=SpectrumWatch(validate_plan(plan())); original=sweep(0); before=copy.deepcopy(original)
        watch.observe(original,0)
        self.assertEqual(original,before)
        self.assertEqual(watch.status()['bands'][0]['state'],'learning')

    def test_persistent_signal_single_open_event_then_clear(self):
        watch=self.ready(); levels=[-100.]*9;levels[4]=-92
        self.assertEqual(watch.observe(sweep(20,levels),0),[])
        self.assertEqual(watch.observe(sweep(21,levels),0),[])
        opened=watch.observe(sweep(22,levels),0)
        self.assertEqual(opened[0]['transition'],'opened')
        self.assertEqual(opened[0]['source']['sweep_id'],'sweep-22')
        for i in range(23,50): self.assertEqual(watch.observe(sweep(i,levels),0),[])
        closed=[]
        for i in range(50,54):closed+=watch.observe(sweep(i),0)
        self.assertEqual([e['transition'] for e in closed],['closed'])
        self.assertEqual(closed[0]['event_id'],opened[0]['event_id'])

    def test_invalid_plan_never_silently_clamps_or_accepts_rf(self):
        for patch in [dict(bands=[]),dict(confirmations=6),dict(margin_db=float('nan')),
                      dict(output_enabled=True),dict(baseline_sweeps=True)]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):validate_plan({**plan(),**patch})
        bad=plan();bad['bands'][0]['stop_hz']=7000000000
        with self.assertRaises(ValueError):validate_plan(bad)

    def test_gap_breaks_confirmations_and_marks_active_event_interrupted(self):
        watch=self.ready(); levels=[-100.]*9;levels[4]=-85
        watch.observe(sweep(20,levels),0)
        events=watch.observe(sweep(100,levels),0)
        self.assertIn('data_gap',[e['transition'] for e in events])

    def test_impulse_and_noise_lifecycle_do_not_flood_journal(self):
        import random
        randomizer=random.Random(20261115)
        watch=self.ready()
        for i in range(20,1000):
            self.assertEqual(watch.observe(sweep(i,[-100+randomizer.uniform(-2,2) for _ in range(9)]),0),[])
        levels=[-100.]*9;levels[3]=-80
        events=watch.observe(sweep(1000,levels),0)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'impulse')
        for i in range(1001,1100):watch.observe(sweep(i),0)
        self.assertEqual([e['transition'] for e in watch.recent],['opened','closed'])
        self.assertEqual(watch.status()['events_opened'],1)

    def test_changed_settings_do_not_relearn_silently(self):
        watch=self.ready();changed=sweep(20);changed.settings['rbw_hz']=30000
        with self.assertRaises(ValueError):watch.observe(changed,0)

    def test_24_hour_run_has_bounded_state_and_no_false_events(self):
        watch=self.ready()
        for i in range(20,21620):self.assertEqual(watch.observe(sweep(i),0),[])
        state=watch.bands[0]
        self.assertLessEqual(len(state['samples']),256)
        self.assertLessEqual(len(state['detector'].state['window']),5)
        self.assertEqual(watch.status()['events_opened'],0)

    def test_allowed_carrier_is_not_unknown(self):
        config=plan();config['allowed']=[dict(start_hz=100040000,end_hz=100040000)]
        watch=SpectrumWatch(validate_plan(config))
        for i in range(20):watch.observe(sweep(i),0)
        levels=[-100.]*9;levels[4]=-50
        self.assertEqual(watch.observe(sweep(20,levels),0),[])

class ReceiveIntegrationTests(unittest.TestCase):
    def controller(self):
        from tests.tinysa_control_fixture import make_adapter
        from radioinspector.tinysa.controller import Controller
        self.service,self.session,self.adapter,self.wire=make_adapter(self,{
            'sweep 100000000 100080000 9':b'', 'repeat 10':b'', 'rbw 30':b'',
            'sweep 100000000 101000000 3':b'', 'sweep 100000000 100000000 3':b''})
        self.c=Controller(self.adapter);self.adapter._instrument=self.c
        self.c.execute(dict(operation='inspect',parameters={}))
        return self.c

    def test_invalid_batch_sends_no_partial_commands(self):
        c=self.controller();before=list(self.wire.sent)
        for values in [dict(rbw_hz=30000,points=1000),dict(lna=True,attenuation_db=10),dict(start_hz=200000000),dict(output_enabled=True)]:
            with self.assertRaises(ValueError):c.execute(dict(operation='receive.configure',parameters=values))
        self.assertEqual(before,self.wire.sent)

    def test_vbw_is_explicit_repeat_mapping_without_readback_claim(self):
        c=self.controller();c.execute(dict(operation='receive.configure',parameters=dict(vbw_ratio='1/10')))
        self.assertEqual(self.wire.sent[-1],'repeat 10')
        self.assertEqual(c.status()['settings']['repeat'],10)
        self.assertFalse(c.status()['settings_readback'])

    def test_watch_owns_settings_and_never_requests_output_on(self):
        import json
        c=self.controller();c.execute(dict(operation='watch.start',parameters=dict(document=json.dumps(plan()))))
        c.before_poll();self.assertEqual(c.watch_band,0)
        before=list(self.wire.sent)
        with self.assertRaises(ValueError):c.execute(dict(operation='setting',parameters=dict(name='rbw_hz',value=30000)))
        self.assertEqual(before,self.wire.sent)
        self.assertNotIn('output on',self.wire.sent)
        c.execute(dict(operation='watch.stop',parameters={}))
        self.assertIsNone(c.watch)

    def test_zero_span_batch_accepts_equal_edges(self):
        c=self.controller();c.execute(dict(operation='receive.configure',parameters=dict(start_hz=100000000,stop_hz=100000000)))
        self.assertEqual(self.adapter.start_hz,self.adapter.stop_hz)

    def test_multi_band_cycle_and_independent_baselines(self):
        import json
        c=self.controller();p=plan();p['bands'].append({**p['bands'][0],'name':'Second','rbw_hz':30000})
        c.execute(dict(operation='watch.start',parameters=dict(document=json.dumps(p))))
        for i in range(50):
            c.before_poll();index=c.watch_band
            item=sweep(i);item.settings['rbw_hz']=p['bands'][index]['rbw_hz']
            c.after_committed_sweep(item)
            self.assertEqual(index,i%2)
        self.assertEqual([b['state'] for b in c.watch.status()['bands']],['watching','watching'])
        self.assertEqual([b['count'] for b in c.watch.bands],[25,25])

    def test_baseline_and_event_artifacts_reference_originals(self):
        import json,base64
        c=self.controller();c.execute(dict(operation='watch.start',parameters=dict(document=json.dumps(plan()))))
        c.before_poll();artifacts=[]
        for i in range(20):artifacts+=c.after_committed_sweep(sweep(i))
        self.assertEqual(len(artifacts),1)
        baseline=json.loads(base64.b64decode(artifacts[0]['data_base64']))
        self.assertEqual(len(baseline['baseline']['source_refs']),20)
        levels=[-100.]*9;levels[2]=-50
        result=c.after_committed_sweep(sweep(20,levels))
        event=json.loads(base64.b64decode(result[0]['data_base64']))['events'][0]
        self.assertEqual(event['source']['sweep_id'],'sweep-20')
        self.assertEqual(event['transition'],'opened')


class WatchArchiveFailureTests(unittest.TestCase):
    def test_journal_failure_stops_after_original_commit(self):
        from unittest.mock import patch
        from radioinspector.device_workers import DeviceWorker, create_simulated_adapter
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload, wait_until
        sessions=SessionService();self.addCleanup(sessions.close)
        session=sessions.create_session(simulated_session_payload())
        sessions.start_session(session.id)
        adapter=create_simulated_adapter(session.streams[0])
        seen=[]
        def committed(item):
            seen.append(item.id)
            self.assertEqual(sessions.list_sweeps(session.id)[0].id,item.id)
            return [dict(kind='diagnostic')]
        adapter.after_committed_sweep=committed
        worker=DeviceWorker(sessions,adapter);self.addCleanup(worker.stop)
        with patch.object(sessions,'save_instrument_artifact',side_effect=OSError('disk full')):
            worker.start()
            wait_until(lambda:not worker.status()['alive'],'must fail stop')
        self.assertEqual(len(seen),1)
        self.assertEqual(len(sessions.list_sweeps(session.id)),1)
        self.assertEqual(worker.status()['state'],'error')
        self.assertIn('disk full',worker.status()['last_error'])
