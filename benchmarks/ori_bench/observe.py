"""Correlate explicit same-host after-draw evidence; never infer missing UI timings."""
from __future__ import annotations

import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import re
import socket
import sys

from .config import BenchmarkError,Config
from .ledger import Ledger
from .metrics import Histogram

MAX_OBSERVER_BYTES=64*1024**2


def records(path):
    with path.open('rb') as source:
        while raw:=source.readline(65_537):
            if len(raw)>65_536:raise BenchmarkError('observer_invalid','Observer record exceeds 64 KiB')
            yield raw


def correlate(path,config,ledger,histogram):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_OBSERVER_BYTES:
        raise BenchmarkError('observer_invalid','Observer evidence is missing or exceeds 64 MiB')
    host_sha=hashlib.sha256(socket.gethostname().encode()).hexdigest()
    metadata=None;summary=None;observed=0;duplicates=0;digest=hashlib.sha256()
    for raw in records(path):
        digest.update(raw)
        try:row=json.loads(raw)
        except (ValueError,RecursionError):raise BenchmarkError('observer_invalid','Invalid observer record') from None
        if not isinstance(row,dict):raise BenchmarkError('observer_invalid','Observer record must be an object')
        if metadata is None:
            metadata=row
            if row.get('kind')!='observer_metadata' or type(row.get('version')) is not int or row.get('version')!=1 \
                    or row.get('clock')!='unix-wall-ns' or row.get('host_sha256')!=host_sha \
                    or row.get('draw_contract')!='data-sweep-id-after-draw' \
                    or type(row.get('clients')) is not int or not 1<=row['clients']<=8:
                raise BenchmarkError('observer_invalid','Observer clock, host, or after-draw contract is unverified')
            continue
        if row.get('kind')=='observer_summary':
            if summary is not None or type(row.get('dropped')) is not int or row['dropped']<0:
                raise BenchmarkError('observer_invalid','Invalid observer completion record')
            summary=row;continue
        if summary is not None or row.get('kind')!='render' or row.get('client') not in {str(i) for i in range(metadata['clients'])}:
            raise BenchmarkError('observer_invalid','Unexpected observer record or client')
        if not isinstance(row.get('id'),str) or len(row['id'])>128 \
                or not isinstance(row.get('rendered_wall_ns'),str) or not re.fullmatch(r'[0-9]{1,19}',row['rendered_wall_ns']):
            raise BenchmarkError('observer_invalid','Invalid observer source ID or clock')
        source=ledger.get(row['id'])
        if source is None or source['kind']!='sweep' or not source['verified']:
            raise BenchmarkError('observer_invalid','Observer identifies an unknown or unverified sweep')
        try:rendered=int(row['rendered_wall_ns'])
        except (ValueError,TypeError,KeyError):raise BenchmarkError('observer_invalid','Invalid render clock value') from None
        latency=(rendered-source['offered_wall_ns'])/1_000_000
        if latency<0 or rendered>2**63-1:raise BenchmarkError('observer_invalid','Render clock precedes the offer or exceeds int64')
        with ledger.db:
            changed=ledger.db.execute('INSERT OR IGNORE INTO rendered VALUES (?,?,?)',
                                     (source['id'],row['client'],rendered)).rowcount
        if changed:histogram.add(latency);observed+=1
        else:duplicates+=1
    if metadata is None:raise BenchmarkError('observer_invalid','Observer metadata is missing')
    expected=ledger.counts()['sweep']['offered']*metadata['clients']
    metric=histogram.report()
    passed=bool(expected and summary is not None and not summary['dropped'] and not summary.get('failed')
                and summary.get('records')==observed+duplicates and observed==expected
                and metric['p95_upper_ms']<=250)
    return {'status':'passed' if passed else 'failed','metric':'offered-to-render conservative upper bound',
        'committed_to_render_measured':False,'clock':'same-host unix wall clock',
        'clients':metadata['clients'],'observed_unique_renders':observed,'expected_renders':expected,
        'duplicate_markers':duplicates,'observer_completed':summary is not None,
        'observer_dropped':summary['dropped'] if summary else None,'p95_upper_ms':metric['p95_upper_ms'],
        'target_ms':250,'observer_sha256':digest.hexdigest(),
        'requires_host_contract':'data-sweep-id is updated only after the matching draw completes'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',type=Path,required=True)
    parser.add_argument('--render-events',type=Path,required=True)
    args=parser.parse_args(argv)
    ledger=None
    try:
        root=args.report_dir.resolve(strict=True)
        if json.loads((root/'owner.json').read_bytes())!={'owner':'ori-benchmarks','version':1}:
            raise BenchmarkError('unsafe_output','Directory is not an owned benchmark report')
        report=json.loads((root/'report.json').read_bytes())
        allowed={field.name for field in fields(Config)}-{'base_url','output_dir'}
        config=Config(base_url=report['endpoint']['origin'],output_dir=root,
                      **{key:value for key,value in report['workload'].items() if key in allowed})
        ledger=Ledger(root/'offers.sqlite',existing=True)
        ledger.db.execute('DELETE FROM rendered');ledger.db.commit()
        histogram=Histogram()
        result=correlate(args.render_events,config,ledger,histogram)
        report['checks']['render_latency']=result
        report['latency']['offered_to_render_ms']=histogram.report()
        from .runner import atomic_report
        atomic_report(root,report)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['status']=='passed' else 1
    except (BenchmarkError,OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','code':getattr(error,'code','observer_invalid'),
                          'message':getattr(error,'message','Observer evidence could not be attached')}))
        return 2
    finally:
        if ledger is not None:ledger.close()


if __name__=='__main__':sys.exit(main())
