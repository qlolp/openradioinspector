"""Bounded Linux probe children; no shell and no inherited credentials."""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    code: int
    stdout: bytes
    stderr: bytes


def environment(release: Path, cwd: Path, role: str | None = None):
    for name in ('home','tmp'):(cwd/name).mkdir(mode=0o700,exist_ok=True)
    library=[release/'lib']
    if role:library.insert(0,release/'runtimes'/role/'lib')
    return {'PATH':'/usr/bin:/bin','HOME':str(cwd/'home'),'TMPDIR':str(cwd/'tmp'),
            'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TZ':'UTC',
            'LD_LIBRARY_PATH':':'.join(map(str,library)),
            **{name:'1' for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS',
                                    'NUMEXPR_NUM_THREADS','VECLIB_MAXIMUM_THREADS','ARROW_NUM_THREADS')}}


def exited(process):
    # Keep the owned leader unreaped until group cleanup, so its PGID cannot be
    # recycled while we may still signal it. Linux is this tool's target.
    return os.waitid(os.P_PID,process.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT) is not None


def live_group(group):
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():continue
        try:text=(entry/'stat').read_text()
        except FileNotFoundError:continue
        fields=text[text.rfind(')')+2:].split()
        if len(fields)<3:raise ProbeError('owned_process_cleanup_unknown')
        if int(fields[2])==group and fields[0] not in ('Z','X'):return True
    return False


def stop_owned(process, timeout=3.0):
    # Only the group returned by our start_new_session Popen is ever signalled.
    for signum in (signal.SIGTERM,signal.SIGKILL):
        try:os.killpg(process.pid,signum)
        except ProcessLookupError:pass
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if exited(process) and not live_group(process.pid):
                process.wait();return
            time.sleep(.01)
    raise ProbeError('owned_process_cleanup_failed')


def run(argv, *, cwd: Path, env: dict, timeout=30.0, output_limit=2*1024*1024):
    if not argv or not Path(argv[0]).is_absolute() or timeout<=0:
        raise ProbeError('invalid_probe_argv')
    process=subprocess.Popen(list(map(str,argv)),cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             shell=False,start_new_session=True,close_fds=True)
    buffers={process.stdout:bytearray(),process.stderr:bytearray()}
    selector=selectors.DefaultSelector()
    try:
        for stream in buffers:
            os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
        deadline=time.monotonic()+timeout;size=0
        while selector.get_map() or not exited(process):
            if time.monotonic()>=deadline:raise ProbeError('probe_timeout')
            for key,_ in selector.select(min(.05,max(0,deadline-time.monotonic()))):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk:selector.unregister(key.fileobj);continue
                size+=len(chunk)
                if size>output_limit:raise ProbeError('probe_output_limit')
                buffers[key.fileobj].extend(chunk)
        if live_group(process.pid):raise ProbeError('probe_left_descendants')
        code=process.wait()
        return ProbeResult(code,bytes(buffers[process.stdout]),bytes(buffers[process.stderr]))
    except BaseException:
        stop_owned(process)
        raise
    finally:
        selector.close()
        for stream in buffers:stream.close()
