"""Retain one release Job across benchmark startup, observation and verified shutdown."""
from __future__ import annotations

import json
import subprocess
import sys

from .config import BenchmarkError


class WindowsOwnedCommand:
    def __init__(self, process, job):
        self.process = process
        self.job = job

    @classmethod
    def start(cls, command, *, environment, root, log, shutdown_timeout, registry=None):
        from tests.run_release_checks import WindowsJob, _process_options, _stop_command

        job = WindowsJob()
        process = None
        assigned = False
        try:
            # The release guard reads "go" before it can create a single child.
            guard = [sys.executable, '-m', 'tests.run_release_checks', '--_supervise-command', json.dumps(command)]
            process = subprocess.Popen(guard, cwd=root, env=environment, stdin=subprocess.PIPE,
                                       stdout=log, stderr=subprocess.STDOUT, **_process_options())
            job.assign(process)
            assigned = True
            process.stdin.write(b'go\n')
            process.stdin.close()
            return cls(process, job)
        except BaseException:
            try:
                if process is not None:
                    if assigned:
                        _stop_command(process, shutdown_timeout, job)
                    else:
                        # No admission byte was sent. Only this direct guard can exist.
                        process.stdin.close()
                        try:
                            process.wait(timeout=shutdown_timeout)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=shutdown_timeout)
            finally:
                if process is not None and not process.stdin.closed:
                    process.stdin.close()
                job.close()
            raise

    def status(self):
        return {'phase': 'running' if self.process.poll() is None else 'exited',
                'child_pid': self.process.pid}

    def wait(self, timeout):
        return self.process.wait(timeout=timeout)

    def release(self, timeout):
        from tests.run_release_checks import _job_still_active

        if self.process.poll() is None or _job_still_active(self.job, timeout):
            raise BenchmarkError('cleanup_failed', 'An original Windows Job member survived graceful shutdown')
        self.job.close()

    def stop(self, timeout):
        from tests.run_release_checks import _stop_command

        # _stop_command waits for the guard and for zero active original Job members.
        _stop_command(self.process, timeout, self.job)
        self.job.close()

    def core_memory(self, binary):
        """Query the original Job member's lifetime working-set high water mark."""
        import ctypes
        import os
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('faults', wintypes.DWORD)]
            _fields_ += [(name, ctypes.c_size_t) for name in
                         ('peak_working_set', 'working_set', 'peak_paged', 'paged',
                          'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]

        api = self.job.api
        query_memory = api.K32GetProcessMemoryInfo
        query_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        query_memory.restype = wintypes.BOOL
        members = self.job.ProcessList()
        if not api.QueryInformationJobObject(self.job.handle, 3, ctypes.byref(members), ctypes.sizeof(members), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not 0 < members.listed == members.assigned <= 32:
            raise BenchmarkError('resource_unmeasured', 'Incomplete original Windows Job membership')
        found = []
        for pid in members.pids[:members.listed]:
            handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                member = ctypes.c_int32()
                if not api.IsProcessInJob(handle, self.job.handle, ctypes.byref(member)) or not member.value:
                    raise BenchmarkError('resource_unmeasured', 'Original Windows Job membership changed during sampling')
                name, length = ctypes.create_unicode_buffer(32768), ctypes.c_uint32(32768)
                if not api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if os.path.samefile(name.value, binary):
                    memory = MemoryCounters()
                    memory.size = ctypes.sizeof(memory)
                    if not query_memory(handle, ctypes.byref(memory), memory.size):
                        raise ctypes.WinError(ctypes.get_last_error())
                    found.append(dict(peak_working_set_bytes=memory.peak_working_set,
                                      working_set_bytes=memory.working_set))
            finally:
                if not api.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
        if len(found) != 1:
            raise BenchmarkError('resource_unmeasured', 'Expected one running Core image in the original Windows Job')
        return dict(status='measured', scope='one owned benchmark Core', method='GetProcessMemoryInfo',
                    job_processes=members.listed, **found[0])
