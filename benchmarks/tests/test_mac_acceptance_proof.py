"""Wrong source/build, absent presentation or bad hardware evidence must fail closed."""
import copy
import unittest

from benchmarks.ori_bench.mac_acceptance.proof import native_metrics, hardware_metrics

RUN = 'independent-fixture'
CORE = 'a' * 64
UI = 'b' * 64


def native_fixture():
    commits = [{'stage':'committed','run_id':RUN,'component_sha256':CORE,'source_id':key,
                'sequence':n,'kind':'sweep','at_ns':1_000_000_000+n*1_000_000_000}
               for n,key in enumerate(['first','second','third'],1)]
    events = [{'stage':'native_identity','run_id':RUN,'component_sha256':UI,'at_ns':1}]
    for item, delay in zip(commits,[200_000_000,250_000_000,100_000_000]):
        events += [dict(stage='received',run_id=RUN,source_id=item['source_id'],sequence=item['sequence'],at_ns=item['at_ns']+1),
                   dict(stage='applied',run_id=RUN,source_id=item['source_id'],sequence=item['sequence'],at_ns=item['at_ns']+2),
                   dict(stage='presented',run_id=RUN,source_id=item['source_id'],sequence=item['sequence'],at_ns=item['at_ns']+delay,render_id=item['sequence'])]
    for n in range(100):
        events += [dict(stage='interaction.begin',run_id=RUN,request_id=str(n),at_ns=10_000_000_000+n*100_000_000),
                   dict(stage='interaction.presented',run_id=RUN,request_id=str(n),at_ns=10_025_000_000+n*100_000_000),
                   dict(stage='queue.begin',run_id=RUN,request_id=str(n),at_ns=10_000_000_000+n*100_000_000),
                   dict(stage='queue.accepted',run_id=RUN,request_id=str(n),job_id='job-'+str(n),at_ns=10_050_000_000+n*100_000_000)]
    events += [dict(stage='pan_zoom.begin',run_id=RUN,request_id='pan',at_ns=30_000_000_000),
               dict(stage='pan_zoom.end',run_id=RUN,request_id='pan',at_ns=40_000_000_000)]
    events += [dict(stage='presented',run_id=RUN,source_id='third',sequence=3,
                    at_ns=30_000_000_000+n*100_000_000,render_id=1000+n,
                    viewport_lower_hz=100_000_000+(n%2)*1000,viewport_upper_hz=101_000_000+(n%2)*1000) for n in range(101)]
    return commits,events


def passing_fixture():
    c,e=native_fixture()
    e=[x for x in e if x.get('render_id',0)<1000]
    e += [dict(stage='presented',run_id=RUN,source_id='third',sequence=3,
               at_ns=30_000_000_000+n*10_000_000,render_id=2000+n,
               viewport_lower_hz=100_000_000+(n%2)*1000,viewport_upper_hz=101_000_000+(n%2)*1000)
          for n in range(1001)]
    return c,e


class NativeAcceptanceProofTests(unittest.TestCase):
    def verify(self, commits, events):
        return native_metrics(commits,events,run_id=RUN,core_sha256=CORE,native_sha256=UI,
                              expected_ids=['first','second','third'])

    def test_presentation_is_measured_from_commit_and_slow_pan_fails(self):
        c,e=native_fixture()
        result=self.verify(c,e)
        self.assertEqual(result['commit_to_presented']['p95_ms'],250.0)
        self.assertEqual(result['reaction']['p95_ms'],25.0)
        self.assertEqual(result['queue_ack']['p95_ms'],50.0)
        self.assertEqual(result['pan_zoom']['minimum_fps'],10.0)
        self.assertEqual(result['status'],'failed')

    def test_complete_presented_trace_can_pass_without_weakening_the_thresholds(self):
        c,e=passing_fixture()
        result=self.verify(c,e)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['pan_zoom']['minimum_fps'],100.0)

    def test_observer_error_or_boolean_job_identity_cannot_pass(self):
        for fault in ['observer','job_id','duplicate_sequence']:
            with self.subTest(fault=fault):
                c,e=passing_fixture()
                if fault=='observer':e.append(dict(stage='native.error',run_id=RUN,at_ns=42,message='capture stopped'))
                elif fault=='job_id':
                    next(row for row in e if row['stage']=='queue.accepted')['job_id']=True
                else:
                    c[1]['sequence']=1
                    for row in e:
                        if row.get('source_id')=='second':row['sequence']=1
                self.assertEqual(self.verify(c,e)['status'],'failed')

    def test_marker_only_refresh_does_not_establish_pan_zoom_fps(self):
        c,e=passing_fixture()
        for row in e:
            if row['stage']=='presented':row.update(viewport_lower_hz=100_000_000,viewport_upper_hz=101_000_000)
        result=self.verify(c,e)
        self.assertEqual(result['pan_zoom']['status'],'failed')

    def test_gpu_completion_is_not_evidence_of_a_presented_source(self):
        c,e=native_fixture()
        e=[dict(x,stage='gpu_completed') if x.get('source_id')=='second' else x for x in e]
        result=self.verify(c,e)
        self.assertEqual(result['commit_to_presented']['missing'],['second'])
        self.assertEqual(result['commit_to_presented']['status'],'failed')

    def test_other_run_build_source_and_negative_latency_are_rejected(self):
        c,e=native_fixture()
        for change in ['run','core','ui','sequence','clock']:
            with self.subTest(change=change):
                cc,ee=copy.deepcopy(c),copy.deepcopy(e)
                if change=='run':ee[1]['run_id']='other'
                if change=='core':cc[0]['component_sha256']='c'*64
                if change=='ui':ee[0]['component_sha256']='c'*64
                if change=='sequence':ee[3]['sequence']=99
                if change=='clock':ee[3]['at_ns']=0
                self.assertEqual(self.verify(cc,ee)['status'],'failed')

    def test_unanswered_ui_or_unaccepted_queue_request_cannot_be_dropped(self):
        c,e=native_fixture()
        e=[x for x in e if not (x.get('request_id')=='4' and x['stage'] in ['interaction.presented','queue.accepted'])]
        result=self.verify(c,e)
        self.assertEqual(result['reaction']['missing'],['4'])
        self.assertEqual(result['queue_ack']['missing'],['4'])
        self.assertEqual(result['status'],'failed')


class HardwareAcceptanceProofTests(unittest.TestCase):
    def test_complete_observed_fixture_requires_references_and_logical_source_digests(self):
        from benchmarks.tests.test_mac_acceptance_checkpoints import sample
        def event(stage,at,**fields):return dict(stage=stage,at_ns=at,run_id=RUN,component_sha256=CORE,**fields)
        rows=[event('hardware.sample',1+i*10_000_000_000,**sample(id='s'+str(i),analyzer_sources=10+i,
                receiver_sources=3+i,analyzer_source_id='a'+str(i),receiver_source_id='r'+str(i),
                baseline_confirmed=True,baseline_sources=20,baseline_span=60,baseline_id='baseline',
                analyzer_max_silence_seconds=30,receiver_max_silence_seconds=30)) for i in range(2881)]
        rows[2].update(event_id='new-event',event_source_ids=['a2'],receiver_evidence_id='receiver-check')
        rows[4]['coverage']='narrow';rows[7]['ui_alive']=False
        rows[10]['analyzer_present']=False;rows[13]['receiver_present']=False
        rows[16]['sleep_delta_ns']=10_000_000_000
        proof=dict(status='passed',sweeps=2890,observations=2890,missing=0,original_sha256=CORE,restored_sha256=CORE,commits_verified=True)
        restore=event('checkpoint.sample',28_801_000_000_001,**sample(id='restore',restore_proof=proof))
        e=[event('hardware.identity',0,mode='live',analyzer_model='tinySA Ultra',receiver_model='AR-DV1',analyzer_firmware='fixture',receiver_firmware='fixture'),*rows,restore,
           event('hardware.window.begin',1),event('hardware.window.end',28_800_000_000_001),
           event('hardware.source_proof',28_801_000_000_001,**proof)]
        references={'baseline':[0],'guard':[0,1],'event':[0,2],'receiver':[0,2],'source_search':[3,4,5],
                    'ui_quit':[6,7,8],'usb_analyzer':[9,10,11],'usb_receiver':[12,13,14],'sleep':[15,16,17]}
        for name,indexes in references.items():e.append(event('checkpoint.verified',28_802_000_000_001,name=name,observed=True,evidence_ids=['s'+str(i) for i in indexes]))
        e.append(event('checkpoint.verified',28_802_000_000_001,name='history_restore',observed=True,evidence_ids=['restore']))
        result=hardware_metrics(e,run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)
        self.assertEqual(result['status'],'passed',result)
        for fault in ['missing_reference','no_transition','changed_values','stopped_guard','frozen_sources']:
            with self.subTest(fault=fault):
                bad=copy.deepcopy(e)
                if fault=='missing_reference':next(r for r in bad if r.get('name')=='ui_quit')['evidence_ids']=['nonexistent']
                elif fault=='no_transition':next(r for r in bad if r.get('id')=='s7')['ui_alive']=True
                elif fault=='changed_values':next(r for r in bad if r['stage']=='hardware.source_proof')['restored_sha256']='f'*64
                elif fault=='stopped_guard':next(r for r in bad if r.get('id')=='s200')['active']=False
                else:
                    for r in bad:
                        if r['stage']=='hardware.sample' and r['at_ns']>201_000_000_000:
                            r.update(analyzer_sources=30,receiver_sources=23,analyzer_source_id='a20',receiver_source_id='r20')
                        if r['stage']=='hardware.source_proof':r.update(sweeps=30,observations=23)
                self.assertEqual(hardware_metrics(bad,run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)['status'],'failed')

    def test_long_session_without_operator_and_source_proofs_is_not_passed(self):
        result=hardware_metrics([],run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)
        self.assertNotEqual(result['status'],'passed')
        self.assertIn('baseline',result['missing_checkpoints'])

    def test_caller_elapsed_time_cannot_replace_eight_hours_of_coverage_samples(self):
        from benchmarks.ori_bench.mac_acceptance.proof import CHECKPOINTS
        events=[dict(stage='hardware.identity',run_id=RUN,component_sha256=CORE,at_ns=1,mode='live',
                     analyzer_model='tinySA Ultra',receiver_model='AR-DV1',analyzer_firmware='test',receiver_firmware='test'),
                dict(stage='hardware.sample',run_id=RUN,component_sha256=CORE,at_ns=2,active=True),
                dict(stage='hardware.source_proof',run_id=RUN,component_sha256=CORE,at_ns=3,
                     values_preserved=True,sweeps=1,observations=1)]
        events += [dict(stage='checkpoint.verified',run_id=RUN,component_sha256=CORE,at_ns=4,
                        name=name,observed=True,evidence_ids=['original']) for name in CHECKPOINTS]
        result=hardware_metrics(events,run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)
        self.assertEqual(result['status'],'failed')
        self.assertIn('eight_hours_of_coverage_not_observed',result['errors'])

    def test_sparse_heartbeats_cannot_prove_eight_hours_of_continuity(self):
        samples=[dict(stage='hardware.sample',run_id=RUN,component_sha256=CORE,at_ns=1,
                      analyzer_sources=1,receiver_sources=1),
                 dict(stage='hardware.sample',run_id=RUN,component_sha256=CORE,at_ns=28_800_000_000_001,
                      analyzer_sources=2,receiver_sources=2)]
        result=hardware_metrics(samples,run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)
        self.assertIn('unobserved_coverage_interval',result['errors'])

    def test_spontaneous_scan_error_is_not_erased_by_a_later_recovery(self):
        events=[dict(stage='hardware.error',run_id=RUN,component_sha256=CORE,at_ns=1,
                     message='scan timeout'),
                dict(stage='hardware.sample',run_id=RUN,component_sha256=CORE,at_ns=2,
                     active=True,analyzer_sources=5,receiver_sources=3)]
        result=hardware_metrics(events,run_id=RUN,core_sha256=CORE,elapsed_seconds=28_800)
        self.assertEqual(result['status'],'failed')
        self.assertIn('scan timeout',str(result['errors']))
