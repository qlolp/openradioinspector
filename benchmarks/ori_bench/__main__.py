from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import BenchmarkError,Config
from .runner import run
from .source import encode


def main(argv=None):
    parser=argparse.ArgumentParser(description='Synthetic offered-input HTTP benchmark; no instrument commands')
    parser.add_argument('mode',choices=('run','history'))
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--endpoint-label',default='explicit-http-endpoint')
    parser.add_argument('--scenario',choices=('stress','overload','fault','lifecycle'),default='stress')
    parser.add_argument('--duration-seconds',type=float)
    parser.add_argument('--sweep-rate',type=int)
    parser.add_argument('--observation-rate',type=int,default=10)
    parser.add_argument('--points',type=int)
    parser.add_argument('--audio-segment-seconds',type=float,default=10)
    parser.add_argument('--speed',type=float)
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--queue-capacity',type=int,default=64)
    parser.add_argument('--request-timeout',type=float,default=10)
    parser.add_argument('--drain-seconds',type=float,default=60)
    parser.add_argument('--history-repeats',type=int,default=20)
    parser.add_argument('--sweeps',type=int,default=1000000)
    parser.add_argument('--batch-size',type=int,default=16)
    parser.add_argument('--allow-million',action='store_true')
    parser.add_argument('--fault-kind',choices=('invalid-batch','client-gap'),default='invalid-batch')
    parser.add_argument('--fault-every',type=int,default=10)
    parser.add_argument('--lifecycle-step-seconds',type=float,default=3600)
    parser.add_argument('--sample-interval',type=float,default=.5)
    parser.add_argument('--pid',action='append',default=[],help='Process tree root, label=PID; repeatable')
    parser.add_argument('--render-events')
    args=vars(parser.parse_args(argv))
    try:
        args['pids']={label:int(pid) for label,pid in (value.split('=',1) for value in args.pop('pid'))}
        if args['run_id'] is None:args.pop('run_id')
        if args['duration_seconds'] is None:args['duration_seconds']=604800 if args['scenario']=='lifecycle' else 30 if args['scenario']=='overload' else 1800
        if args['sweep_rate'] is None:args['sweep_rate']=1000 if args['scenario']=='overload' else 10
        if args['points'] is None:args['points']=290 if args['mode']=='history' else 4096
        if args['speed'] is None:args['speed']=86400 if args['scenario']=='lifecycle' else 1
        config=Config(**args)
        report,status=run(config)
        print(encode({'report':str(Path(config.output_dir).absolute()/'report.json'),
            'run_status':report['run_status'],'errors':report['errors']}).decode())
        return status
    except (BenchmarkError,ValueError) as error:
        print(encode({'run_status':'failed','error':{'code':getattr(error,'code','invalid_config'),
            'message':getattr(error,'message','Invalid command-line value')}}).decode())
        return 2


if __name__=='__main__':sys.exit(main())
