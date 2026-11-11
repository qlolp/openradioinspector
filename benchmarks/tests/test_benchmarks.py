"""Contract tests use a controlled real HTTP/SQLite fixture, never an actual core."""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote, urlsplit
import wave

REPO = Path(__file__).resolve().parents[2]


class Fixture:
    """A minimal protocol peer, deliberately not evidence of core behavior."""
    def __init__(self, path, *, audio=True, reject=0, corrupt=False, summaries=True, audio_metadata_corrupt=False,
                 summary_corrupt=False, invalid_ack=False, drop_audio=False, guard_active=None):
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.executescript('CREATE TABLE sessions(id PRIMARY KEY,payload);'
            'CREATE TABLE measurements(id PRIMARY KEY,kind,payload);'
            'CREATE TABLE artifacts(id PRIMARY KEY,summary,payload BLOB);')
        self.lock=threading.RLock()
        self.audio,self.reject,self.corrupt,self.summaries=audio,reject,corrupt,summaries
        self.audio_metadata_corrupt=audio_metadata_corrupt
        self.summary_corrupt=summary_corrupt
        self.invalid_ack,self.drop_audio=invalid_ack,drop_audio
        self.time_corrupt=None
        self.guard_active=guard_active
        self.posts=[]

    def handler(self):
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,status,value):
                raw=json.dumps({'contract':'controlled-http-fixture','contract_version':1,'data':value},
                               allow_nan=False).encode()
                self.send_response(status)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                path=urlsplit(self.path).path
                pieces=[unquote(p) for p in path.strip('/').split('/')]
                with fixture.lock:
                    if path=='/api/v1/capabilities':
                        self.reply(200,{'persistent_archive':True,'features':{
                            'audio_segment_ingest':fixture.audio,'measurement_batch_ingest':True,
                            'measurement_batch_limit':100,'measurement_batch_max_request_bytes':1048576,
                            'session_archive_export_ndjson':True,'sweep_summaries':fixture.summaries,
                            'monitoring_guard':fixture.guard_active is not None}})
                    elif path=='/api/v1/monitoring-projects':
                        self.reply(200,{'items':[{'id':'fixture-project'}] if fixture.guard_active is not None else [],'next_cursor':None})
                    elif path=='/api/v1/monitoring-projects/fixture-project/status':
                        self.reply(200,{'active':fixture.guard_active,'receiver_busy':False})
                    elif path=='/api/v1/health':
                        count=fixture.db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0]
                        self.reply(200,{'last_event_id':count+1,'fixture':True})
                    elif path.endswith('/archive/export.ndjson'):
                        rows=fixture.db.execute('SELECT kind,payload FROM measurements ORDER BY id')
                        self.send_response(200)
                        self.send_header('Content-Type','application/x-ndjson')
                        self.end_headers()
                        self.wfile.write((json.dumps({'format':'openradioinspector-archive-ndjson',
                            'format_version':1,'session_id':pieces[3]})+'\n').encode())
                        for kind,payload in rows:
                            value=json.loads(payload)
                            if fixture.corrupt and kind=='sweep': value['levels'][0]+=1
                            if fixture.time_corrupt=='naive':value['interval']['started_at']=value['interval']['started_at'].removesuffix('Z')
                            if fixture.time_corrupt=='submicrosecond':value['interval']['started_at']=value['interval']['started_at'].replace('Z','7Z')
                            self.wfile.write((json.dumps({'kind':kind,'measurement':value})+'\n').encode())
                    elif len(pieces)==4 and pieces[:3]==['api','v1','sessions']:
                        row=fixture.db.execute('SELECT payload FROM sessions WHERE id=?',(pieces[3],)).fetchone()
                        self.reply(200,json.loads(row[0])) if row else self.reply(404,{'code':'absent'})
                    elif pieces[-1] in ('sweep-summaries','sweeps'):
                        rows=[json.loads(r[0]) for r in fixture.db.execute(
                            'SELECT payload FROM measurements WHERE kind="sweep" ORDER BY id DESC LIMIT 100')]
                        if pieces[-1]=='sweep-summaries':
                            if not fixture.summaries: self.reply(404,{}); return
                            rows=[{k:r[k] for k in ('id','session_id','stream_id','interval','level_unit')}
                                  | {'point_count':len(r['levels']),'min_frequency_hz':min(r['frequencies_hz']),
                                     'max_frequency_hz':max(r['frequencies_hz']),
                                     'peak_level':max(r['levels']),
                                     'peak_frequency_hz':r['frequencies_hz'][r['levels'].index(max(r['levels']))]} for r in rows]
                            if fixture.summary_corrupt and rows:rows[0]['point_count']+=1
                            self.reply(200,{'items':rows,'next_cursor':None})
                        else: self.reply(200,rows)
                    elif pieces[-1]=='artifacts':
                        self.reply(200,{'items':[json.loads(r[0]) for r in fixture.db.execute(
                            'SELECT summary FROM artifacts ORDER BY id')]})
                    elif pieces[-1]=='content':
                        row=fixture.db.execute('SELECT payload FROM artifacts WHERE id=?',(pieces[-2],)).fetchone()
                        if not row: self.reply(404,{}); return
                        self.send_response(200);self.send_header('Content-Length',str(len(row[0])))
                        self.end_headers();self.wfile.write(row[0])
                    else: self.reply(404,{})
            def do_POST(self):
                path=urlsplit(self.path).path
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.posts.append(path)
                    if path=='/api/v1/sessions':
                        if fixture.db.execute('SELECT 1 FROM sessions WHERE id=?',(value['id'],)).fetchone():
                            self.reply(409,{});return
                        fixture.db.execute('INSERT INTO sessions VALUES (?,?)',(value['id'],json.dumps(value)))
                        fixture.db.commit();self.reply(201,value)
                    elif path.endswith('/sweeps/batch') or path.endswith('/observations/batch'):
                        kind='sweep' if '/sweeps/' in path else 'observation'
                        items=value['items']
                        if (fixture.reject and len(fixture.posts)%fixture.reject==0) or any(
                            kind=='sweep' and not x['frequencies_hz'] for x in items):
                            self.reply(429,{'code':'controlled_rejection'});return
                        for item in items:
                            fixture.db.execute('INSERT INTO measurements VALUES (?,?,?)',
                                               (item['id'],kind,json.dumps(item)))
                        fixture.db.commit();self.reply(201,{'items':items,'count':0 if fixture.invalid_ack else len(items)})
                    elif path.endswith('/audio-segments'):
                        raw=base64.b64decode(value['data_base64'])
                        with wave.open(io.BytesIO(raw)) as audio: frames=audio.getnframes()
                        summary={'id':value['id'],'sha256':hashlib.sha256(raw).hexdigest(),
                            'size_bytes':len(raw),'metadata':{'sample_rate':48000,'frame_count':frames,
                            'frequency_hz':value['frequency_hz'],'mode':value['mode'],'interval':value['interval']}}
                        if fixture.audio_metadata_corrupt:summary['metadata']['frame_count']+=1
                        if not fixture.drop_audio:
                            fixture.db.execute('INSERT INTO artifacts VALUES (?,?,?)',(value['id'],json.dumps(summary),raw))
                        fixture.db.commit();self.reply(201,summary)
                    else: self.reply(404,{})
        return Handler


@contextmanager
def http_fixture(path, **kwargs):
    fixture=Fixture(path,**kwargs)
    server=ThreadingHTTPServer(('127.0.0.1',0),fixture.handler())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield fixture,f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join();fixture.db.close()


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def command(self,url,*,output=None,more=(),command='run'):
        return subprocess.run([sys.executable,'-m','benchmarks.ori_bench',command,
            '--base-url',url,'--output-dir',str(output or self.root/'results'),
            '--run-id','fixture-bench','--endpoint-label','controlled-http-fixture',
            '--duration-seconds','0.2','--audio-segment-seconds','0.1','--points','4',
            '--speed','100','--history-repeats','3',*more],cwd=REPO,capture_output=True,text=True,timeout=40)

    def test_default_target_counts_all_offered_and_complete_audio(self):
        from benchmarks.ori_bench.config import Config
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused')
        self.assertEqual(config.planned_counts(),{'sweep':18000,'observation':18000,'audio':180})
        self.assertEqual(config.planned_audio_frames,86400000)
        self.assertEqual(config.points,4096)
        self.assertEqual(config.speed,1)

    def test_generator_preserves_float_bits_hz_microseconds_and_raw_s_meter(self):
        from benchmarks.ori_bench.config import Config
        from benchmarks.ori_bench.source import sweep,observation
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused',run_id='fixture')
        value=sweep(config,0)
        self.assertEqual([struct.pack('>d',v).hex() for v in value['levels'][:4]],
            ['c059080000000000','8000000000000000','bff0000000000001','c040600000000000'])
        self.assertTrue(all(type(v) is int for v in value['frequencies_hz']))
        self.assertEqual(value['frequencies_hz'][:4],[100000001,100001001,100002001,100003001])
        self.assertEqual(value['interval']['started_at'],'2026-11-11T00:00:00.123456Z')
        self.assertFalse(value['settings']['generator_output'])
        obs=observation(config,0)
        self.assertEqual((obs['level'],obs['level_unit']),(137.0,'s_meter_count'))
        self.assertEqual(value,sweep(config,0))

    def test_wav_segments_preserve_continuous_exact_sample_sequence(self):
        from benchmarks.ori_bench.config import Config
        from benchmarks.ori_bench.source import audio_segment,pcm_chunks
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused',
                      duration_seconds=.25,audio_segment_seconds=.1)
        merged=bytearray()
        for index in range(3):
            value=audio_segment(config,index)
            raw=base64.b64decode(value['data_base64'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),value['sha256'])
            with wave.open(io.BytesIO(raw)) as audio:
                self.assertEqual((audio.getframerate(),audio.getnchannels(),audio.getsampwidth()),(48000,1,2))
                merged.extend(audio.readframes(audio.getnframes()))
        self.assertEqual(len(merged),24000)
        self.assertEqual(struct.unpack('<hhhh',merged[:8]),(-32751,-24832,-16913,-8994))
        self.assertEqual(struct.unpack('<h',merged[9600:9602])[0],-32431)
        self.assertEqual(bytes(merged),b''.join(pcm_chunks(0,12000)))

    def test_controlled_http_archive_integrity_is_not_a_core_acceptance_claim(self):
        with http_fixture(self.root/'peer.sqlite') as (fixture,url):
            process=self.command(url)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['counts']['sweep']['offered'],2)
            self.assertEqual(report['counts']['observation']['verified'],2)
            self.assertEqual(report['audio']['offered_frames'],9600)
            self.assertEqual(report['audio']['verified_frames'],9600)
            self.assertEqual(report['checks']['archive_integrity']['status'],'passed')
            self.assertEqual(report['checks']['render_latency']['status'],'unmeasured')
            self.assertEqual(report['checks']['pi_acceptance']['status'],'unmeasured')
            self.assertEqual(report['endpoint']['label'],'controlled-http-fixture')
            self.assertTrue(all('/workers' not in p and '/start' not in p for p in fixture.posts))
            self.assertEqual(list((self.root/'results').glob('*.tmp')),[])

    def test_missing_audio_capability_fails_without_creating_a_session(self):
        with http_fixture(self.root/'peer.sqlite',audio=False) as (fixture,url):
            process=self.command(url)
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['run_status'],'unsupported')
            self.assertEqual(fixture.posts,[])

    def test_rejections_and_injected_gaps_remain_in_offered_denominator(self):
        with http_fixture(self.root/'peer.sqlite') as (_,url):
            process=self.command(url,more=('--scenario','fault','--fault-kind','invalid-batch','--fault-every','1'))
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['counts']['sweep']['offered'],2)
            self.assertEqual(report['counts']['sweep']['rejected'],2)
            self.assertEqual(report['counts']['sweep']['verified'],0)
            self.assertEqual(report['checks']['complete_delivery']['status'],'failed')
            self.assertEqual(report['checks']['offered_accounting']['status'],'passed')
            self.assertEqual(process.returncode,0,process.stderr)

    def test_corrupt_export_is_a_failed_run(self):
        with http_fixture(self.root/'peer.sqlite',corrupt=True) as (_,url):
            process=self.command(url)
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['checks']['archive_integrity']['status'],'failed')

    def test_existing_output_or_session_is_never_overwritten(self):
        output=self.root/'existing';output.mkdir();(output/'precious').write_bytes(b'keep')
        with http_fixture(self.root/'peer.sqlite') as (fixture,url):
            process=self.command(url,output=output)
            self.assertNotEqual(process.returncode,0)
            self.assertEqual((output/'precious').read_bytes(),b'keep')
            self.assertEqual(fixture.posts,[])
            self.assertEqual(self.command(url).returncode,0)
            before=fixture.db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0]
            process=self.command(url,output=self.root/'second')
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0],before)

    def test_summary_capability_absence_is_reported_unmeasured(self):
        with http_fixture(self.root/'peer.sqlite',summaries=False) as (_,url):
            process=self.command(url)
            self.assertEqual(process.returncode,0,process.stderr)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['checks']['history_summary_p95']['status'],'unmeasured')
            self.assertGreater(report['latency']['history_full_payload_ms']['count'],0)

    def test_history_fixture_requires_explicit_million_opt_in(self):
        with http_fixture(self.root/'peer.sqlite') as (fixture,url):
            process=self.command(url,command='history',more=('--sweeps','1000000'))
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(fixture.posts,[])

    def test_small_streamed_history_fixture_verifies_every_source(self):
        with http_fixture(self.root/'peer.sqlite') as (_,url):
            process=self.command(url,command='history',more=('--sweeps','11','--batch-size','3'))
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['counts']['sweep']['offered'],11)
            self.assertEqual(report['counts']['sweep']['verified'],11)
            self.assertEqual(report['workload']['mode'],'history')

    def test_histogram_is_bounded_and_reports_conservative_quantiles(self):
        from benchmarks.ori_bench.metrics import Histogram
        histogram=Histogram()
        for _ in range(100000): histogram.add(12.5)
        report=histogram.report()
        self.assertEqual(report['count'],100000)
        self.assertGreaterEqual(report['p95_upper_ms'],12.5)
        self.assertLess(report['p95_upper_ms'],13)
        self.assertLessEqual(len(histogram.bins),1025)

    def test_source_metadata_corruption_fails_even_with_exact_wav_bytes(self):
        with http_fixture(self.root/'peer.sqlite',audio_metadata_corrupt=True) as (_,url):
            process=self.command(url)
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['checks']['archive_integrity']['status'],'failed')

    def test_overload_keeps_queue_drops_and_rejections_in_source_counts(self):
        with http_fixture(self.root/'peer.sqlite',reject=3) as (_,url):
            process=self.command(url,more=('--scenario','overload','--duration-seconds','0.05',
                '--audio-segment-seconds','0.05','--sweep-rate','1000','--observation-rate','1000',
                '--workers','1','--queue-capacity','1','--speed','1000'))
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['counts']['sweep']['offered'],50)
            self.assertEqual(report['counts']['observation']['offered'],50)
            self.assertGreater(sum(r['queue_dropped'] for r in report['counts'].values()),0)
            self.assertEqual(report['checks']['offered_accounting']['status'],'passed')
            self.assertEqual(report['checks']['complete_delivery']['status'],'failed')
            self.assertEqual(process.returncode,0,process.stderr)

    def test_small_fast_run_does_not_pass_the_million_sweep_history_target(self):
        with http_fixture(self.root/'peer.sqlite') as (_,url):
            self.assertEqual(self.command(url).returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['checks']['history_summary_p95']['status'],'unmeasured')
            self.assertGreater(report['latency']['history_summary_ms']['count'],0)

    def test_accelerated_seven_day_source_replay_is_separate_from_stress(self):
        from benchmarks.ori_bench.config import Config
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused',scenario='lifecycle',
            duration_seconds=604800,speed=86400)
        self.assertEqual(config.planned_counts(),{'sweep':168,'observation':168,'audio':168})
        self.assertEqual(config.planned_audio_frames,80640000)
        self.assertNotEqual(config.planned_audio_frames,604800*48000)

    @unittest.skipUnless(sys.platform=='linux','/proc sampling is Linux specific')
    def test_process_sampling_reports_missing_roots_as_unmeasured(self):
        from benchmarks.ori_bench.config import Config
        from benchmarks.ori_bench.metrics import Sampler
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused',pids={'core':2147483647})
        with Sampler(config) as sampler:pass
        report=sampler.report()
        self.assertEqual(report['core_process_tree_status'],'unmeasured')
        self.assertEqual(report['process_trees']['core']['status'],'unmeasured')
        self.assertGreater(report['process_trees']['harness']['peak_root_rss_bytes'],0)

    def test_output_inside_repository_is_rejected_before_any_http_mutation(self):
        with http_fixture(self.root/'peer.sqlite') as (fixture,url):
            output=REPO/'benchmarks'/'must-not-create-runtime-files'
            process=self.command(url,output=output)
            self.assertNotEqual(process.returncode,0)
            self.assertFalse(output.exists())
            self.assertEqual(fixture.posts,[])

    def test_same_host_render_observations_need_complete_client_coverage(self):
        from benchmarks.ori_bench.config import Config
        from benchmarks.ori_bench.ledger import Ledger
        from benchmarks.ori_bench.metrics import Histogram
        from benchmarks.ori_bench.observe import correlate
        from benchmarks.ori_bench.source import identifier
        config=Config(base_url='http://127.0.0.1:1',output_dir=self.root/'unused',duration_seconds=.1,
                      audio_segment_seconds=.1,run_id='fixture-bench')
        ledger=Ledger(self.root/'offered.sqlite');self.addCleanup(ledger.close)
        key=identifier(config,'sweep',0)
        ledger.offer(config,'sweep',0,time.monotonic_ns());ledger.status(key,'accepted',201);ledger.verified(key)
        offered=ledger.get(key)['offered_wall_ns']
        header={'kind':'observer_metadata','version':1,'clients':3,'clock':'unix-wall-ns',
            'host_sha256':hashlib.sha256(socket.gethostname().encode()).hexdigest(),
            'draw_contract':'data-sweep-id-after-draw'}
        events=self.root/'render.ndjson'
        rows=[header]+[{'kind':'render','id':key,'client':str(i),'rendered_wall_ns':str(offered+25_000_000)}
                       for i in range(3)]+[{'kind':'observer_summary','dropped':0,'records':3,'failed':False}]
        events.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        result=correlate(events,config,ledger,Histogram())
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['observed_unique_renders'],3)
        self.assertEqual(result['metric'],'offered-to-render conservative upper bound')
        self.assertFalse(result['committed_to_render_measured'])
        rows[-1]['dropped']=1
        events.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        # A new ledger avoids reusing already-recorded observation deduplication.
        ledger.db.execute('DELETE FROM rendered');ledger.db.commit()
        self.assertEqual(correlate(events,config,ledger,Histogram())['status'],'failed')

    @unittest.skipUnless(os.environ.get('ORI_BENCH_PLAYWRIGHT_TEST'),'Optional browser fixture test requires explicit opt-in')
    def test_playwright_observer_reads_only_after_draw_markers(self):
        class Page(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                value=b'''<canvas width="8" height="8"></canvas><script>
                const canvas=document.querySelector('canvas');let i=0;
                const timer=setInterval(()=>{canvas.getContext('2d').fillRect(0,0,8,8);
                  canvas.dataset.sweepId='fixture-bench-sweep-'+String(i++).padStart(9,'0');
                  if(i===3)clearInterval(timer);},50);</script>'''
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            output=self.root/'browser-render.ndjson'
            command=[os.environ.get('ORI_BENCH_NODE','node'),str(REPO/'benchmarks/observe-render.mjs'),
                '--url',f'http://127.0.0.1:{server.server_port}','--output',str(output),
                '--run-id','fixture-bench','--clients','3','--duration-seconds','.5']
            process=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            rows=[json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(rows[0]['clients'],3)
            renders=[row for row in rows if row['kind']=='render']
            self.assertGreaterEqual(len(renders),9)
            self.assertEqual({r['client'] for r in renders},{'0','1','2'})
            self.assertEqual(rows[-1]['dropped'],0)
        finally:server.shutdown();server.server_close();thread.join()

    def test_summary_source_metadata_must_match_the_original_sweep(self):
        with http_fixture(self.root/'peer.sqlite',summary_corrupt=True) as (_,url):
            process=self.command(url)
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['errors'][0]['code'],'history_invalid')

    def test_existing_ledger_open_cannot_change_an_unowned_sqlite_database(self):
        from benchmarks.ori_bench.ledger import Ledger
        path=self.root/'foreign.sqlite'
        db=sqlite3.connect(path);db.execute('PRAGMA user_version=1')
        db.execute('CREATE TABLE sessions(id TEXT)');db.commit();db.close()
        before=hashlib.sha256(path.read_bytes()).digest()
        with self.assertRaises(ValueError):Ledger(path,existing=True)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)
        self.assertFalse(Path(str(path)+'-wal').exists())

    def test_invalid_acknowledgements_are_not_expected_overload_rejections(self):
        with http_fixture(self.root/'peer.sqlite',invalid_ack=True) as (_,url):
            process=self.command(url,more=('--scenario','overload','--sweep-rate','10'))
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertGreater(report['counts']['sweep']['invalid_ack'],0)

    def test_acknowledged_audio_loss_fails_even_an_explicit_fault_scenario(self):
        with http_fixture(self.root/'peer.sqlite',drop_audio=True) as (_,url):
            process=self.command(url,more=('--scenario','overload','--sweep-rate','10'))
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['counts']['audio']['acknowledged'],2)
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(report['checks']['archive_integrity']['status'],'failed')

    def test_render_evidence_can_be_attached_only_to_owned_finished_report(self):
        with http_fixture(self.root/'peer.sqlite') as (_,url):
            self.assertEqual(self.command(url).returncode,0)
        root=self.root/'results'
        db=sqlite3.connect(root/'offers.sqlite')
        offers=db.execute("SELECT id,offered_wall_ns FROM offers WHERE kind='sweep'").fetchall();db.close()
        rows=[{'kind':'observer_metadata','version':1,'clients':3,'clock':'unix-wall-ns',
            'host_sha256':hashlib.sha256(socket.gethostname().encode()).hexdigest(),
            'draw_contract':'data-sweep-id-after-draw'}]
        for key,offered in offers:
            rows.extend({'kind':'render','id':key,'client':str(i),'rendered_wall_ns':str(offered+10_000_000)} for i in range(3))
        rows.append({'kind':'observer_summary','records':len(offers)*3,'dropped':0,'failed':False})
        events=self.root/'synthetic-observer-contract.ndjson'
        events.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        process=subprocess.run([sys.executable,'-m','benchmarks.ori_bench.observe','--report-dir',str(root),
            '--render-events',str(events)],cwd=REPO,capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        report=json.loads((root/'report.json').read_text())
        self.assertEqual(report['checks']['render_latency']['observed_unique_renders'],6)
        self.assertFalse(report['checks']['render_latency']['committed_to_render_measured'])
        self.assertFalse((root/'report.json.tmp').exists())

    def test_archive_time_zone_and_precision_cannot_be_silently_reinterpreted(self):
        for corruption in ('naive','submicrosecond'):
            with self.subTest(corruption=corruption),http_fixture(self.root/(corruption+'.sqlite')) as (fixture,url):
                fixture.time_corrupt=corruption
                process=self.command(url,output=self.root/corruption)
                self.assertNotEqual(process.returncode,0)
                report=json.loads((self.root/corruption/'report.json').read_text())
                self.assertEqual(report['checks']['archive_integrity']['status'],'failed')

    def test_advertised_armed_guard_blocks_load_without_changing_it(self):
        with http_fixture(self.root/'peer.sqlite',guard_active=True) as (fixture,url):
            process=self.command(url)
            self.assertNotEqual(process.returncode,0)
            report=json.loads((self.root/'results/report.json').read_text())
            self.assertEqual(report['errors'][0]['code'],'unsafe_target')
            self.assertEqual(fixture.posts,[])


if __name__=='__main__': unittest.main()
