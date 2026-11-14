"""Private POSIX cooperative worker I/O capability. No canonical writes or cleanup."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time

RATE = 20 * 1024 * 1024
MAX_CHARGE = 8 * 1024 * 1024
MAX_DEBT_NS = 30_000_000_000
MAGIC = b'ORIIOV1\0'


class SharedIoError(Exception):
    def __init__(self):
        super().__init__('Shared cooperative I/O capability is unavailable')


def _now():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class SharedIoBudget:
    def __init__(self, capability):
        self._fd = None
        self._failed = False
        self._total = 0
        self._mutex = threading.Lock()
        try:
            if os.name!='posix' or not hasattr(time,'CLOCK_MONOTONIC') \
                    or not isinstance(capability,dict) or set(capability)!={'version','path','nonce','clock'} \
                    or type(capability['version']) is not int or capability['version']!=1 \
                    or capability['clock']!='posix-monotonic' \
                    or not isinstance(capability['nonce'],str) or not re.fullmatch('[0-9a-f]{32}',capability['nonce']):
                raise SharedIoError()
            self._path = Path(capability['path'])
            if not self._path.is_absolute() or '..' in self._path.parts:
                raise SharedIoError()
            for ancestor in (self._path,*self._path.parents):
                if ancestor.is_symlink():
                    raise SharedIoError()
            self._nonce = bytes.fromhex(capability['nonce'])
            self._fd = os.open(self._path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
            opened = os.fstat(self._fd)
            self._identity = (opened.st_dev,opened.st_ino)
            directory = self._path.parent.lstat()
            self._directory_identity = (directory.st_dev,directory.st_ino)
            self._validate()
            import fcntl
            self._flock = fcntl
            # Reject a wrong nonce or corrupt state before worker imports or
            # cache construction. A zero-byte reservation never refunds debt.
            deadline = _now()+2_000_000_000
            while self._reserve(0) is None:
                if _now()>=deadline:
                    raise SharedIoError()
                time.sleep(0.01)
        except (OSError,ValueError,TypeError,KeyError,AttributeError,ImportError,SharedIoError):
            self.close()
            raise SharedIoError() from None

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self):
        self.close()

    def _validate(self):
        metadata = self._path.lstat()
        opened = os.fstat(self._fd)
        directory = self._path.parent.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 \
                or metadata.st_uid!=os.geteuid() or stat.S_IMODE(metadata.st_mode)!=0o600 \
                or metadata.st_size!=128 or (metadata.st_dev,metadata.st_ino)!=self._identity \
                or (opened.st_dev,opened.st_ino)!=self._identity \
                or not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=os.geteuid() \
                or stat.S_IMODE(directory.st_mode)!=0o700 \
                or (directory.st_dev,directory.st_ino)!=self._directory_identity:
            raise SharedIoError()

    def _reserve(self, size):
        if not self._mutex.acquire(blocking=False):
            return None
        locked = False
        try:
            try:
                self._flock.flock(self._fd,self._flock.LOCK_EX|self._flock.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EWOULDBLOCK,errno.EAGAIN):
                    return None
                raise
            locked = True
            self._validate()
            os.lseek(self._fd,0,os.SEEK_SET)
            raw = bytearray(os.read(self._fd,128))
            if len(raw)!=128:
                raise SharedIoError()
            rate,next_ns,total,created,clock = struct.unpack_from('<5Q',raw,24)
            now = _now()
            if raw[:8]!=MAGIC or raw[8:24]!=self._nonce or rate!=RATE or clock!=1 \
                    or any(raw[64:]) or not 0<created<=now or next_ns<created \
                    or next_ns>now+MAX_DEBT_NS or not self._total<=total<=2**63-1:
                raise SharedIoError()
            until = max(now,next_ns)+(size*1_000_000_000+RATE-1)//RATE
            total += size
            if until>now+MAX_DEBT_NS or total>2**63-1:
                raise SharedIoError()
            struct.pack_into('<2Q',raw,32,until,total)
            os.lseek(self._fd,0,os.SEEK_SET)
            if os.write(self._fd,raw)!=128:
                raise SharedIoError()
            self._total = total
            return until
        finally:
            if locked:
                self._flock.flock(self._fd,self._flock.LOCK_UN)
            self._mutex.release()

    def account(self, size, *, cancelled=None, deadline_ns=None):
        if type(size) is not int or not 0<=size<=MAX_CHARGE or self._failed:
            raise SharedIoError()
        lock_deadline = _now()+2_000_000_000
        try:
            while True:
                if cancelled is not None and cancelled():
                    raise SharedIoError()
                if deadline_ns is not None and _now()>=deadline_ns:
                    raise SharedIoError()
                until = self._reserve(size)
                if until is not None:
                    break
                if _now()>=lock_deadline:
                    raise SharedIoError()
                time.sleep(0.01)
            while True:
                if cancelled is not None and cancelled():
                    raise SharedIoError()
                now = _now()
                if deadline_ns is not None and now>=deadline_ns:
                    raise SharedIoError()
                if now>=until:
                    return
                time.sleep(min((until-now)/1_000_000_000,0.01))
        except (OSError,ValueError,TypeError,OverflowError,SharedIoError):
            self._failed = True
            raise SharedIoError() from None
