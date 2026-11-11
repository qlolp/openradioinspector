from __future__ import annotations

from dataclasses import asdict,dataclass,field
from decimal import Decimal
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid


class BenchmarkError(Exception):
    def __init__(self,code,message):
        self.code,self.message=code,message
        super().__init__(message)


def microseconds(value):
    try:
        number=Decimal(str(value))*1_000_000
        if not number.is_finite() or number != number.to_integral_value() or number<=0:
            raise ValueError()
        return int(number)
    except (ValueError,TypeError,ArithmeticError):
        raise BenchmarkError('invalid_config','Times must be positive exact microseconds') from None


@dataclass(frozen=True)
class Config:
    base_url:str
    output_dir:Path
    run_id:str=field(default_factory=lambda:'bench-'+uuid.uuid4().hex)
    mode:str='run'
    scenario:str='stress'
    endpoint_label:str='explicit-http-endpoint'
    duration_seconds:float=1800.0
    sweep_rate:int=10
    observation_rate:int=10
    points:int=4096
    audio_segment_seconds:float=10.0
    speed:float=1.0
    workers:int=2
    queue_capacity:int=64
    request_timeout:float=10.0
    drain_seconds:float=60.0
    history_repeats:int=20
    sweeps:int=1_000_000
    batch_size:int=16
    allow_million:bool=False
    fault_kind:str='invalid-batch'
    fault_every:int=10
    lifecycle_step_seconds:float=3600.0
    sample_interval:float=0.5
    pids:dict=field(default_factory=dict)
    render_events:str|None=None

    def __post_init__(self):
        url=urlsplit(self.base_url)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password \
                or url.path not in ('','/') or url.query or url.fragment:
            raise BenchmarkError('invalid_config','Supply an explicit HTTP(S) core origin without credentials')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',self.run_id):
            raise BenchmarkError('invalid_config','run_id must be 1..64 simple identifier characters')
        if self.mode not in ('run','history') or self.scenario not in ('stress','overload','fault','lifecycle'):
            raise BenchmarkError('invalid_config','Unknown workload mode or scenario')
        for name,maximum in (('sweep_rate',100_000),('observation_rate',100_000),('points',65_536),
                             ('workers',16),('queue_capacity',4096),('history_repeats',1000),
                             ('sweeps',1_000_000),('batch_size',100),('fault_every',1_000_000)):
            value=getattr(self,name)
            if type(value) is not int or not 1<=value<=maximum:
                raise BenchmarkError('invalid_config','Workload integer is outside supported bounds: '+name)
        for name in ('speed','request_timeout','drain_seconds','sample_interval'):
            value=getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise BenchmarkError('invalid_config','Invalid positive finite setting: '+name)
        if self.sample_interval<.01 or self.request_timeout>120 or self.drain_seconds>3600:
            raise BenchmarkError('invalid_config','Sampling, request, or drain interval exceeds supported bounds')
        if self.duration_us>366*86400*1_000_000 or self.segment_us>60*1_000_000:
            raise BenchmarkError('invalid_config','Duration or audio segment exceeds the bounded workload limit')
        if self.duration_us*48_000%1_000_000 or self.segment_us*48_000%1_000_000:
            raise BenchmarkError('invalid_config','Audio boundaries must represent complete 48 kHz frames')
        if self.scenario=='lifecycle' and (microseconds(self.lifecycle_step_seconds)<self.segment_us \
                or microseconds(self.lifecycle_step_seconds)*48000%1000000):
            raise BenchmarkError('invalid_config','Lifecycle cadence must fit disjoint complete-frame audio segments')
        if self.fault_kind not in ('invalid-batch','client-gap'):
            raise BenchmarkError('invalid_config','Unknown explicit client fault')
        if self.mode=='history' and self.sweeps>=1_000_000 and not self.allow_million:
            raise BenchmarkError('explicit_opt_in_required','The million-sweep history fixture requires --allow-million')
        if not isinstance(self.endpoint_label,str) or not 0<len(self.endpoint_label)<=128:
            raise BenchmarkError('invalid_config','Endpoint label must be bounded text')
        for label,pid in self.pids.items():
            if label=='harness' or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}',label) or type(pid) is not int or pid<=0:
                raise BenchmarkError('invalid_config','Process roots must be label=positive-pid')

    @property
    def duration_us(self): return microseconds(self.duration_seconds)
    @property
    def segment_us(self): return microseconds(self.audio_segment_seconds)
    @property
    def session_id(self): return self.run_id
    @property
    def spectrum_id(self): return self.run_id+'-spectrum'
    @property
    def receiver_id(self): return self.run_id+'-receiver'

    def period_us(self,kind):
        if self.scenario=='lifecycle':return microseconds(self.lifecycle_step_seconds)
        return 1_000_000/(self.sweep_rate if kind=='sweep' else self.observation_rate)

    def planned_counts(self):
        if self.mode=='history':return {'sweep':self.sweeps,'observation':0,'audio':0}
        if self.scenario=='lifecycle':
            count=(self.duration_us+microseconds(self.lifecycle_step_seconds)-1)//microseconds(self.lifecycle_step_seconds)
            return dict(sweep=count,observation=count,audio=count)
        return {'sweep':(self.duration_us*self.sweep_rate+999_999)//1_000_000,
                'observation':(self.duration_us*self.observation_rate+999_999)//1_000_000,
                'audio':(self.duration_us+self.segment_us-1)//self.segment_us}

    def audio_bounds(self,index):
        start_us=index*(microseconds(self.lifecycle_step_seconds) if self.scenario=='lifecycle' else self.segment_us)
        span=min(self.segment_us,self.duration_us-start_us)
        return start_us*48_000//1_000_000,span*48_000//1_000_000

    @property
    def planned_audio_frames(self):
        if self.mode=='history':return 0
        count=self.planned_counts()['audio']
        if self.scenario!='lifecycle':return self.duration_us*48_000//1_000_000
        return (count-1)*(self.segment_us*48000//1000000)+self.audio_bounds(count-1)[1]

    def public(self):
        value=asdict(self)
        value.pop('base_url');value.pop('output_dir');value.pop('render_events')
        return value|{'planned_counts':self.planned_counts(),'planned_audio_frames':self.planned_audio_frames,
                      'synthetic':True,'audio_sample_rate':48000,'audio_channels':1,'audio_sample_bytes':2}
