"""Opt-in physical acceptance. Never imported by automatic test discovery.

Default receive-only. RF tests require an additional explicit flag and setup
acknowledgement. A short run never reports eight-hour acceptance.
"""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radioinspector.contracts import format_time,utc_now
from radioinspector.web.server import create_server
from radioinspector.windows.client import PlatformClient
from radioinspector.windows.tinysa import InstrumentConnection


def main(argv=None):
    parser=argparse.ArgumentParser(description='Physical ZS405/core/API/archive endurance check (default 8 hours, receive-only)')
    parser.add_argument('--port',required=True)
    parser.add_argument('--ardv1-port',help='Optional second real receiver for independence/endurance check')
    parser.add_argument('--output-dir',required=True,help='New private directory outside the repository')
    parser.add_argument('--hours',type=float,default=8)
    parser.add_argument('--sd-file',help='Read-only SD round-trip filename; never delete')
    parser.add_argument('--lab-rf-test',action='store_true',help='Enable a 30MHz/-60dBm test for 3s, then off; needs a load and external measurement')
    parser.add_argument('--confirm-physical-setup',action='store_true')
    args=parser.parse_args(argv)
    if not math.isfinite(args.hours) or not 0<args.hours<=24:parser.error('--hours must be >0 and <=24')
    if args.lab_rf_test and not args.confirm_physical_setup:parser.error('RF test needs --confirm-physical-setup')
    output=Path(args.output_dir).expanduser().resolve()
    repo=Path(__file__).resolve().parents[1]
    if output==repo or repo in output.parents:parser.error('Hardware records must remain outside the repository')
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    report=dict(started_at=format_time(utc_now()),requested_hours=args.hours,
                eight_hour_acceptance='not_completed',physical_rf_measurement='not_recorded',checks=[],errors=[])
    server=create_server(host='127.0.0.1',port=0,session_database=str(output/'archive.sqlite'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=PlatformClient(f'http://127.0.0.1:{server.server_address[1]}',timeout=10)
    context=None;started=time.monotonic();sid=None
    def record(name,result):
        report['checks'].append(dict(name=name,at=format_time(utc_now()),result=result))
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        streams=[dict(device='tinySA Ultra',role='spectrum_analyzer',level_unit='dBm',port=args.port,
                      capabilities=['sweep'],settings=dict(adapter='tinysa-serial',start_hz=100000000,stop_hz=101000000,points=101,rbw_hz=10000,period_s=2,max_consecutive_errors=1))]
        if args.ardv1_port:streams.append(dict(device='AOR AR-DV1',role='receiver',level_unit='s_meter_count',port=args.ardv1_port,capabilities=['receiver'],settings=dict(adapter='ardv1-serial',frequency_hz=100000000,mode='FM',period_s=2,max_consecutive_errors=1)))
        session=client.create_session(dict(name='Physical full tinySA acceptance',streams=streams));sid=session['id'];stream=session['streams'][0]['id']
        client.start_session(sid);client.start_workers(sid)
        context=InstrumentConnection(client,sid,stream)
        record('identity_and_help',context.operation('inspect'))
        record('screen',context.operation('capture',{'save':True}))
        for name,operation in [('scanraw','acquire.raw'),('zero_span','acquire.zero')]:
            result=context.operation(operation,{'start_hz':100000000,'stop_hz':101000000,'points':450} if name=='scanraw' else {'start_hz':100000000,'points':450})
            item=result['artifact'];raw=client.instrument_artifact(sid,stream,item['id']);data=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()!=item['sha256'] or len(data['levels'])!=450 or data['level_unit']!='dBm' or not all(math.isfinite(v) for v in data['levels']):raise RuntimeError('Invalid acquisition artifact')
            if name=='zero_span' and data['sample_offsets_seconds'] is not None:raise RuntimeError('Fabricated zero-span timing')
            (output/item['filename']).write_bytes(raw);record(name,item)
        if args.sd_file:
            result=context.operation('sd.read',{'filename':args.sd_file});item=result['artifact']
            raw=client.instrument_artifact(sid,stream,item['id'])
            if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise RuntimeError('SD checksum mismatch')
            (output/item['filename']).write_bytes(raw);record('sd_read',item)
        if args.lab_rf_test:
            context.operation('lease.acquire',{'acknowledge_rf':True})
            try:
                for name,value in [('output_hz',30000000),('output_dbm',-60),('output_enabled',True)]:context.operation('setting',{'name':name,'value':value})
                record('rf_requested',dict(hz=30000000,dbm=-60,readback=False))
                time.sleep(3)
                record('rf_screen',context.operation('capture',{'save':True}))
            finally:
                record('rf_off',context.operation('output.off'))
                context.operation('monitor.resume')
        deadline=started+args.hours*3600;minimum={}
        while time.monotonic()<deadline:
            workers=client.list_workers(sid)
            for worker in workers:
                if worker.get('last_error') or not worker.get('alive'):raise RuntimeError('Worker degraded: '+str(worker))
                minimum[worker['stream_id']]=worker['measurements']
            record('endurance_checkpoint',minimum.copy())
            time.sleep(min(30,max(0,deadline-time.monotonic())))
        if any(minimum.get(item['id'],0)<2 for item in session['streams']):raise RuntimeError('Not enough real measurements')
        report['elapsed_seconds']=time.monotonic()-started
        report['eight_hour_acceptance']='protocol_run_completed' if args.hours>=8 and report['elapsed_seconds']>=8*3600 else 'partial_run_only'
        report['two_device_run']=bool(args.ardv1_port)
        report['physical_rf_measurement']='requires_external_record; command_ACK_is_not_RF_readback'
    except (Exception,KeyboardInterrupt) as exc:
        report['errors'].append(str(exc) or 'interrupted');return_code=1
    else:return_code=0
    finally:
        if context:
            if context.lease:
                try:context.operation('output.off')
                except Exception as exc:report['errors'].append('RF state unknown: '+str(exc))
            context.close()
        server.shutdown();thread.join(3)
        try:server.server_close()
        except Exception as exc:report['errors'].append('Cleanup failed: '+str(exc));return_code=1
        report['ended_at']=format_time(utc_now())
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(str(output/'report.json'))
    return return_code

if __name__=='__main__':raise SystemExit(main())
