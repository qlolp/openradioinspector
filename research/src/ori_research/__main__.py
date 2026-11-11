"""Bounded JSON stdin/stdout IPC. Diagnostic errors never echo source records."""
from __future__ import annotations

import json
import os
import sys

from .protocol import (DEFAULT_LIMITS, REQUEST_BYTES, RESPONSE_BYTES, WorkerError,
                       encode, parse)
from .resources import Budget


def main():
    # Set before the heavy scientific runtime is imported. The parent should
    # also set these in its sanitized child environment.
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS','VECLIB_MAXIMUM_THREADS','ARROW_NUM_THREADS'):
        os.environ[name] = '1'
    raw = sys.stdin.buffer.read(REQUEST_BYTES+1)
    request_id,operation = None,None
    budget = Budget(DEFAULT_LIMITS)
    try:
        # Retain a valid correlation ID even when another field is invalid.
        if len(raw) <= REQUEST_BYTES:
            try:
                tentative = json.loads(raw)
                identifier = tentative.get('request_id') if isinstance(tentative,dict) else None
                if isinstance(identifier,str) and len(identifier.encode('utf-8')) <= 128 \
                        and all(ord(c)>=32 for c in identifier):
                    request_id = identifier
            except (ValueError,TypeError,UnicodeError,RecursionError):
                pass
        request = parse(raw)
        request_id,operation = request['request_id'],request['operation']
        budget = Budget(request['limits'])
        from .worker import run
        result = run(request,budget)
        response = {'version':1,'request_id':request_id,'operation':operation,
                    'ok':True,'result':result,'metrics':budget.final_metrics()}
        encoded = encode(response)
        if len(encoded) > RESPONSE_BYTES:
            raise WorkerError('response_too_large', 'Result exceeds the bounded response budget')
        status = 0
    except WorkerError as error:
        response = {'version':1,'request_id':request_id,'ok':False,
            'error':{'code':error.code,'message':error.message,'retryable':error.retryable},
            'metrics':budget.final_metrics()}
        encoded,status = encode(response),2
    except (Exception, MemoryError):
        response = {'version':1,'request_id':request_id,'ok':False,
            'error':{'code':'internal_error','message':'Worker failed; no source data was changed',
                     'retryable':False},'metrics':budget.final_metrics()}
        encoded,status = encode(response),3
    sys.stdout.buffer.write(encoded+b'\n')
    sys.stdout.buffer.flush()
    return status


if __name__ == '__main__':
    raise SystemExit(main())
