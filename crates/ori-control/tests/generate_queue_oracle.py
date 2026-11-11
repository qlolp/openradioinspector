"""Actual legacy HTTP/worker serialization, owned in-memory tinySA transport."""
from __future__ import annotations
import json
from pathlib import Path
import threading
import time
from radioinspector.device_workers import WorkerError
from tests.test_tinysa_instrument_api import InstrumentHTTPTests

def queued(case, count):
    worker=case.workers.instrument_worker(case.session.id,case.stream.id)
    deadline=time.monotonic()+3
    while worker._commands.qsize()<count:
        if time.monotonic()>deadline:raise AssertionError('Owned queue barrier timed out')
        time.sleep(.001)

def main():
    case=InstrumentHTTPTests(methodName='runTest');case.setUp()
    try:
        lease=case.finish(case.submit({'operation':'lease.acquire','parameters':{'acknowledge_rf':True}}))
        token=lease['result']['lease_id'];revision=lease['result']['status']['revision']
        wire=case.wires[0];wire.responses.update({'repeat 2':b'','repeat 3':b'','repeat 4':b''})
        entered=threading.Event();release=threading.Event();original=wire.send
        def send(data):
            if data==b'repeat 2\r':
                entered.set()
                if not release.wait(3):raise AssertionError('Owned write release timed out')
            original(data)
        wire.send=send;wire.sent.clear()
        first=case.submit({'operation':'setting','parameters':{'name':'repeat','value':2},'lease_id':token})
        assert entered.wait(3)
        second=case.submit({'operation':'setting','parameters':{'name':'repeat','value':3},'lease_id':token})
        stale=case.submit({'operation':'setting','parameters':{'name':'repeat','value':4},'lease_id':token,'expected_revision':revision})
        queued(case,2);release.set()
        views=[case.finish(k) for k in [first,second,stale]]
        record={'source':'Real Python InstrumentOperations HTTP + DeviceWorkerManager + Controller + UltraWire; no hardware',
                'omitted_then_stale':{'states':[v['state'] for v in views],'revisions':[v.get('result',{}).get('revision') if isinstance(v.get('result'),dict) else None for v in views],'commands':list(wire.sent),'explicit_revision':revision,'admission_keys':sorted(case.request('POST',case.base+'/operations',{'operation':'inspect'})[1]['data'])}}
        # Original cache-only policy callback is captured by the queued command.
        allowed=threading.Event();allowed.set()
        def authorize(*_):
            if not allowed.is_set():raise WorkerError('Owned original authority revoked')
        case.workers.control_authorizer=authorize
        entered.clear();release.clear();wire.sent.clear()
        first=case.submit({'operation':'setting','parameters':{'name':'repeat','value':2},'lease_id':token})
        assert entered.wait(3)
        denied=case.submit({'operation':'setting','parameters':{'name':'repeat','value':3},'lease_id':token})
        queued(case,1);allowed.clear();release.set()
        views=[case.finish(k) for k in [first,denied]]
        record['revoke_queued']={'states':[v['state'] for v in views],'commands':list(wire.sent)}
        path=Path(__file__).with_name('fixtures')/'python_queue.json'
        path.write_text(json.dumps(record,indent=2)+'\n')
    finally:case.doCleanups()

if __name__=='__main__':main()
