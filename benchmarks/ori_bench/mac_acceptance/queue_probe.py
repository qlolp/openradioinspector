"""Measure real persisted investigation admission without starting a device."""
from dataclasses import replace
import threading
import time
from ..client import Client,segment
from ..source import session,interval


class QueueProbe:
    def __init__(self,config,emit):
        self.config=replace(config,run_id=config.run_id+'-queue')
        self.emit=emit;self.stop=threading.Event();self.errors=[]
        self.thread=threading.Thread(target=self.run,name='mac-acceptance-queue')
    def start(self):self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread.ident is None:return
        self.thread.join(self.config.request_timeout+2)
        if self.thread.is_alive():raise RuntimeError('Queue probe did not join')
    def run(self):
        try:
            client=Client(self.config)
            client.json('/api/v1/sessions',method='POST',payload=session(self.config),status=201)
            base='/api/v1/sessions/'+segment(self.config.session_id)
            anchor=time.monotonic();period=self.config.duration_seconds/100
            for index in range(100):
                if self.stop.wait(max(0,anchor+index*period-time.monotonic())):return
                identifier=self.config.run_id+'-candidate-'+str(index)
                client.json(base+'/candidates',method='POST',status=201,payload=dict(id=identifier,
                    source_stream_id=self.config.spectrum_id,interval=interval(0,70_000),
                    center_frequency_hz=100_000_001,level_unit='dBm',peak_level=-80.0,score=1.0,observation_ids=[]))
                request='enqueue-'+str(index)
                self.emit('queue.begin',dict(request_id=request,operation='investigation.enqueue'))
                job=client.json(base+'/candidates/'+segment(identifier)+'/investigations',method='POST',status=201,
                    payload=dict(target_stream_id=self.config.receiver_id,target_frequency_hz=100_000_001))
                if not isinstance(job.get('id'),str) or job.get('state')!='queued':
                    raise RuntimeError('Actual scheduler did not confirm queued state and ID')
                self.emit('queue.accepted',dict(request_id=request,job_id=job['id'],operation='investigation.enqueue'))
        except Exception as error:
            self.errors.append(str(error));self.emit('queue.error',dict(message=str(error)))
