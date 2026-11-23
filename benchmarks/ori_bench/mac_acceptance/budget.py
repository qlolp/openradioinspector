"""Bounded deadlines shared with the private packaged-core acceptance policy."""
import math
from time import monotonic,monotonic_ns,sleep
from pathlib import Path

from ..concurrent import bounded_json
from ..config import BenchmarkError

RATE = 20 * 1024**2
MAX_SNAPSHOT = 16 * 1024**3


def sample_duty_storage(fetch):
    """A cold core needs two allocation readings at least five seconds apart."""
    for _ in range(3):
        report = fetch()
        if report.get('forecast_status') != 'measuring_allocation' or report.get('next_recheck_seconds') != 2:
            return report
        sleep(2)
    return fetch()


def require_duty_storage(storage):
    """Check the core's full local-volume forecast before starting physical duty."""
    def measured(key, minimum):
        value = storage.get(key)
        return type(value) in (int, float) and math.isfinite(value) and value >= minimum

    def byte_count(key):
        value = storage.get(key)
        return type(value) is int and 0 <= value < 2**64

    complete = (
        storage.get('ready') is True and storage.get('storage_id') == 'node-archive'
        and storage.get('backup_reserve_requested') is True and storage.get('restore_reserve_requested') is True
        and all(byte_count(key) for key in ('required_free_bytes', 'filesystem_free_bytes', 'shortfall_bytes',
                                            'backup_reserve_bytes', 'restore_reserve_bytes'))
        and storage['required_free_bytes'] > 0 and storage['filesystem_free_bytes'] >= storage['required_free_bytes']
        and storage['shortfall_bytes'] == 0 and min(storage['backup_reserve_bytes'], storage['restore_reserve_bytes']) > 0
        and measured('bytes_per_second', 0) and storage['bytes_per_second'] > 0
        and measured('physical_growth_bytes_per_second', 0)
        and measured('forecast_seconds', 43_200) and measured('estimated_retention_seconds', 43_200)
    )
    if not complete:
        raise BenchmarkError('insufficient_space', 'Measured 8h +50% storage for recording, backup and restore is required')


def deadlines(size):
    if type(size) is not int or not 0 < size <= MAX_SNAPSHOT:
        raise BenchmarkError('invalid_evidence', 'Recovery snapshot size is outside the admitted quota')
    return dict(snapshot_bytes=size, rate_bytes_per_second=RATE,
                copy_timeout_seconds=max(300, (3*size + RATE-1)//RATE + 60),
                total_timeout_seconds=max(600, (9*size + RATE-1)//RATE + 120))


def recovery_timeout(manifest):
    try:
        size = manifest['archive']['bytes']
    except (KeyError, TypeError) as error:
        raise BenchmarkError('invalid_evidence', 'Missing sealed recovery snapshot size') from error
    return deadlines(size)['total_timeout_seconds']


def wait_for_backup(receipts, *, final=False, owner=None):
    root = Path(receipts)
    name = 'final-backup' if final else 'backup'
    start_deadline = monotonic() + 60
    while True:
        state = None
        for suffix in ('finished', 'started'):
            path = root / (name + '-' + suffix + '.json')
            if path.exists() or path.is_symlink():
                state = bounded_json(path)
                break
        if state is not None:
            if state.get('phase') in ('completed', 'failed'):
                return state
            budget = state.get('budget')
            if state.get('phase') != 'running' or not isinstance(budget, dict):
                raise BenchmarkError('invalid_evidence', 'Backup did not publish an admitted deadline')
            expected = deadlines(budget.get('snapshot_bytes'))
            if any(type(budget.get(key)) is not int or budget[key] != expected[key]
                   for key in ('copy_timeout_seconds', 'total_timeout_seconds')):
                raise BenchmarkError('invalid_evidence', 'Backup deadline differs from the measured-size policy')
            stamp, now = state.get('at_ns'), monotonic_ns()
            if type(stamp) is not int or not 0 < stamp <= now:
                raise BenchmarkError('invalid_evidence', 'Backup admission clock is invalid')
            if now >= stamp + (expected['total_timeout_seconds'] + 30) * 1_000_000_000:
                raise BenchmarkError('backup_timeout', 'Owner-admitted backup deadline and cleanup allowance expired')
        elif monotonic() >= start_deadline:
            raise BenchmarkError('backup_timeout', 'Backup owner did not admit the request within60seconds')
        if owner is not None:
            status = owner.status()
            if status and status.get('phase') in ('failed', 'exited', 'released', 'quiescent'):
                raise BenchmarkError('backup_failed', 'Backup owner exited before its completion receipt')
        sleep(.2)
