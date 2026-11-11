"""Owned IPC fixture; no files, ports, source archives or scientific libraries."""
import json
import os
import sys

body = json.loads(sys.stdin.buffer.read(16*1024*1024+1))
if body.get('payload') == 'expected-error':
    print(json.dumps(dict(version=1,request_id=body['request_id'],operation=body['operation'],ok=False,
                         error=dict(code='validation_error',api_code='invalid_request',http_status=400))))
    sys.exit(1)
result = body.get('payload')
if result == 'environment':
    result = dict(
        inherited_private_keys=[key for key in ('ORI_RUNTIME_CONTROL_TOKEN', 'HOME', 'PYTHONPATH',
                                               'ORI_WORKER_ENV_TEST_CHILD') if key in os.environ],
        path=os.environ.get('PATH'), timezone=os.environ.get('TZ'), locale=os.environ.get('LC_ALL'),
        no_user_site=os.environ.get('PYTHONNOUSERSITE'), utf8=os.environ.get('PYTHONUTF8'),
        threads={key:os.environ.get(key) for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
            'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'ARROW_NUM_THREADS')})
print(json.dumps(dict(version=1, request_id=body['request_id'], operation=body['operation'],
                      ok=True, result=result), allow_nan=False))
