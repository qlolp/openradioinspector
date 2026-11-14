"""Only temporary synthetic process/filesystem I/O, no devices or canonical DB."""
import json
import os
import signal
import subprocess
import sys
import time

request = json.load(sys.stdin)
if request['operation'] == 'sleep':
    time.sleep(60)
if request['operation'] == 'fork':
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    with open(request['pid_file'], 'w') as f:
        f.write(str(child.pid))
    time.sleep(60)
if request['operation'] == 'hold_pipes':
    # The test owns this bounded helper via a release file and watchdog. It
    # deliberately keeps inherited pipes open after the leader is reaped.
    code = '''import pathlib,sys,time
ready,release=map(pathlib.Path,sys.argv[1:])
ready.write_text('ready')
deadline=time.monotonic()+5
while not release.exists() and time.monotonic()<deadline:time.sleep(.01)
'''
    subprocess.Popen([sys.executable,'-c',code,request['ready_file'],request['release_file']],
                     start_new_session=True)
    time.sleep(60)
if request['operation'] == 'huge':
    sys.stdout.write('x' * (3 * 1024 * 1024))
else:
    print(json.dumps(dict(version=1,request_id=request['request_id'],ok=True,result=request['limits'],metrics={})))
