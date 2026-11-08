import io
import json
import os
import unittest
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import monotonic
from unittest.mock import Mock
from tests.test_windows_sweep_threshold import panel as base_panel
from radioinspector.contracts import api_payload
from radioinspector.windows.client import PlatformClient, PlatformClientError, MAX_RESPONSE_BYTES
from radioinspector.windows.sweep_context import SweepContextPanel, validate_context, validate_counts, result_text


def source(index=3, bins=3):
    return dict(id=f'sweep-{index}', stream_id='s', level_unit='dBm', frequencies_hz=[100+i for i in range(bins)], levels=[-100+index]*bins,
        settings={'rbw_hz':1000,'flag':True,'sequence':index}, interval={'started_at':(datetime(2026,11,8,tzinfo=timezone.utc)+timedelta(seconds=index,microseconds=1)).isoformat().replace('+00:00','Z'),'ended_at':(datetime(2026,11,8,tzinfo=timezone.utc)+timedelta(seconds=index,microseconds=2)).isoformat().replace('+00:00','Z')})


def response(before=2, after=2, bins=3):
    s=source(bins=bins)
    return dict(session_id='session',stream_id='s',anchor_id=s['id'],order='started_at_then_id',requested={'before':before,'after':after},available={'before':before,'after':after},missing={'before':0,'after':0},complete=True,frequencies_hz=s['frequencies_hz'],level_unit='dBm',points=bins,
        items=[dict(offset=i,**{k:v for k,v in source(3+i,bins).items() if k not in ('stream_id','frequencies_hz')}) for i in range(-before,after+1)])


def panel():
    base=base_panel(); p=SweepContextPanel.__new__(SweepContextPanel);p.__dict__.update(base.__dict__)
    for key in ('anchor','before','after','anchors','selected','selection'):setattr(p,key,Mock())
    p.anchor.get.return_value='sweep-3';p.before.get.return_value=p.after.get.return_value='2';p.selection.current.return_value=2
    p.app.snapshot.sweeps=[source(i) for i in range(1,6)]
    return p


class ValidationTests(unittest.TestCase):
    def check(self,r,s=None,before=2,after=2):return validate_context(r,('session','s'),s or source(),before,after)
    def test_valid_metadata_and_edges(self):
        self.check(response());text=result_text(response())
        for token in ('Anchor sweep-3','missing 0/0','.000001Z','.000002Z','"sequence": 5','+2 · sweep-5'):self.assertIn(token,text)
        self.check(response(0,0),before=0,after=0)
        r=response(0,1);r['requested']={'before':2,'after':2};r['missing']={'before':2,'after':1};r['complete']=False;self.check(r)
    def test_scope_ids_unbounded_and_source_session_checked(self):
        session,stream='session-'+'я'*200,'stream-'+'𐀀'*200
        r=response();r.update(session_id=session,stream_id=stream)
        src=source();src.update(session_id=session,stream_id=stream)
        validate_context(r,(session,stream),src,2,2)
        for invalid in ('wrong',None,True):
            src['session_id']=invalid
            with self.assertRaises(ValueError):validate_context(r,(session,stream),src,2,2)
        for invalid in ('', ' ', '\ud800'):
            src.update(session_id=invalid,stream_id=stream);r['session_id']=invalid
            with self.assertRaises(ValueError):validate_context(r,(invalid,stream),src,2,2)

    def test_equivalent_utc_anchor_echo_preserves_microseconds(self):
        r=response();r['items'][2]['interval']={k:v.replace('Z','+00:00') for k,v in r['items'][2]['interval'].items()}
        self.check(r)
        r['items'][2]['interval']['started_at']='2026-11-08T00:00:03.000002+00:00'
        with self.assertRaises(ValueError):self.check(r)

    def test_adversarial_metadata_and_items(self):
        changes=[lambda r:r.update(session_id='bad'),lambda r:r.update(stream_id='bad'),lambda r:r.update(anchor_id='bad'),lambda r:r.update(order='id'),lambda r:r.update(points=True),lambda r:r.update(level_unit='S'),lambda r:r.update(complete=1),lambda r:r['requested'].update(before=True),lambda r:r['available'].update(before=1),lambda r:r['missing'].update(after=1),lambda r:r['items'].reverse(),lambda r:r['items'].pop(),lambda r:r['items'][0].update(offset=True),lambda r:r['items'][0].update(id='sweep-2'),lambda r:r['items'][0].update(levels=[float('nan')]*3),lambda r:r['items'][0]['settings'].update(flag=1),lambda r:r['items'][2]['settings'].update(sequence=99),lambda r:r['items'][2]['interval'].update(started_at='2026-11-08T00:00:03.000002Z'),lambda r:r['items'][0]['interval'].update(started_at='2026-11-08T00:00:01.0000001Z'),lambda r:r.update(frequencies_hz=[100.,101,102])]
        for change in changes:
            r=response();change(r)
            with self.subTest(change=change),self.assertRaises(ValueError):self.check(r)
        for value in (-1,17,True,2.0,'2'):
            with self.assertRaises(ValueError):validate_counts(value,2)
    def test_order_utf8_microseconds_and_source_limits(self):
        r=response(0,1);s=source();r['items'][1]['interval']=deepcopy(s['interval']);r['items'][1]['id']='\U00010000';s['id']='\ue000';r['items'][0]['id']=r['anchor_id']=s['id'];self.check(r,s,0,1)
        r['items'][1]['id']='a'
        with self.assertRaises(ValueError):self.check(r,s,0,1)
        for changes in ({'id':'\ud800'},{'id':'x'*129},{'settings':{'sequence':1}},{'frequencies_hz':[2**53]*3},{'levels':[True]*3}):
            s=source();s.update(changes)
            with self.assertRaises(ValueError):self.check(response(),s)
    def test_total_point_boundary_and_all_33_rows(self):
        self.check(response(3,4,16384),source(bins=16384),3,4)
        with self.assertRaises(ValueError):self.check(response(4,4,16384),source(bins=16384),4,4)
        r=response(16,16);self.check(r,before=16,after=16)
        text=result_text(r);self.assertEqual(text.count('Settings:'),33)

    def test_fullsize_and_single_duplicate_bins(self):
        r=response(2,2,16384);self.check(r,source(bins=16384))
        p=panel();p.data=r;p.draw();self.assertEqual(len(p.canvas.create_line.call_args.args),32768)
        for bins in (1,3):
            r=response(0,0,bins);s=source(bins=bins);s['frequencies_hz']=[100]*bins;r['frequencies_hz']=[100]*bins;self.check(r,s,0,0)
            p=panel();p.data=r;p.selection.current.return_value=0;p.draw();p.canvas.create_oval.assert_called_once()
        r=response(2,2,16384);r['items']*=2
        with self.assertRaises(ValueError):self.check(r,source(bins=16384))


class LifecycleTests(unittest.TestCase):
    def test_foreign_session_sweeps_not_choices_or_requests(self):
        p=panel();p.app.snapshot.sweeps[2]['session_id']='foreign'
        p.app.snapshot.sweeps[0]['session_id']='session'
        self.assertEqual([s['id'] for s in p.sources()],['sweep-1','sweep-2','sweep-4','sweep-5'])
        p.analyze();p.executor.submit.assert_not_called()
        session,stream='session'+'x'*200,'stream'+'y'*200
        p.app.session_id=session;p.app.selected_stream_id=lambda:stream
        p.app.snapshot.session={'id':session};p.app.snapshot.spectrum_stream_id=stream
        src=source();src.update(session_id=session,stream_id=stream);p.app.snapshot.sweeps=[src]
        self.assertEqual(p.sources(),[src])
        p.analyze();p.executor.submit.assert_called_once_with(p.app.client.sweep_context,session,'sweep-3',2,2)

    def test_retained_chosen_source_reload_after_polling_and_count_edit(self):
        p=panel();p.retained_info=Mock();p.analyze();p.future.set_result(response());p.finish(p.generation)
        p.app.snapshot.sweeps=[];p.update();self.assertEqual(p.chosen_source,source())
        self.assertIn('sweep-3',p.anchors.configure.call_args.kwargs['values'])
        self.assertIn('вне текущей страницы',p.retained_info.set.call_args.args[0])
        p.before.get.return_value='1';p.after.get.return_value='0';p.clear();p.analyze()
        p.executor.submit.assert_called_with(p.app.client.sweep_context,'session','sweep-3',1,0)
        p.future.set_result(response(1,0));p.finish(p.generation);self.assertEqual(p.data,response(1,0))
        p.clear();self.assertEqual(p.chosen_source,source())
        p.anchor.get.return_value='sweep-2';p.invalidate_choice();p.analyze();self.assertIsNone(p.chosen_source)
        self.assertEqual(p.executor.submit.call_count,2)

    def test_retained_source_clears_on_server_edit(self):
        for rolled in (False,True):
            p=panel();p.update();self.assertIsNotNone(p.chosen_source)
            if rolled:p.app.snapshot.sweeps=[]
            p.disconnect('server_var','','write');p.update();p.analyze()
            self.assertIsNone(p.chosen_source);p.executor.submit.assert_not_called()
            self.assertEqual(p.sources(),[]);p.button.configure.assert_called_with(state='disabled')

    def test_frozen_explicit_anchor_not_latest(self):
        p=panel();p.update();p.executor.submit.assert_not_called();p.analyze();p.analyze()
        p.executor.submit.assert_called_once_with(p.app.client.sweep_context,'session','sweep-3',2,2)
        p.app.snapshot.sweeps[2]['settings']['sequence']=999;p.app.snapshot.sweeps=[]
        p.future.set_result(response());p.finish(p.generation);self.assertEqual(p.data,response());self.assertEqual(p.source['settings']['sequence'],3)
        p.update();self.assertEqual(p.data,response());self.assertEqual(p.executor.submit.call_count,1)
    def test_interruption_singleflight_and_retry(self):
        for action in ('clear','close','stream','session','server','disconnect','timeout'):
            p=panel();p.analyze();old=p.future;old.set_running_or_notify_cancel();g=p.generation
            if action in ('clear','close'):getattr(p,action)()
            if action=='stream':p.app.selected_stream_id=lambda:'other'
            if action=='session':p.app.session_id='other'
            if action=='server':p.app.client=Mock()
            if action=='disconnect':p.app.client=None
            if action=='timeout':p.deadline=monotonic()-1
            p.finish(g);p.analyze();self.assertEqual(p.executor.submit.call_count,1)
            old.set_result(response());p.finish(g);self.assertIsNone(p.data)
        p=panel();p.analyze();old=p.future;old.set_running_or_notify_cancel();p.clear();p.analyze();self.assertEqual(p.executor.submit.call_count,1)
        old.set_result(response());p.analyze();self.assertEqual(p.executor.submit.call_count,2);p.future.set_result(response());p.finish(p.generation);self.assertIsNotNone(p.data)
    def test_bad_reply_and_status_isolated(self):
        for status in (400,404,409,413,500):
            p=panel();p.analyze();p.future.set_exception(PlatformClientError('bad',status=status));p.finish(p.generation);self.assertIsNone(p.data)
            p.analyze();self.assertEqual(p.executor.submit.call_count,2)
    def test_actual_refresh_failure_clears_running_and_completed(self):
        import queue
        from concurrent.futures import Future
        from radioinspector.windows.app import WindowsApp
        for running in (False,True):
            for page_rolled in (False,True):
                with self.subTest(running=running,page_rolled=page_rolled):
                    p=panel();p.analyze()
                    if running:
                        old=p.future;old.set_running_or_notify_cancel();generation=p.generation
                    else:
                        p.future.set_result(response());p.finish(p.generation)
                    self.assertIsNotNone(p.chosen_source)
                    if page_rolled:p.app.snapshot.sweeps=[]
                    app=WindowsApp.__new__(WindowsApp);app._closed=False;app._revision=7;app._messages=queue.Queue()
                    app.root=Mock();app.connection_var=Mock();app._show_archive_capacity=Mock();app._render_controls=Mock()
                    app.sweep_context_panel=p;app._refresh_running=True
                    failed=Future();failed.set_exception(PlatformClientError('offline',code='connection_error'))
                    app._messages.put(('snapshot',failed,7,''));app._drain_messages()
                    self.assertIsNone(p.data);self.assertIsNone(p.chosen_source)
                    p.anchor.set.assert_called_with('');p.anchors.configure.assert_called_with(values=[])
                    if running:
                        old.set_result(response());p.finish(generation);self.assertIsNone(p.data)
                    p.clear();p.button.configure.assert_called_with(state='disabled')
                    p.update();p.analyze();self.assertIsNone(p.chosen_source)
                    self.assertEqual(p.sources(),[]);self.assertEqual(p.executor.submit.call_count,1)
                    # Neither a scope change nor reappearing old page grants reconnection.
                    p.app.client=Mock();p.app.snapshot.sweeps=[source()];p.update();p.analyze()
                    self.assertIsNone(p.chosen_source);self.assertEqual(p.executor.submit.call_count,1)
                    fresh=Future();fresh.set_result(p.app.snapshot)
                    app._apply_snapshot=Mock(side_effect=lambda snapshot:setattr(p.app,'snapshot',snapshot))
                    app._messages.put(('snapshot',fresh,7,''));app._drain_messages()
                    app._apply_snapshot.assert_called_once_with(p.app.snapshot)
                    p.anchor.get.return_value='sweep-3';p.choose_anchor();p.analyze()
                    self.assertEqual(p.executor.submit.call_count,2)
                    app._messages.put(('snapshot',fresh,7,''));app._drain_messages()
                    p.button.configure.assert_called_with(state='disabled')
                    p.analyze();self.assertEqual(p.executor.submit.call_count,2)
                    p.future.set_result(response());p.finish(p.generation);self.assertIsNotNone(p.data)

    def test_select_each_context_spectrum(self):
        p=panel();p.data=response()
        for index,item in enumerate(p.data['items']):
            p.selection.current.return_value=index;p.draw();self.assertIn(item['id'],p.canvas.create_text.call_args.kwargs['text'])
            self.assertEqual(len(p.canvas.create_line.call_args.args),6)


class HTTPTests(unittest.TestCase):
    def client(self,raw,status=200):
        stream=io.BytesIO(raw);stream.status=status;opener=Mock();opener.open.return_value=stream
        return PlatformClient('http://localhost:56039',opener=opener),opener
    def test_exact_post_and_status_cap(self):
        client,opener=self.client(json.dumps(api_payload(response())).encode());self.assertEqual(client.sweep_context('session','sweep-3'),response())
        request=opener.open.call_args.args[0];self.assertEqual(request.method,'POST');self.assertTrue(request.full_url.endswith('/sessions/session/sweep-context'));self.assertEqual(json.loads(request.data),{'sweep_id':'sweep-3','before':2,'after':2})
        for status in (201,202,204,400,404,409,413,500):
            client,_=self.client(json.dumps(api_payload(response())).encode(),status)
            with self.assertRaises(PlatformClientError):client.sweep_context('session','sweep-3')
        for status in (200,202,400,500):
            client,_=self.client(b'x'*(MAX_RESPONSE_BYTES+1),status)
            with self.assertRaises(PlatformClientError):client.sweep_context('session','sweep-3')
        for count in (True,17,-1,2.0):
            client,opener=self.client(b'')
            with self.assertRaises(ValueError):client.sweep_context('session','sweep-3',count,2)
            opener.open.assert_not_called()
    def test_real_synthetic_http_harness(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                seen.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                raw=json.dumps(api_payload(response())).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=PlatformClient(f'http://127.0.0.1:{server.server_port}')
            for _ in range(2):validate_context(client.sweep_context('session','sweep-3'),('session','s'),source(),2,2)
            self.assertEqual(seen,[('/api/v1/sessions/session/sweep-context',{'sweep_id':'sweep-3','before':2,'after':2})]*2)
        finally:server.shutdown();server.server_close();thread.join()

    def test_long_session_id_accepted_by_transport(self):
        client,opener=self.client(json.dumps(api_payload(response())).encode())
        session='session-'+'x'*200;client.sweep_context(session,'sweep-3')
        self.assertIn('/sessions/'+session+'/sweep-context',opener.open.call_args.args[0].full_url)

    def test_duplicate_json_and_bounded_timeout(self):
        for raw in (b'{"data": {}, "data": {}}', b'{"value": NaN}'):
            client,opener=self.client(raw)
            with self.assertRaises(PlatformClientError) as error:client.sweep_context('session','sweep-3')
            self.assertEqual(error.exception.code,'invalid_json')
        client,opener=self.client(json.dumps(api_payload(response())).encode());client.timeout=120;client.sweep_context('session','sweep-3')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],8)

    @unittest.skipUnless(os.getenv('ORI_CONTEXT_SMOKE_BASE_URL'),'optional live synthetic HTTP fixture')
    def test_live_synthetic(self):
        client=PlatformClient(os.environ['ORI_CONTEXT_SMOKE_BASE_URL']);r=client.sweep_context('context-one','one-sweep-4')
        self.assertEqual([i['id'] for i in r['items']],[f'one-sweep-{i}' for i in range(2,7)])
        anchor=r['items'][2];s=dict(anchor,stream_id=r['stream_id'],frequencies_hz=r['frequencies_hz'])
        validate_context(r,('context-one','spectrum-one'),s,2,2)


@unittest.skipUnless(os.environ.get('ORI_CONTEXT_SMOKE_BASE_URL'), 'Explicit synthetic fixture and Tk desktop required')
class ActualTkHTTPTests(unittest.TestCase):
    def test_actual_hidden_panel_with_retained_anchor_reload(self):
        import tkinter as tk
        from tkinter import ttk
        from time import sleep
        from types import SimpleNamespace
        root=tk.Tk();root.withdraw()
        client=PlatformClient(os.environ['ORI_CONTEXT_SMOKE_BASE_URL'])
        app=SimpleNamespace(tk=tk,ttk=ttk,root=root,client=client,session_id='context-one',selected_stream_id=lambda:'spectrum-one',
            server_var=tk.StringVar(value=client.base_url),snapshot=SimpleNamespace(session={'id':'context-one'},spectrum_stream_id='spectrum-one',sweeps=client.list_sweeps('context-one','spectrum-one')))
        notebook=ttk.Notebook(root);notebook.pack();p=SweepContextPanel(app,notebook)
        def finish():
            until=monotonic()+10
            while p.future is not None and monotonic()<until:root.update();sleep(.02)
            self.assertIsNotNone(p.data,p.info.get())
        try:
            p.update();p.anchor.set('one-sweep-4');self.assertEqual(p.chosen_source['id'],'one-sweep-4')
            p.analyze();finish();self.assertEqual([i['offset'] for i in p.data['items']],[-2,-1,0,1,2])
            for index,item in enumerate(p.data['items']):
                p.selection.current(index);p.draw()
                lines=[i for i in p.canvas.find_all() if p.canvas.type(i)=='line'];self.assertEqual(len(lines),1)
                self.assertEqual(len(p.canvas.coords(lines[0])),202)
                self.assertIn(item['id'],p.canvas.itemcget(p.canvas.find_all()[0],'text'))
            app.snapshot.sweeps=[];p.update();self.assertIn('вне текущей страницы',p.retained_info.get())
            p.before.set('1');p.after.set('1');self.assertIsNone(p.data);self.assertEqual(p.chosen_source['id'],'one-sweep-4')
            p.analyze();finish();self.assertEqual([i['offset'] for i in p.data['items']],[-1,0,1])
            p.clear();self.assertIsNone(p.data);self.assertIsNotNone(p.chosen_source)
            p.analyze();finish()
            app.snapshot.sweeps=client.list_sweeps('context-one','spectrum-one')
            app.server_var.set('http://localhost:56039');p.update();p.analyze()
            self.assertIsNone(p.data);self.assertIsNone(p.chosen_source);self.assertIsNone(p.future)
            self.assertEqual(p.anchor.get(),'');self.assertEqual(str(p.button['state']),'disabled')
        finally:p.close();root.destroy()
