"""RC1 no-audio load with concurrent paged history and a verified online backup.

Engineering harness only. It starts one isolated hardware-free embedding, never
SMAppService or the operator's RC archive. Native presentation, UI response,
queue acknowledgement and pan/zoom FPS are separate unmeasured gates.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time
import uuid

from .client import Client, HTTPFailure
from .concurrent import BackgroundCandidate, BackgroundWork, restore, utc
from .config import BenchmarkError, Config
from .endurance import candidate_identity
from .runner import atomic_report, output_directory, run
from .source import encode, measurement_hash, observation, sweep


def verify_raw_page(config, rows):
    if not isinstance(rows, list) or len(rows) > 100:
        raise BenchmarkError('history_invalid', 'Unbounded original-history response')
    indices = []
    prefix = config.run_id + '-sweep-'
    for row in rows:
        identifier = row.get('id', '')
        if not isinstance(identifier, str) or not identifier.startswith(prefix):
            raise BenchmarkError('history_invalid', 'Foreign history source')
        index = int(identifier[len(prefix):])
        if not 0 <= index < config.planned_counts()['sweep'] or measurement_hash('sweep', row) != measurement_hash('sweep', sweep(config, index)):
            raise BenchmarkError('history_invalid', 'Original measurement changed in concurrent history')
        indices.append(index)
    # /sweeps retains the legacy ascending order within its newest bounded
    # page. /sweep-summaries has a separate newest-first contract.
    if indices != sorted(set(indices)):
        raise BenchmarkError('history_invalid', 'Raw history order/identity changed')
    return len(rows)


class RCHistoryAndBackup(BackgroundWork):
    def __init__(self, *args):
        super().__init__(*args)
        self.production_done = threading.Event()

    def _operations(self):
        path = self.client.session_path(self.config)
        deadline = time.monotonic() + 60
        while not self.cancelled.is_set():
            try:
                counts = self.client.json(path + '/analytics/summary')
                if counts['sweeps']['count'] > 0 and counts['observations']['count'] > 0: break
            except HTTPFailure as error:
                if error.status != 404: raise
            if time.monotonic() >= deadline: raise BenchmarkError('background_incomplete', 'No original committed source appeared')
            self.cancelled.wait(.05)
        if self.cancelled.is_set(): return
        if self.cancelled.wait(self.after): return
        self.report.update(status='running', started_at=utc(), pages=0, largest_page=0,
                           backup_running_during_history=False, history_during_production=False)
        self.node.request_backup()
        deadline = time.monotonic() + self.config.duration_seconds + self.config.bulk_timeout
        while not self.cancelled.is_set():
            before = self.node.backup_state()
            began, started_at = time.monotonic_ns(), utc()
            # A bounded raw page, not a DuckDB/Python research job. Verify every
            # returned source against the same deterministic offered generator.
            rows = self.client.json(path + '/sweeps?limit=100')
            elapsed = (time.monotonic_ns() - began)/1e6
            verify_raw_page(self.config, rows)
            after = self.node.backup_state()
            self.report['pages'] += 1
            self.report['largest_page'] = max(self.report['largest_page'], len(rows))
            self.report['history_during_production'] |= not self.production_done.is_set()
            self.report['backup_running_during_history'] |= before.get('phase') == 'running' and after.get('phase') == 'running'
            self.report['last_page'] = dict(started_at=started_at, finished_at=utc(), elapsed_ms=elapsed, sources=len(rows))
            if after.get('phase') == 'failed': raise BenchmarkError('backup_failed', 'Original online backup failed')
            if self.production_done.is_set() and after.get('phase') == 'completed':
                self.report.update(status='passed', backup=after, finished_at=utc())
                return
            if time.monotonic() >= deadline: raise BenchmarkError('background_incomplete', 'Online backup/history did not finish')
            self.cancelled.wait(.2)


def host():
    value = dict(system=platform.system(), architecture=platform.machine(), macos_version=None, memory_bytes=None)
    if platform.system() == 'Darwin':
        value['macos_version'] = subprocess.check_output(['/usr/bin/sw_vers', '-productVersion'], text=True).strip()
        value['memory_bytes'] = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.memsize'], text=True))
    return value


def run_rc(output, binary, *, smoke_seconds=None, run_id=None):
    output = output_directory(output).resolve(strict=True)
    report = dict(profile='macos-rc1', successful=False, rc1_accepted=False, hardware_tested=False,
        host=host(), errors=[], native_ui=dict(status='unmeasured', required=['reaction_p95_100ms',
            'queue_ack_p95_200ms', 'commit_to_presented_p95_250ms', 'pan_zoom_fps_30']),
        physical_eight_hour='not_run', actual_30_minute_run_observed=False,
        prior_full_modernization_load='remains_open; this profile excludes audio and does not replace it')
    node = work = None
    status = 1
    try:
        options = dict(workload_profile='macos-rc1')
        if smoke_seconds is not None:
            if not 1 <= smoke_seconds <= 60: raise BenchmarkError('invalid_config', 'Smoke must be explicit 1..60 seconds')
            options['duration_seconds'] = smoke_seconds
        initial = Config('http://127.0.0.1:1', output/'producer', run_id=run_id or 'macrc-' + uuid.uuid4().hex, **options)
        # Three physical copies can coexist: active archive, completed backup,
        # restored verification target. This fixture-only estimate uses actual
        # serialized inputs with a 2x SQLite/index allowance, not RF assumptions.
        counts = initial.planned_counts()
        source_bytes = len(encode(sweep(initial, 0))) * counts['sweep'] + len(encode(observation(initial, 0))) * counts['observation']
        needed = source_bytes * 2 * 3 + 256*1024**2
        free = shutil.disk_usage(output).free
        report['archive_budget'] = dict(estimated_peak_bytes=needed, free_bytes=free, source_bytes=source_bytes,
            basis='serialized fixture input; 2x SQLite allowance; active+backup+restore', automatic_deletion=False)
        if free < needed: raise BenchmarkError('insufficient_space', 'Isolated load archive plus backup/restore budget does not fit')
        node = BackgroundCandidate(binary.resolve(strict=True), output, None, workload_profile='macos-rc1')
        config = replace(initial, base_url=node.start())
        client = Client(config)
        caps = client.json('/api/v1/capabilities')
        candidate_identity(caps, client.json('/api/v1/health'))
        if caps.get('candidate', {}).get('history_worker_configured') is not False:
            raise BenchmarkError('invalid_profile', 'RC1 must not silently configure heavy research')
        owner = node.owner.status().get('child_pid')
        if type(owner) is not int or owner <= 0: raise BenchmarkError('candidate_unavailable', 'Missing original owned process')
        config = replace(config, pids={'owned_node_tree': owner})
        report.update(workload=config.public(), candidate_sha256=node.sha256,
                      scope='isolated synthetic replay embedding; no physical live measurements')
        work = RCHistoryAndBackup(config, node, config.duration_seconds/2)
        work.start()
        produced, producer_status = run(config)
        work.production_done.set()
        work.wait(); work.cancel(); node.close(); work.join()
        report.update(producer_report='producer/report.json', producer_status=producer_status,
                      counts=produced['counts'], background=work.report,
                      restore=restore(node, output, config.bulk_timeout))
        report['successful'] = producer_status == 0 and work.report['status'] == 'passed'
        report['actual_30_minute_run_observed'] = report['successful'] and smoke_seconds is None and produced['production']['production_elapsed_seconds'] >= 1800
        report['software_concurrency_observed'] = work.report['backup_running_during_history'] and work.report['history_during_production']
        report['macos_27_3_host_matched'] = report['host']['system'] == 'Darwin' and report['host']['architecture'] == 'arm64' and report['host']['macos_version'].split('.')[:2] == ['27', '3'] and report['host']['memory_bytes'] >= 16*1024**3
        status = 0 if report['successful'] else 1
    except Exception as error:
        report['errors'].append(dict(code=getattr(error, 'code', 'rc_fixture_failed'), message=str(error)))
    finally:
        if work is not None: work.production_done.set(); work.cancel()
        if node is not None:
            try: node.close(expect_success=False)
            except Exception as error: report['errors'].append(dict(code='cleanup_failed', message=str(error)))
            report['owned_nodes'] = node.records
        if work is not None:
            try: work.join()
            except Exception as error: report['errors'].append(dict(code='background_cleanup_failed', message=str(error)))
            report['background'] = work.report
        if report['errors']: report['successful'] = False; status = 1
        report['run_status'] = 'passed' if report['successful'] else 'failed'
        atomic_report(output, report)
    return report, status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--smoke-seconds', type=float)
    args = parser.parse_args(argv)
    _, status = run_rc(args.output_dir, args.candidate, smoke_seconds=args.smoke_seconds, run_id=args.run_id)
    return status


if __name__ == '__main__': raise SystemExit(main())
