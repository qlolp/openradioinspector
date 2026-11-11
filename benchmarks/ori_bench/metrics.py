from __future__ import annotations

from bisect import bisect_left
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time


class Histogram:
    """Fixed memory, conservative bucket upper bounds (about 2% resolution)."""
    def __init__(self):
        self.bounds=[.01*1.02**i for i in range(1024)]
        self.bins=[0]*1025
        self.count=0;self.total=0.0;self.maximum=0.0;self.minimum=None
        self.lock=threading.Lock()
    def add(self,value):
        if not math.isfinite(value) or value<0:raise ValueError('Invalid latency')
        with self.lock:
            self.bins[bisect_left(self.bounds,value)]+=1
            self.count+=1;self.total+=value;self.maximum=max(self.maximum,value)
            self.minimum=value if self.minimum is None else min(self.minimum,value)
    def report(self):
        with self.lock:
            def quantile(fraction):
                threshold=math.ceil(self.count*fraction);count=0
                for index,amount in enumerate(self.bins):
                    count+=amount
                    if count>=threshold:return self.bounds[index] if index<len(self.bounds) else self.maximum
            return {'count':self.count,'mean_ms':self.total/self.count if self.count else None,
                'min_ms':self.minimum,'max_ms':self.maximum if self.count else None,
                'median_upper_ms':quantile(.5) if self.count else None,
                'p95_upper_ms':quantile(.95) if self.count else None,
                'quantile_method':'fixed logarithmic histogram, conservative upper bucket, <=2% bucket width'}


def host():
    result={'python':sys.version.split()[0],'platform':platform.platform(),
            'machine':platform.machine(),'cpu_count':os.cpu_count(),'hardware_tested':False}
    try:result['physical_memory_bytes']=os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
    except (AttributeError,ValueError,OSError):result['physical_memory_bytes']=None
    return result


def linux_processes():
    if sys.platform!='linux':return None
    result={}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():continue
        try:
            fields=(path/'stat').read_text().rpartition(')')[2].split()
            result[int(path.name)]={'parent':int(fields[1]),'start':int(fields[19]),
                'rss':int((path/'statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')}
        except (OSError,ValueError,IndexError):continue
    return result


class Sampler:
    def __init__(self,config):
        self.roots={'harness':os.getpid()}|config.pids
        self.interval=config.sample_interval
        self.result={label:{'root_pid':pid,'samples':0,'peak_root_rss_bytes':0,
            'peak_tree_rss_bytes':0,'max_process_count':0,'missing_samples':0} for label,pid in self.roots.items()}
        self.identities={};self.stop_event=threading.Event();self.thread=None
    def sample(self):
        processes=linux_processes()
        if processes is None:return
        for label,pid in self.roots.items():
            record=self.result[label]
            if pid not in processes or (label in self.identities and processes[pid]['start']!=self.identities[label]):
                record['missing_samples']+=1;continue
            self.identities[label]=processes[pid]['start']
            children={pid}
            while True:
                additions={child for child,info in processes.items() if info['parent'] in children}-children
                if not additions:break
                children.update(additions)
            record['samples']+=1
            record['peak_root_rss_bytes']=max(record['peak_root_rss_bytes'],processes[pid]['rss'])
            record['peak_tree_rss_bytes']=max(record['peak_tree_rss_bytes'],sum(processes[p]['rss'] for p in children))
            record['max_process_count']=max(record['max_process_count'],len(children))
    def __enter__(self):
        self.sample()
        def loop():
            while not self.stop_event.wait(self.interval):self.sample()
        self.thread=threading.Thread(target=loop,name='benchmark-resource-sampler',daemon=True)
        self.thread.start();return self
    def __exit__(self,*args):
        self.stop_event.set();self.thread.join();self.sample()
    def report(self):
        result={label:record|{'status':'measured' if record['samples'] else 'unmeasured'}
                for label,record in self.result.items()}
        return {'status':'measured' if sys.platform=='linux' else 'unmeasured',
            'method':'sampled /proc RSS sums; shared pages double-counted; short-lived peaks can be missed',
            'interval_seconds':self.interval,'process_trees':result,
            'core_process_tree_status':'measured' if any(record['samples'] for label,record in self.result.items()
                                                        if label!='harness') else 'unmeasured'}
