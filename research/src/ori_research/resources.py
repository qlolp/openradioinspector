"""Bounded writes and cooperative accounting; OS hard limits remain the parent's job."""
from __future__ import annotations

from contextlib import contextmanager
import io
import os
from pathlib import Path
import time

from .protocol import WorkerError


def _windows_resident_bytes():
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    current_process = kernel32.GetCurrentProcess
    current_process.argtypes = []
    current_process.restype = ctypes.c_void_p
    memory_info = psapi.GetProcessMemoryInfo
    memory_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
    memory_info.restype = ctypes.c_int
    data = Counters()
    data.cb = ctypes.sizeof(data)
    if memory_info(current_process(), ctypes.byref(data), data.cb):
        return data.PeakWorkingSetSize
    return 0


def resident_bytes():
    try:
        if os.name == 'nt':
            value = _windows_resident_bytes()
        elif Path('/proc/self/statm').exists():
            value = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        else:
            import resource
            import sys
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            value = int(value if sys.platform == 'darwin' else value * 1024)
        if value > 0:
            return value
    except (OSError, ValueError, ImportError, AttributeError):
        pass
    raise WorkerError('rss_measurement_unavailable', 'Worker memory usage is unavailable') from None


class Budget:
    def __init__(self, limits, *, shared_io=None):
        self.limits = limits
        self.started = time.monotonic()
        self.io_started = self.started
        self.accounted_io = 0
        self.cache_used = 0
        self.shared_io = None
        if shared_io is not None:
            try:
                from radioinspector.worker_io import SharedIoBudget
                self.shared_io = SharedIoBudget(shared_io)
            except Exception:
                raise WorkerError('shared_io_unavailable','Shared cooperative I/O capability is unavailable') from None
        self.metrics = dict(source_bytes_read=0, cache_bytes_read=0, cache_bytes_written=0, max_batch_rows=0,
                            max_batch_bytes=0, peak_rss_bytes=0)

    def checkpoint(self):
        rss = resident_bytes()
        self.metrics['peak_rss_bytes'] = max(self.metrics['peak_rss_bytes'], rss)
        if rss > self.limits['worker_rss_bytes']:
            raise WorkerError('rss_limit_exceeded', 'Worker memory budget exceeded', retryable=True)

    def account_io(self, size, metric):
        self.metrics[metric] += size
        self.accounted_io += size
        if self.shared_io is not None:
            try:
                self.shared_io.account(size)
            except Exception:
                raise WorkerError('shared_io_unavailable','Shared cooperative I/O capability is unavailable') from None
            self.checkpoint()
            return
        # Smooth logical file bytes with at most one 64 KiB chunk of burst.
        delay = self.accounted_io / self.limits['io_bytes_per_second'] - (time.monotonic()-self.io_started)
        while delay > 0:
            time.sleep(min(delay, 0.05))
            self.checkpoint()
            delay = self.accounted_io / self.limits['io_bytes_per_second'] - (time.monotonic()-self.io_started)
        self.checkpoint()

    def reserve(self, size):
        if self.cache_used + size > self.limits['cache_bytes']:
            raise WorkerError('cache_quota_exceeded', 'Derived cache byte quota exceeded', retryable=True)
        self.cache_used += size

    def final_metrics(self, *, require_measurement=False):
        metrics = self.metrics.copy()
        try:
            self.metrics['peak_rss_bytes'] = max(self.metrics['peak_rss_bytes'], resident_bytes())
            metrics['peak_rss_bytes'] = self.metrics['peak_rss_bytes']
        except WorkerError as error:
            if require_measurement:
                raise
            # Error reporting must work even when the RSS probe itself failed.
            metrics['rss_measurement_error'] = error.code
            if not metrics['peak_rss_bytes']:
                metrics['peak_rss_bytes'] = None
        return metrics | {'elapsed_ms': round((time.monotonic()-self.started)*1000),
            'cache_bytes': self.cache_used,
            'cross_process_logical_io': self.shared_io is not None,
            'hard_limits_require_parent': ['process_tree_memory', 'physical_io_rate', 'filesystem_quotas'],
            'limits': self.limits}


class QuotaFile(io.RawIOBase):
    def __init__(self, path, budget):
        self._file = path.open('xb', buffering=0)
        path.chmod(0o600)
        self.budget = budget

    def writable(self):
        return True

    def tell(self):
        return self._file.tell()

    def write(self, data):
        # Arrow may emit a large row group in a single call. Split so both
        # quota admission and pacing remain bounded before each actual write.
        total = 0
        view = memoryview(data)
        for offset in range(0, len(view), 65_536):
            block = view[offset:offset+65_536]
            self.budget.reserve(len(block))
            self.budget.account_io(len(block), 'cache_bytes_written')
            while block:
                size = self._file.write(block)
                total += size
                block = block[size:]
        return total

    def flush(self):
        if not self._file.closed:
            self._file.flush()

    def close(self):
        if not self.closed:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            finally:
                super().close()
                self._file.close()


@contextmanager
def exclusive_lock(root):
    path = root / '.worker.lock'
    if path.is_symlink():
        raise WorkerError('unsafe_cache', 'Cache contains a linked lock file')
    with path.open('a+b') as lock:
        path.chmod(0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WorkerError('cache_busy', 'Another heavy job owns this cache', retryable=True) from None
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
