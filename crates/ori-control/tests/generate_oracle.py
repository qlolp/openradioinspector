"""Owned Python controller/analog fixtures; never enumerates ports or audio."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import wave
from radioinspector.tinysa.commands import catalog
from radioinspector.tinysa.controller import Controller
from radioinspector.tinysa.workflows import GUIDES, service_guide
from tests.tinysa_control_fixture import make_adapter

HERE=Path(__file__).parent

def main():
    resources=HERE.parent/'src'/'resources';resources.mkdir(exist_ok=True)
    (resources/'capabilities.json').write_text(json.dumps(catalog(),ensure_ascii=False,indent=2)+'\n')
    (resources/'guides.json').write_text(json.dumps({k:service_guide(k) for k in GUIDES},ensure_ascii=False,indent=2)+'\n')
    wav=io.BytesIO()
    with wave.open(wav,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(bytes(range(256))*6+b'\0'*64)
    wav=wav.getvalue()
    scan=b'{'+b''.join(b'x'+round((174+level)*32).to_bytes(2,'little') for level in [-90,-80,-70])+b'}'
    extras={c:b'' for c in ['repeat 3','touch 1 2','text 100M','menu','sd_delete TRACE.CSV','save 0','recall 0','ultra off','sweep 100000000 101000000 3','trace 1 copy 2','marker 1 on','selftest 1','mode output','sweep 30000000 31000000 100','level -30','levelchange 0','sweeptime 1','output on','sweep go']}
    extras.update({'capture':b'\xf8\0'*153600,'sd_list':b'TRACE.CSV\r\n','sd_read TRACE.CSV':len(b'original\0SD\xffch> bytes').to_bytes(4,'little')+b'original\0SD\xffch> bytes','sd_delete TRACE.CSV':b'delete: TRACE.CSV OK\r\n','scanraw 100000000 101000000 3 0':scan,'scanraw 100000000 100000000 3 0':scan,'trace 1 value':b'0 -90\r\n1 -80\r\n','marker 1':b'marker 1 100500000 -80\r\n','rbw':b'rbw {auto|...}\r\n10000Hz\r\n'})
    test=unittest.TestCase()
    try:
        _,_,adapter,wire=make_adapter(test,extras);adapter._clock=lambda:datetime(2026,11,11,12,0,0,tzinfo=timezone.utc)
        wire.responses['help'] += b'sweeptime\r\n'
        controller=Controller(adapter,monotonic=lambda:100.)
        controller.execute({'operation':'inspect'});wire.sent.clear()
        cases=[];lease=None
        def run(op,p=None,lab=False):
            nonlocal lease
            request={'operation':op,'parameters':p or {}}
            if lab:request['lease_id']=lease
            before=len(wire.sent); result=controller.execute(request)
            if 'lease_id' in result:lease=result['lease_id'];result['lease_id']='<private synthetic lease>'
            if 'status' in result:result['status'].pop('connection_id',None)
            result.pop('connection_id',None)
            artifact=result.get('artifact')
            if artifact:
                artifact['metadata'].pop('connection_id',None);artifact['metadata'].pop('segment_id',None)
                if op=='capture':artifact.pop('data_base64');artifact['representation']='PNG payload omitted; exhaustive pixel oracle is separate'
            cases.append({'operation':op,'parameters':p or {},'commands':wire.sent[before:],'result':result})
        run('inspect');run('service.guide');run('capture');run('lease.acquire',{'acknowledge_rf':True});run('lease.renew',lab=True)
        run('setting',{'name':'repeat','value':3},True);run('touch',{'x':1,'y':2},True);run('release',lab=True);run('keypad',{'text':'100M'},True);run('menu.home',lab=True)
        run('sd.list');run('sd.read');run('sd.delete',{'confirm_delete':True},True);run('preset.save',{'confirm':True},True);run('preset.load',{'confirm':True},True)
        document=json.dumps({'schema_version':1,'profile':'tinysa-ultra-zs405-control-v1','settings':{'start_hz':100000000,'stop_hz':101000000,'points':3}})
        run('preset.import',{'document':document,'confirm':True},True);run('acquire.raw',{'points':3},True);run('acquire.zero',{'points':3},True)
        run('trace',{},True);run('trace',{'action':'copy'},True);run('marker',{},True);run('marker',{'action':'on'},True);run('preset.export')
        run('selftest.start',{'confirm':True},True);run('generator.sweep',{'confirm':True},True);run('output.off')
        with patch('radioinspector.tinysa.audio.inputs',return_value={'available':True,'devices':[{'index':0,'name':'owned-memory-input','input_channels':1,'default_sample_rate_hz':8000.0}],'reason':None,'source':'analog_input','usb_pcm':False}),patch('radioinspector.tinysa.audio.capture',return_value=wav):
            run('audio.inputs');run('audio.capture',{'device':0,'seconds':0.1,'sample_rate_hz':8000,'confirm_connection':True},True)
        run('lease.release',lab=True);run('lease.acquire',{'acknowledge_rf':True});run('monitor.resume',lab=True)
        for method in ['peak','occupied_bandwidth','bandwidth_3db','centroid','channel_power']:run('measurement',{'method':method,'points':3})
        fixtures=HERE/'fixtures';fixtures.mkdir(exist_ok=True)
        (fixtures/'python_control.json').write_text(json.dumps({'source':'Actual Python Controller + UltraWire; memory-selected analog WAV; no hardware','cases':cases,'wav_base64':base64.b64encode(wav).decode()},ensure_ascii=False,indent=2)+'\n')
    finally:test.doCleanups()

if __name__=='__main__':main()
