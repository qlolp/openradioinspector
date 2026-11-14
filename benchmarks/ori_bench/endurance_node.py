"""Real replay Core CLI under the existing release owner; no HTTP emulator."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from .config import BenchmarkError

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        while block:=source.read(1024*1024):value.update(block)
    return value.hexdigest()


def publish_stop(path,nonce):
    """Publish complete bytes atomically without replacing any existing path."""
    descriptor,temporary = tempfile.mkstemp(prefix='.stop-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as output:
            output.write(nonce.encode('ascii'));output.flush();os.fsync(output.fileno())
        os.link(temporary,path)
    finally:
        os.unlink(temporary)


class OwnedCandidate:
    """Reuse a fresh archive only after original owner wait/release proves JOIN."""
    def __init__(self,binary,output):
        self.binary,self.output = binary,output
        self.sha256 = digest(binary)
        self.owner = None
        self.records = []
        self.failed = False
        self.registry = output/'process-registry'
        self.registry.mkdir(mode=0o700)

    def unchanged(self):
        if digest(self.binary)!=self.sha256:
            self.failed = True
            raise BenchmarkError('candidate_changed','Requested immutable candidate binary changed')

    def _adapter_command(self, data):
        """Trusted fixture subclasses keep this same process owner/lifeline."""
        return [sys.executable, '-B', '-m', 'benchmarks.ori_bench.endurance_node',
                str(self.binary), str(data), str(self.ready), str(self.stop), self.nonce]

    def start(self):
        if self.owner is not None or self.failed:
            raise BenchmarkError('cleanup_failed','Previous owned candidate has not closed successfully')
        self.unchanged()
        from tests.release_process_ownership import OwnedCommand
        phase = self.output/('node-'+str(len(self.records)))
        phase.mkdir(mode=0o700)
        data = self.output/'data'
        if not self.records:data.mkdir(mode=0o700)
        self.ready,self.stop = phase/'ready',phase/'stop.request'
        self.nonce = uuid.uuid4().hex
        self.log = (phase/'process.log').open('xb')
        environment = {key:value for key,value in os.environ.items()
                       if not key.startswith(('ORI_RUNTIME_','ORI_CORE_'))}
        environment.update(PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1',
                           OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        command = self._adapter_command(data)
        self.current = dict(epoch=len(self.records),joined=False,graceful=False)
        self.records.append(self.current)
        try:
            self.owner = OwnedCommand.start(command,environment=environment,registry=self.registry,
                                            root=ROOT,log=self.log,shutdown_timeout=15)
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                state = self.owner.status()
                if state and state['phase'] in ('exited','failed','quiescent','released'):
                    raise BenchmarkError('candidate_unavailable','Candidate exited before actual readiness')
                if (phase/'process.log').stat().st_size>1024*1024:
                    raise BenchmarkError('candidate_output_overflow','Candidate log exceeds the bounded budget')
                if self.ready.is_file() and not self.ready.is_symlink():
                    if self.ready.stat().st_size>1024:
                        raise BenchmarkError('candidate_unavailable','Invalid bounded readiness evidence')
                    url = self.ready.read_text(encoding='utf-8').strip()
                    parsed = urlsplit(url)
                    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port \
                            or parsed.path or parsed.query or parsed.fragment or parsed.username:
                        raise BenchmarkError('candidate_unavailable','Readiness is not the owned loopback listener')
                    self.current['ready_monotonic'] = time.monotonic()
                    return url
                time.sleep(.01)
            raise BenchmarkError('candidate_unavailable','Actual candidate readiness deadline exceeded')
        except BaseException:
            self.failed = True
            self.close(expect_success=False)
            raise

    def close(self,*,expect_success=True):
        if self.owner is None:
            if hasattr(self,'log'):self.log.close()
            return
        try:
            publish_stop(self.stop,self.nonce)
            code = self.owner.wait(20)
            self.owner.release(15)
            self.current.update(joined=True,exit_code=code,stopped_monotonic=time.monotonic(),
                                graceful=code==0 and not self.ready.exists())
            self.owner = None
            self.unchanged()
            if expect_success and not self.current['graceful']:
                raise BenchmarkError('candidate_close_failed','Candidate did not prove graceful shutdown')
        except BaseException as error:
            self.failed = True
            if self.owner is not None:
                # Uses retained numeric authority; never signal a PID read from
                # HTTP/readiness, infer success, or remove the owned archive.
                try:
                    self.owner.stop(15)
                    self.current.update(joined=True,forced_cleanup=True)
                    self.owner = None
                except BaseException:
                    self.current['cleanup_failed'] = True
                    raise BenchmarkError('cleanup_failed','Original owner retained unconfirmed child cleanup') from error
            raise
        finally:
            self.log.close()


def requested(path, nonce):
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or path.stat().st_size != 32 \
            or path.read_bytes() != nonce.encode('ascii'):
        raise ValueError('Unknown private owner request')
    return True


def adapter(binary,data,ready,stop,nonce,*,fixture_profile=None):
    """A direct child in the held owner session; stdin EOF drives Core close."""
    stopping = False
    def request_stop(*_args):
        nonlocal stopping
        stopping = True
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,request_stop)
    command = [str(binary),'--mode','replay','--data-dir',str(data),
               '--host','127.0.0.1','--port','0','--stdin-lifeline','--ready-file',str(ready)]
    if fixture_profile is not None:
        command = [str(binary), 'serve', str(fixture_profile)]
    child = subprocess.Popen(command,stdin=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    backup_requested = False
    try:
        while child.poll() is None and not stopping:
            if requested(stop, nonce):
                break
            if fixture_profile is not None and not backup_requested \
                    and requested(ready.parent/'backup.request', nonce):
                child.stdin.write(b'B');child.stdin.flush()
                backup_requested = True
            time.sleep(.01)
    finally:
        child.stdin.close()
        # A timeout propagates to the original owner's verified cleanup. This
        # adapter never releases ownership or reports an unknown child as joined.
        child.wait(timeout=15)
    return child.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path);parser.add_argument('data',type=Path)
    parser.add_argument('ready',type=Path);parser.add_argument('stop',type=Path)
    parser.add_argument('nonce')
    parser.add_argument('--fixture-profile', type=Path)
    args = parser.parse_args()
    if len(args.nonce)!=32 or any(c not in '0123456789abcdef' for c in args.nonce):return 2
    return adapter(args.binary,args.data,args.ready,args.stop,args.nonce,fixture_profile=args.fixture_profile)


if __name__=='__main__':sys.exit(main())
