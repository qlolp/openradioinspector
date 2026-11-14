"""Actual legacy ApplicationState/HTTP DTO oracle, no listener or devices."""
from __future__ import annotations

import os
for _key in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS","VECLIB_MAXIMUM_THREADS","ARROW_NUM_THREADS"):
    os.environ[_key]="1"

from datetime import datetime, timezone, timedelta
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from radioinspector import legacy_rpc
from radioinspector.jsonio import to_jsonable
from radioinspector.panorama import PanoramaStore, ScanSettings
from radioinspector.trace import Trace
from radioinspector.threshold import ThresholdLine
from radioinspector.web.server import ApplicationState, Handler
from tests.test_research_rpc import shared_io_capability, shared_io_total

ROOT=Path(__file__).resolve().parents[1]


def create_pan(path:Path):
    settings=ScanSettings(device='Прибор-é-é-😀',antenna='Ant/"\\',tract='raw',start_hz=100_000_000.125,
        stop_hz=100_000_002.625,step_hz=.25,bandwidth_hz=1.,points=11)
    frequencies=settings.start_hz+np.arange(11)*settings.step_hz
    with PanoramaStore(path,settings) as store:
        for index,offset in enumerate((0.,-2.,1.0000000000000002)):
            levels=np.array([-100.,-100.,-100.,-50.,-20.,-10.,-20.,-50.,-100.,-100.,-100.])+offset
            if index==2:levels[0]=-0.;levels[1]=-1e-300;levels[5]=-1.0000000000000002
            store.append(Trace(frequencies,levels,'dBm'),recorded_at=datetime(2026,11,11,12,0,index,123456,tzinfo=timezone(timedelta(hours=3))),
                latitude=42.+index if index<2 else None,longitude=45.+index if index<2 else None)
        store.save_threshold(ThresholdLine(np.array([-0.,.125,2.125]),np.array([-0.,1.0000000000000002,-1e-300]),1))
        store.save_threshold(ThresholdLine(np.array([100000000.125,100000002.625]),np.array([-80.,-70.]),2))
    with closing(sqlite3.connect(path)) as db,db:db.execute('UPDATE panorama SET id=? WHERE id=3',(9007199254740993,))


def source_rows(path:Path):
    with closing(sqlite3.connect(path)) as db,db:
        return [(table,db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()) for table in ('panorama_meta','panorama','signal_event','sqlite_sequence')]


class Capture:
    def __init__(self,state,path=''):
        self.state=state;self.path=path;self.runtime_bridge=None;self.response=None
    def _runtime_request(self,_method):return False
    def _send_json(self,value,status=200):self.response=(status,to_jsonable(value))
    def _get(self):return Handler._get(self)
    def _send_api_error(self,*_):raise AssertionError('v1 API is outside this oracle')


def direct_get(state,path):
    captured=Capture(state,path);Handler.do_GET(captured);return captured.response


def direct_threshold(state,payload):
    captured=Capture(state)
    try:Handler._dispatch_post(captured,'/api/threshold',payload)
    except (ValueError,TypeError,OverflowError,UnicodeError) as error:captured._send_json({'ok':False,'error':str(error)},400)
    return captured.response


def sealed_snapshot(source:Path,directory:Path):
    directory.mkdir(mode=0o700)
    destination=directory/'workspace.pan'
    with closing(sqlite3.connect(source)) as reader,closing(sqlite3.connect(destination)) as writer:reader.backup(writer)
    destination.chmod(0o600)
    nonce='owned-legacy-test-nonce'
    (directory/'snapshot.json').write_text(json.dumps({'version':1,'nonce':nonce,'panorama_file':'workspace.pan','signals_file':None}))
    (directory/'snapshot.json').chmod(0o600)
    return nonce


class LegacyRpcTests(unittest.TestCase):
    def test_generator_closes_all_real_sqlite_connections_before_returning(self):
        import importlib.util
        path=ROOT/'crates/ori-legacy/tests/generate_fixtures.py'
        spec=importlib.util.spec_from_file_location('owned_fixture_generator',path)
        generator=importlib.util.module_from_spec(spec);spec.loader.exec_module(generator)
        original=sqlite3.connect;connections=[]
        def connect(*args,**kwargs):
            db=original(*args,**kwargs);connections.append(db);return db
        try:
            with patch.object(sqlite3,'connect',connect):
                source,_=generator.create(self.root/'generator')
            self.assertTrue(source.is_file())
            self.assertTrue(connections)
            for db in connections:
                with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
        finally:
            for db in connections:db.close()

    def test_readonly_sqlite_uri_normalizes_only_windows_disk_namespace(self):
        from pathlib import PureWindowsPath
        from radioinspector.legacy_rpc import _readonly_sqlite_uri

        expected = 'file:///C:/private/%D1%82%D0%B5%D1%81%D1%82%20%23%3F.pan?mode=ro'
        for source in (r'C:\private\тест #?.pan', r'\\?\C:\private\тест #?.pan'):
            with self.subTest(source=source):
                self.assertEqual(_readonly_sqlite_uri(PureWindowsPath(source)), expected)
        source = self.root / 'private #?.pan'
        self.assertEqual(_readonly_sqlite_uri(source), source.as_uri() + '?mode=ro')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.path=self.root/'source-é-é-😀.pan';create_pan(self.path)
        self.app=ApplicationState(str(self.path),str(self.root));self.addCleanup(self.app.close)
        self.snapshot=self.root/'private';self.nonce=sealed_snapshot(self.path,self.snapshot)
        self.rows=source_rows(self.path)
    def request(self,method,parameters=None):
        return {'version':1,'request_id':'legacy-test','operation':'analysis','method':'legacy_workspace','action':method,
            'snapshot_directory':str(self.snapshot),'snapshot_nonce':self.nonce,'database':str(self.path),
            'source_version':self.app.version,'state':{'threshold':to_jsonable(self.app.threshold.as_dict()),'signals':self.app.signals.to_rows()},
            'parameters':parameters or {},'legacy_limits':legacy_rpc.DEFAULT_LEGACY_LIMITS}
    def call(self,request):return legacy_rpc.process_request(json.dumps(request,allow_nan=False).encode())
    def assert_source_unchanged(self):self.assertEqual(source_rows(self.path),self.rows)
    def test_all_queries_match_actual_legacy_http_dtos(self):
        for action,parameters,path in (
            ('state',{'include_waterfall':True},'/api/state'),('signals',{},'/api/signals'),
            ('coverage',{'rows':3,'cols':4},'/api/coverage?rows=3&cols=4'),
            ('occupancy',{},'/api/occupancy'),('occupancy',{'threshold':-30.},'/api/occupancy?threshold=-30'),
            ('efficiency',{},'/api/efficiency'),('keywords',{},'/api/keywords'),
            ('measurements',{'start':100_000_000.625,'stop':100_000_002.125,'norm':1.},'/api/measure?start=100000000.625&stop=100000002.125&norm=1')):
            with self.subTest(action=action,parameters=parameters):
                response=self.call(self.request(action,parameters));self.assertTrue(response['ok'],response)
                self.assertEqual(response['result']['reply'],direct_get(self.app,path)[1]);self.assert_source_unchanged()
    def test_threshold_uses_actual_handler_and_mutates_only_disposable_copy(self):
        for payload in ({'offset_db':3.5},{'offset_db':4.,'frequencies':[100_000_000.125,100_000_002.625],'levels':[-0.,-45.]},{}):
            with self.subTest(payload=payload):
                before=self.app.version;request=self.request('threshold',payload)
                with closing(sqlite3.connect(self.path)) as db,db:before_lines=db.execute('SELECT * FROM threshold_line ORDER BY line_index').fetchall()
                response=self.call(request)
                with closing(sqlite3.connect(self.path)) as db,db:self.assertEqual(db.execute('SELECT * FROM threshold_line ORDER BY line_index').fetchall(),before_lines)
                expected=direct_threshold(self.app,payload)
                self.assertTrue(response['ok'],response);self.assertEqual(response['result']['reply'],expected[1])
                self.assertEqual(response['result']['state'],to_jsonable(self.app.snapshot()))
                self.assertEqual(response['result']['state']['version'],before+1)
                # The direct oracle deliberately changes source threshold_line;
                # scientific bridge must leave every original raw row untouched.
                self.assert_source_unchanged()
    def test_invalid_requests_duplicate_json_and_bad_threshold_preserve_source(self):
        for raw in (b'{',b'[]',b'{"version":1,"version":1}',b'{"bad":NaN}',b'\xff'):
            self.assertFalse(legacy_rpc.process_request(raw)['ok']);self.assert_source_unchanged()
        for payload in ({'offset_db':'bad'},{'frequencies':[1,1],'levels':[-80,-70]},{'frequencies':[1,2],'levels':[1]},{'frequencies':[1,2],'levels':[[1,2],[3,4]]}):
            request=self.request('threshold',payload);before=self.app.version
            actual=self.call(request);expected=direct_threshold(self.app,payload)
            self.assertFalse(actual['ok']);self.assertEqual(actual['error']['http_status'],expected[0]);self.assertEqual(actual['error']['api_message'],expected[1]['error'])
            self.assertEqual(self.app.version,before);self.assert_source_unchanged()
    def test_private_snapshot_and_method_allowlist_are_enforced(self):
        for key,value in (('action','database.delete'),('method','SessionService'),('snapshot_directory',str(self.root)),('snapshot_nonce','wrong')):
            request=self.request('state');request[key]=value;self.assertFalse(self.call(request)['ok']);self.assert_source_unchanged()
        request=self.request('state');request['sql']='DROP TABLE panorama';self.assertFalse(self.call(request)['ok']);self.assert_source_unchanged()
    def test_no_database_state_queries_and_threshold_error(self):
        state=ApplicationState();self.addCleanup(state.close)
        for action,path in (('state','/api/state'),('signals','/api/signals'),('occupancy','/api/occupancy'),('efficiency','/api/efficiency'),('coverage','/api/coverage'),('measurements','/api/measure?start=1&stop=2')):
            request={'version':1,'request_id':'empty','operation':'analysis','method':'legacy_workspace','action':action,'source_version':0,
                'database':None,'state':{'threshold':None,'signals':[]},'parameters':{'start':1.,'stop':2.} if action=='measurements' else {},'legacy_limits':legacy_rpc.DEFAULT_LEGACY_LIMITS}
            response=self.call(request);self.assertTrue(response['ok'],response);self.assertEqual(response['result']['reply'],direct_get(state,path)[1])
        request['action']='threshold';request['parameters']={'offset_db':1}
        response=self.call(request);self.assertFalse(response['ok']);self.assertEqual(response['error']['api_message'],'нет активной линии порога')
    def test_frame_and_source_budgets_and_eof_are_bounded(self):
        request=self.request('state');request['legacy_limits']={**legacy_rpc.DEFAULT_LEGACY_LIMITS,'max_total_points':2}
        response=self.call(request);self.assertFalse(response['ok']);self.assertEqual(response['error']['code'],'resource_limit');self.assert_source_unchanged()
        self.assertFalse(legacy_rpc.process_request(b' '* (legacy_rpc.MAX_REQUEST_BYTES+1))['ok'])
        output=io.BytesIO();self.assertEqual(legacy_rpc.run(io.BytesIO(b''),output),1);self.assertFalse(json.loads(output.getvalue())['ok'])
    def test_threshold_control_has_its_own_64k_limit_with_large_source_envelope(self):
        request=self.request('threshold',{'offset_db':1,'ignored':'x'*65536})
        response=self.call(request);self.assertFalse(response['ok']);self.assertEqual(response['error']['code'],'resource_limit');self.assert_source_unchanged()
    def test_snapshot_seal_version_is_an_integer_and_nonce_is_required(self):
        manifest=self.snapshot/'snapshot.json';value=json.loads(manifest.read_text());value['version']=True;manifest.write_text(json.dumps(value))
        self.assertFalse(self.call(self.request('state'))['ok']);self.assert_source_unchanged()
    def test_threshold_rejects_cascading_foreign_keys_in_private_snapshot(self):
        with closing(sqlite3.connect(self.snapshot/'workspace.pan')) as db,db:
            db.executescript('CREATE UNIQUE INDEX threshold_levels_unique ON threshold_line(levels);CREATE TABLE external_raw_history(id INTEGER PRIMARY KEY,levels BLOB REFERENCES threshold_line(levels) ON UPDATE CASCADE);')
        response=self.call(self.request('threshold',{'offset_db':3.5}))
        self.assertFalse(response['ok'])
        self.assertIn('custom foreign-key relations',response['error']['api_message'])
        self.assert_source_unchanged()

    def test_shared_capability_is_optional_private_and_counts_bounded_copy_io(self):
        capability,ledger=shared_io_capability(self.root)
        before=hashlib.sha256(self.path.read_bytes()).digest()
        for action,parameters in (
            ('load',{}),('state',{}),('signals',{}),('coverage',{'rows':3,'cols':4}),
            ('occupancy',{}),('efficiency',{}),('keywords',{}),
            ('measurements',{'start':100_000_000.625,'stop':100_000_002.125,'norm':1.}),
            ('threshold',{'offset_db':3.5})):
            ordinary=self.request(action,parameters)
            expected=self.call(ordinary)
            old_total=shared_io_total(ledger)
            actual=self.call(dict(ordinary,shared_io=capability))
            with self.subTest(action=action):
                if os.name!='posix':
                    self.assertFalse(actual['ok']);self.assertEqual(actual['error']['code'],'shared_io_unavailable')
                    continue
                self.assertTrue(actual['ok'],actual)
                receipt=actual.pop('shared_io_receipt')
                self.assertEqual(legacy_rpc._encode(actual),legacy_rpc._encode(expected))
                self.assertNotIn('shared_io_receipt',expected)
                self.assertEqual(receipt['version'],1)
                self.assertEqual(receipt['accounted_bytes'],shared_io_total(ledger)-old_total)
                self.assertGreaterEqual(receipt['accounted_bytes'],2*(self.snapshot/'workspace.pan').stat().st_size)
                self.assertGreater(receipt['charges'],0)
                self.assertTrue(0<receipt['max_charge_bytes']<=65536)
                self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),before)
                self.assert_source_unchanged()

    def test_unsafe_ledger_fails_before_temp_copy_or_science_without_source_changes(self):
        capability,ledger=shared_io_capability(self.root)
        ledger.chmod(0o644)
        before=hashlib.sha256(self.path.read_bytes()).digest()
        with patch.object(legacy_rpc,'_execute',side_effect=AssertionError('dispatch before capability validation')) as execute, \
             patch.object(legacy_rpc.tempfile,'TemporaryDirectory',side_effect=AssertionError('temporary mutation')) as temporary:
            response=self.call(dict(self.request('state'),shared_io=capability))
        self.assertFalse(response['ok'])
        self.assertEqual(response['error']['code'],'shared_io_unavailable')
        self.assertNotIn('shared_io_receipt',response)
        self.assertNotIn(str(ledger),json.dumps(response))
        self.assertNotIn(capability['nonce'],json.dumps(response))
        execute.assert_not_called();temporary.assert_not_called()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),before)

    def test_shared_copy_and_sidecar_match_exact_oracle_and_charge_only_bounded_units(self):
        database=self.snapshot/'workspace.pan'
        with closing(sqlite3.connect(database)) as db,db:
            db.execute('CREATE TABLE preserved_padding(payload BLOB)')
            db.execute('INSERT INTO preserved_padding VALUES(?)',(b'x'*(192*1024),))
        sidecar=self.snapshot/'signals.json'
        sidecar.write_text(json.dumps({'signals':[],'cycles':0,'metadata':{'é':1,'e\u0301':2},'padding':'x'*(130*1024)},ensure_ascii=False))
        sidecar.chmod(0o600)
        manifest=self.snapshot/'snapshot.json'
        value=json.loads(manifest.read_text());value['signals_file']='signals.json';manifest.write_text(json.dumps(value))
        expected=self.call(self.request('load'))
        source_before=hashlib.sha256(database.read_bytes()).digest()
        capability,ledger=shared_io_capability(self.root)
        from radioinspector.worker_io import SharedIoBudget
        original=SharedIoBudget.account;charges=[]
        def observe(owner,size,**kwargs):
            charges.append(size)
            return original(owner,size,**kwargs)
        with patch.object(SharedIoBudget,'account',observe):
            actual=self.call(dict(self.request('load'),shared_io=capability))
        if os.name!='posix':
            self.assertFalse(actual['ok']);self.assertEqual(actual['error']['code'],'shared_io_unavailable')
            self.assert_source_unchanged();return
        self.assertTrue(actual['ok'],actual)
        receipt=actual.pop('shared_io_receipt')
        self.assertEqual(legacy_rpc._encode(actual),legacy_rpc._encode(expected))
        self.assertEqual(receipt['accounted_bytes'],2*database.stat().st_size+3*sidecar.stat().st_size+manifest.stat().st_size)
        self.assertEqual(receipt['accounted_bytes'],shared_io_total(ledger))
        self.assertEqual(receipt['charges'],len(charges))
        self.assertEqual(receipt['max_charge_bytes'],max(charges))
        self.assertTrue(all(0<count<=65536 for count in charges))
        self.assertEqual(hashlib.sha256(database.read_bytes()).digest(),source_before)
        self.assertEqual(list(self.snapshot.glob('ori-legacy-science-*')),[])
        self.assert_source_unchanged()

    def test_denied_mid_copy_never_acknowledges_and_removes_only_owned_work(self):
        with closing(sqlite3.connect(self.snapshot/'workspace.pan')) as db,db:
            db.execute('CREATE TABLE preserved_padding(payload BLOB)')
            db.execute('INSERT INTO preserved_padding VALUES(?)',(b'x'*(192*1024),))
        capability,ledger=shared_io_capability(self.root)
        before=hashlib.sha256((self.snapshot/'workspace.pan').read_bytes()).digest()
        from radioinspector.worker_io import SharedIoBudget
        original=SharedIoBudget.account;calls=0
        def revoke(owner,size,**kwargs):
            nonlocal calls
            original(owner,size,**kwargs);calls+=1
            if calls==3:
                raw=bytearray(ledger.read_bytes());raw[8:24]=b'\xff'*16;ledger.write_bytes(raw)
        with patch.object(SharedIoBudget,'account',revoke):
            response=self.call(dict(self.request('state'),shared_io=capability))
        self.assertFalse(response['ok'])
        self.assertEqual(response['error']['code'],'shared_io_unavailable')
        self.assertNotIn('shared_io_receipt',response)
        self.assertEqual(list(self.snapshot.glob('ori-legacy-science-*')),[])
        self.assertEqual(hashlib.sha256((self.snapshot/'workspace.pan').read_bytes()).digest(),before)
        self.assert_source_unchanged()

    def test_parent_cancellation_reaps_actual_blocked_child_before_owned_tree_cleanup(self):
        capability,ledger=shared_io_capability(self.root,debt_seconds=5)
        if os.name!='posix':
            response=self.call(dict(self.request('state'),shared_io=capability))
            self.assertFalse(response['ok']);self.assertEqual(response['error']['code'],'shared_io_unavailable')
            self.assert_source_unchanged();return
        before=hashlib.sha256(self.path.read_bytes()).digest()
        bootstrap=f"import sys,runpy;sys.path.insert(0,{str(ROOT)!r});runpy.run_module('radioinspector.legacy_rpc',run_name='__main__')"
        child=subprocess.Popen([sys.executable,'-I','-c',bootstrap],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=ROOT)
        try:
            child.stdin.write(json.dumps(dict(self.request('state'),shared_io=capability)).encode());child.stdin.close();child.stdin=None
            deadline=time.monotonic()+4
            while shared_io_total(ledger)==0 and child.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            self.assertGreater(shared_io_total(ledger),0)
            self.assertIsNone(child.poll())
            self.assertEqual(len(list(self.snapshot.glob('ori-legacy-science-*'))),1)
            child.terminate();stdout,stderr=child.communicate(timeout=5)
            self.assertNotEqual(child.returncode,0)
            self.assertEqual(stdout,b'')
            self.assertEqual(stderr,b'')
            self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),before)
            # The parent owns both stages, and cleans them only after actual JOIN.
            import shutil
            shutil.rmtree(self.snapshot)
            self.assertFalse(self.snapshot.exists())
        finally:
            if child.poll() is None:child.kill();child.wait()

    def test_private_receipt_is_bounded_and_no_database_has_zero_file_io(self):
        capability,_=shared_io_capability(self.root)
        request=self.request('state')
        expected=self.call(request)
        request['legacy_limits']={**legacy_rpc.DEFAULT_LEGACY_LIMITS,'max_result_bytes':len(legacy_rpc._encode(expected))}
        actual=self.call(dict(request,shared_io=capability))
        self.assertFalse(actual['ok']);self.assertNotIn('shared_io_receipt',actual)
        self.assertEqual(actual['error']['code'],'resource_limit' if os.name=='posix' else 'shared_io_unavailable')
        empty={'version':1,'request_id':'empty','operation':'analysis','method':'legacy_workspace','action':'keywords',
               'source_version':0,'database':None,'state':{'threshold':None,'signals':[]},'parameters':{},'legacy_limits':{}}
        actual=self.call(dict(empty,shared_io=capability))
        if os.name=='posix':
            self.assertTrue(actual['ok'],actual)
            self.assertEqual(actual['shared_io_receipt'],{'version':1,'accounted_bytes':0,'charges':0,'max_charge_bytes':0})
        else:self.assertEqual(actual['error']['code'],'shared_io_unavailable')


if __name__=='__main__':unittest.main()
