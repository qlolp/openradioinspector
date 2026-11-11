"""Bounded async instrument operations. HTTP never owns a serial port."""
from __future__ import annotations
import copy
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from urllib.parse import unquote
from ..contracts import api_payload
from ..device_workers import WorkerError
from .artifacts import summary
from .commands import catalog
from .forms import OPERATIONS as FORM_OPERATIONS

OPERATIONS=frozenset({'inspect','capture','lease.acquire','lease.renew','lease.release','monitor.resume',
    'setting','touch','release','keypad','menu.home','sd.list','sd.read','sd.delete',
    'preset.save','preset.load','acquire.raw','acquire.zero','service.guide'}) | frozenset(FORM_OPERATIONS)
TERMINAL={'succeeded','failed','cancelled','unknown'}


class Conflict(ValueError):pass


class InstrumentOperations:
    def __init__(self,sessions,workers):
        self.sessions=sessions;self.workers=workers
        self._lock=threading.RLock();self._jobs={};self._requests={};self._threads=set();self._closed=False

    def scope(self,sid,stream):
        item=self.sessions.instrument_context(sid,stream)
        if item.settings.get('adapter')!='tinysa-serial':raise ValueError('Stream is not tinySA serial')

    def state(self,sid,stream):
        self.scope(sid,stream)
        current=next((x['current'] for x in self.workers.list_workers(sid) if x['stream_id']==stream),{})
        return current.get('instrument',dict(mode='monitoring',revision=0,profile_verified=False,
            lease_active=False,settings={},settings_source='unknown',settings_readback=False))

    @staticmethod
    def view(job,include_result=True,*,reveal_owner=False):
        result={k:job[k] for k in ['operation_id','session_id','stream_id','operation','state','error','created_at']}
        if include_result:
            result['result']=copy.deepcopy(job.get('result'))
            if isinstance(result['result'],dict) and not reveal_owner:result['result'].pop('lease_id',None)
        return result

    def submit(self,sid,stream,value,*,client_key=None):
        self.scope(sid,stream)
        if client_key is not None and (not isinstance(client_key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{16,128}',client_key)):
            raise ValueError('Invalid X-Instrument-Client capability')
        owner=hashlib.sha256(client_key.encode()).hexdigest() if client_key else None
        if not isinstance(value,dict) or set(value)-{'operation','parameters','lease_id','expected_revision','request_id'}:
            raise ValueError('Invalid instrument request')
        if value.get('operation') not in OPERATIONS or not isinstance(value.get('parameters',{}),dict):
            raise ValueError('Unknown typed instrument operation')
        if value['operation']=='lease.acquire' and owner is None:raise ValueError('Lab acquisition requires private X-Instrument-Client capability')
        canonical=json.dumps(value,sort_keys=True,allow_nan=False)
        if len(canonical.encode())>65536:raise ValueError('Request too large')
        request_id=value.get('request_id')
        if request_id is not None and (not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',request_id)):
            raise ValueError('Invalid request_id')
        # Preview/metadata/stop actions are safe to repeat and must never consume
        # the mutation ledger (a 2fps screen otherwise fills it in ~17 minutes).
        track_request=not (value['operation'] in {'inspect','service.guide','lease.renew','output.off','audio.inputs'} or (value['operation']=='capture' and value.get('parameters',{}).get('save') is not True))
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest();key=(sid,stream,owner,request_id)
        with self._lock:
            if self._closed:raise Conflict('Instrument service closed')
            if request_id is not None and key in self._requests:
                digest,identifier=self._requests[key]
                if digest!=fingerprint:raise Conflict('request_id identifies another operation')
                if identifier not in self._jobs:raise Conflict('History expired; mutation will not be replayed')
                return self.view(self._jobs[identifier],False)
            if track_request and request_id is not None and len(self._requests)>=2048:raise Conflict('Idempotency ledger full; no mutation accepted')
            if value['operation']=='capture' and value.get('parameters',{}).get('save') is not True:
                for old in reversed(list(self._jobs.values())):
                    if old['session_id']==sid and old['stream_id']==stream and old['operation']=='capture' and old['input'].get('parameters',{}).get('save') is not True and time.monotonic()-old['created_at']<.5:
                        return self.view(old,False)
            if value['operation']=='output.off':
                old=next((j for j in self._jobs.values() if j['session_id']==sid and j['stream_id']==stream and j['operation']=='output.off' and j['state'] not in TERMINAL),None)
                if old:return self.view(old,False)
            maximum=5 if value['operation']=='output.off' else 4
            if len(self._threads)>=maximum and value['operation']!='lease.renew':raise Conflict('Instrument busy; command not queued')
            worker=self.workers.instrument_worker(sid,stream)
            while len(self._jobs)>=128:
                removable=next((k for k,v in self._jobs.items() if v['state'] in TERMINAL),None)
                if removable is None:raise Conflict('Job limit reached')
                del self._jobs[removable]
            job=dict(operation_id=uuid.uuid4().hex,session_id=sid,stream_id=stream,operation=value['operation'],
                state='queued',error=None,result=None,created_at=time.monotonic(),input=copy.deepcopy(value),worker=worker,cancel_requested=False,owner=owner)
            self._jobs[job['operation_id']]=job
            if track_request and request_id is not None:self._requests[key]=(fingerprint,job['operation_id'])
            if value['operation']=='lease.renew':
                # Pure metadata: a long SD transfer cannot starve heartbeats.
                try:
                    controller=getattr(worker.adapter,'_instrument',None)
                    if controller is None:raise ValueError('No active lab lease')
                    job['result']=controller.renew_only(value);job['state']='succeeded'
                except (ValueError,WorkerError) as exc:job['error']=str(exc);job['state']='failed'
            else:
                thread=threading.Thread(target=self.run,args=(job,),daemon=True,name='instrument-'+job['operation_id'][:8])
                self._threads.add(thread);thread.start()
            return self.view(job,False)

    def run(self,job):
        try:
            with self._lock:
                if self._closed or job['cancel_requested']:job['state']='cancelled';return
                job['state']='running'
            payload={k:v for k,v in job['input'].items() if k!='request_id'}
            result=self.workers.instrument(job['session_id'],job['stream_id'],payload,expected_worker=job['worker'])
            if 'artifact' in result:
                artifact=result.pop('artifact')
                if job['operation']=='capture' and job['input'].get('parameters',{}).get('save') is not True:result['preview']=artifact
                else:result['artifact']=self.sessions.save_instrument_artifact(job['session_id'],job['stream_id'],artifact)
            with self._lock:job['result']=result;job['state']='succeeded'
        except Exception as exc:
            with self._lock:
                job['error']=str(exc)
                job['state']='unknown' if getattr(exc,'uncertain',False) or getattr(job['worker'].adapter,'_restart_required',False) else 'cancelled' if job['cancel_requested'] else 'failed'
        finally:
            with self._lock:self._threads.discard(threading.current_thread())

    def get(self,sid,stream,identifier,*,client_key=None):
        self.scope(sid,stream)
        with self._lock:
            job=self._jobs.get(identifier)
            if job is None or job['session_id']!=sid or job['stream_id']!=stream:raise KeyError('Operation not found')
            digest=hashlib.sha256(client_key.encode()).hexdigest() if isinstance(client_key,str) else ''
            result=self.view(job,reveal_owner=job.get('owner') is not None and hmac.compare_digest(job['owner'],digest))
            try:result['context_current']=self.workers.instrument_worker(sid,stream) is job['worker']
            except WorkerError:result['context_current']=False
            return result

    def list(self,sid,stream):
        self.scope(sid,stream)
        with self._lock:return [self.view(x,False) for x in reversed(list(self._jobs.values())) if x['session_id']==sid and x['stream_id']==stream][:20]

    def cancel(self,sid,stream,identifier):
        self.get(sid,stream,identifier)
        with self._lock:
            job=self._jobs[identifier]
            if job['state'] in TERMINAL:return self.view(job)
            job['cancel_requested']=True
            if job['state']=='queued':job['state']='cancelled';return self.view(job)
            worker=job['worker']
        worker.stop(timeout=5)
        return self.get(sid,stream,identifier)

    def close(self):
        with self._lock:
            self._closed=True
            active=[j for j in self._jobs.values() if j['state'] not in TERMINAL];threads=list(self._threads)
            for job in active:job['cancel_requested']=True
        for worker in {j['worker'] for j in active}:worker.stop(timeout=5)
        for thread in threads:
            if thread is not threading.current_thread():thread.join(2)
        if any(t.is_alive() for t in threads):raise RuntimeError('Instrument I/O active; archive remains open')


def dispatch(handler,method,route,payload=None,query=None):
    parts=route.strip('/').split('/')
    if len(parts)<7 or parts[:3]!=['api','v1','sessions'] or parts[4]!='streams' or parts[6]!='instrument':return False
    sid,stream=unquote(parts[3]),unquote(parts[5]);tail=parts[7:];service=handler.instruments
    client_key=handler.headers.get('X-Instrument-Client')
    try:
        if method=='GET' and tail and tail[0]=='artifacts':
            handler.sessions.instrument_context(sid,stream)
        else:
            service.scope(sid,stream)
        if method=='GET':
            if tail==['capabilities']:data=dict(capabilities=catalog(),status=service.state(sid,stream))
            elif tail in (['settings'],['control-lease']):data=service.state(sid,stream)
            elif tail==['operations']:data=dict(items=service.list(sid,stream))
            elif len(tail)==2 and tail[0]=='operations':data=service.get(sid,stream,tail[1],client_key=client_key)
            elif tail==['screen']:
                handler._send_json(api_payload(service.submit(sid,stream,{'operation':'capture','parameters':{'save':False}})),202);return True
            elif tail==['artifacts']:data=dict(items=handler.sessions.list_instrument_artifacts(sid,stream,int((query or {}).get('limit',['100'])[0]),int((query or {}).get('offset',['0'])[0])))
            elif len(tail) in (2,3) and tail[0]=='artifacts':
                item=handler.sessions.get_instrument_artifact(sid,stream,unquote(tail[1]))
                if len(tail)==3:
                    if tail[2]!='content':raise KeyError('Artifact route not found')
                    handler.send_response(200);handler.send_header('Content-Type',item['mime_type'])
                    handler.send_header('Content-Length',str(len(item['payload'])))
                    handler.send_header('Content-Disposition',('inline' if item['mime_type']=='image/png' else 'attachment')+'; filename="'+item['filename']+'"')
                    handler.send_header('Cache-Control','no-store');handler.send_header('X-Content-Type-Options','nosniff');handler.end_headers()
                    try:handler.wfile.write(item['payload'])
                    except (BrokenPipeError,ConnectionResetError):pass
                    return True
                data=summary(item)
            else:raise KeyError('Instrument route not found')
            handler._send_json(api_payload(data))
        elif method=='POST':
            if tail==['operations']:value=payload
            elif tail==['settings']:value={**payload,'operation':'setting'}
            elif tail==['control-lease']:
                value=dict(payload);action=value.pop('action','acquire')
                if action not in {'acquire','renew','release','resume'}:raise ValueError('Unknown lease action')
                value['operation']='monitor.resume' if action=='resume' else 'lease.'+action
            elif len(tail)==3 and tail[0]=='operations' and tail[2]=='cancel':
                handler._send_json(api_payload(service.cancel(sid,stream,tail[1])));return True
            else:raise KeyError('Instrument route not found')
            handler._send_json(api_payload(service.submit(sid,stream,value,client_key=client_key)),202)
        else:raise ValueError('Unsupported method')
    except KeyError as exc:handler._send_api_error(404,'instrument_not_found',str(exc))
    except (Conflict,WorkerError) as exc:handler._send_api_error(409,'instrument_conflict',str(exc))
    except (ValueError,TypeError,OverflowError) as exc:handler._send_api_error(400,'instrument_invalid',str(exc))
    return True
