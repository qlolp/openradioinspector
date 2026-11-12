"""One replay Core, unchanged producer/three clients, and owned background work."""
from __future__ import annotations

import argparse
import calendar
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from .client import Client, HTTPFailure, segment
from .concurrent_proof import ExportProof, MAX_FRAME_BYTES, overlap, reference_config
from .config import BenchmarkError
from .endurance import candidate_identity
from .endurance_node import OwnedCandidate, ROOT, digest, publish_stop
from .runner import atomic_report, output_directory, run
from .source import BASE, encode
from tools.modernization.api_parity import load_json


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def bounded_json(path, maximum=65536):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise BenchmarkError('invalid_receipt', 'Owned receipt is missing, linked, or too large')
    with path.open('rb') as source:
        return load_json(source.read(maximum + 1), maximum)


class BackgroundCandidate(OwnedCandidate):
    def __init__(self, binary, output, python, *, cgroup=None, best_effort=False):
        if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK) \
                or best_effort == (cgroup is not None) or (cgroup is not None and not cgroup.is_absolute()):
            raise BenchmarkError('invalid_config', 'Choose an absolute Python and explicit cgroup or best-effort policy')
        super().__init__(binary, output)
        self.python, self.cgroup, self.best_effort = python, cgroup, best_effort
        self.python_sha256 = digest(python)
        self.bundle = output / 'backup'
        self.backup_requested = False

    def _adapter_command(self, data):
        self.receipts = self.ready.parent / 'backup-receipts'
        self.receipts.mkdir(mode=0o700)
        profile = self.ready.parent / 'fixture.json'
        value = dict(data=str(data), ready=str(self.ready), bundle=str(self.bundle),
                     receipts=str(self.receipts), analysis_python=str(self.python),
                     research_cgroup=None if self.cgroup is None else str(self.cgroup),
                     research_best_effort=self.best_effort, max_snapshot_bytes=4 * 1024**3,
                     copy_timeout_ms=300000, total_timeout_ms=600000)
        descriptor = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as target:
            target.write(encode(value) + b'\n'); target.flush(); os.fsync(target.fileno())
        return super()._adapter_command(data) + ['--fixture-profile', str(profile)]

    def request_backup(self):
        if self.backup_requested:
            raise BenchmarkError('duplicate_backup', 'Only one owned backup may be requested')
        publish_stop(self.ready.parent / 'backup.request', self.nonce)
        self.backup_requested = True

    def backup_state(self):
        for name in ('backup-finished.json', 'backup-started.json'):
            path = self.receipts / name
            if path.exists() or path.is_symlink():
                return bounded_json(path)
        return dict(phase='requested' if self.backup_requested else 'not_requested')


class BackgroundWork:
    def __init__(self, config, node, after_seconds):
        self.config, self.node, self.after = config, node, after_seconds
        self.client = Client(config)
        self.cancelled, self.done, self.first_record = (threading.Event() for _ in range(3))
        self.lock = threading.Lock()
        self.raw = dict(phase='not_started', verified_records=0)
        self.job = None
        self.cancel_sent = False
        self.export_thread = None
        self.report = dict(status='not_run', overlap=None, qos_samples=[], history_phases=[], errors=[])
        self.thread = threading.Thread(target=self._run, name='benchmark-background-controller')

    def start(self):
        self.thread.start()

    def qos(self):
        value = self.client.json('/api/v1/capabilities')['candidate']['background_qos']
        if not isinstance(value, dict):
            raise BenchmarkError('invalid_response', 'Actual Core background diagnostics are missing')
        sample = dict(monotonic_ns=time.monotonic_ns(), status=value)
        if len(self.report['qos_samples']) < 512:
            self.report['qos_samples'].append(sample)
        self.report['qos_last'] = sample
        return value

    def _export(self):
        began = time.monotonic_ns()
        deadline = time.monotonic() + self.config.bulk_timeout
        try:
            path = self.client.session_path(self.config) + '/archive/export.ndjson'
            with self.client.open(path, timeout=self.config.bulk_timeout) as response:
                if response.status != 200:
                    raise HTTPFailure(response.status)
                proof = ExportProof(self.config, response.headers)
                with self.lock:
                    self.raw.update(phase='streaming', started_monotonic_ns=began,
                                    source_boundary=proof.boundary, declared_counts=proof.expected)
                while not self.cancelled.is_set():
                    if time.monotonic() >= deadline:
                        raise BenchmarkError('background_incomplete', 'Live export deadline exceeded')
                    raw = response.readline(MAX_FRAME_BYTES + 1)
                    if not raw:
                        break
                    proof.observe(raw)
                    with self.lock:
                        self.raw['verified_records'] = proof.records
                    if proof.records:
                        self.first_record.set()
                if self.cancelled.is_set():
                    raise BenchmarkError('background_cancelled', 'Owned live export was cancelled')
                result = proof.finish()
            with self.lock:
                self.raw.update(result, phase='completed', finished_monotonic_ns=time.monotonic_ns())
        except Exception as error:
            with self.lock:
                self.raw.update(phase='failed', finished_monotonic_ns=time.monotonic_ns(),
                                error=dict(code=getattr(error, 'code', 'export_failed'), message=str(error)))
        finally:
            self.first_record.set()

    def _run(self):
        try:
            self._operations()
        except Exception as error:
            self.report['status'] = 'failed'
            self.report['errors'].append(dict(code=getattr(error, 'code', 'background_failed'), message=str(error)))
            self.cancel()
        finally:
            self.done.set()

    def _operations(self):
        path = self.client.session_path(self.config)
        deadline = time.monotonic() + self.config.observer_ready_timeout + 60
        while not self.cancelled.is_set():
            try:
                counts = self.client.json(path + '/analytics/summary')
                if counts['sweeps']['count'] > 0 and counts['observations']['count'] > 0:
                    break
            except HTTPFailure as error:
                if error.status != 404:
                    raise
            if time.monotonic() >= deadline:
                raise BenchmarkError('background_not_started', 'Original producer did not commit its first source')
            self.cancelled.wait(.05)
        if self.cancelled.is_set():
            return
        first = time.monotonic()
        self.report['first_source'] = dict(monotonic_ns=time.monotonic_ns(), counts=counts,
                                          health=self.client.json('/api/v1/health'))
        if self.cancelled.wait(max(0, first + self.after - time.monotonic())):
            return
        self.report.update(status='running', started_at=utc(), started_monotonic_ns=time.monotonic_ns(),
                           health_before=self.client.json('/api/v1/health'))
        deadline = time.monotonic() + self.config.bulk_timeout
        self.qos()
        self.node.request_backup()
        self.export_thread = threading.Thread(target=self._export, name='benchmark-live-export')
        self.export_thread.start()
        # Admit the real research job after the other two owners are observed,
        # rather than merely starting three requests and inferring overlap.
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            backup = self.node.backup_state()
            if self.first_record.is_set() and backup.get('phase') in ('running', 'completed', 'failed'):
                break
            self.cancelled.wait(.05)
        if self.cancelled.is_set():
            return
        if time.monotonic() >= deadline:
            raise BenchmarkError('background_incomplete', 'Backup/export did not enter their original owned operations')
        base_us = calendar.timegm(BASE.utctimetuple()) * 1000000 + BASE.microsecond
        request = dict(operation='summary', page_size=100,
                       filters=dict(session_id=self.config.session_id, stream_id=self.config.spectrum_id,
                                    started_at_us=base_us,
                                    ended_at_us=base_us + min(self.config.duration_us, 60000000)))
        job = self.client.json('/api/v1/history/jobs', method='POST', payload=request, status=202)
        self.job = job['id']
        self.report['history_request'] = request
        job_path = '/api/v1/history/jobs/' + segment(self.job)
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            view = self.client.json(job_path)
            if view.get('phase') not in ('snapshot', 'running', 'completed', 'failed', 'cancelled'):
                raise BenchmarkError('invalid_response', 'Unknown original History phase')
            phases = self.report['history_phases']
            if not phases or phases[-1]['view']['phase'] != view['phase']:
                if len(phases) >= 8:
                    raise BenchmarkError('invalid_response', 'History phase transition bound exceeded')
                phases.append(dict(monotonic_ns=time.monotonic_ns(), view=view))
            backup = self.node.backup_state()
            with self.lock:
                raw = dict(self.raw)
            qos = self.qos()
            if self.report['overlap'] is None and overlap(backup, raw, view, qos):
                self.report['overlap'] = dict(observed_at=utc(), monotonic_ns=time.monotonic_ns(), backup=backup,
                    live_export=raw, history=view, qos=qos)
            if view['phase'] in ('completed', 'failed', 'cancelled') \
                    and backup.get('phase') in ('completed', 'failed') and raw['phase'] in ('completed', 'failed'):
                break
            self.cancelled.wait(.1)
        else:
            raise BenchmarkError('background_incomplete', 'Original background work did not reach terminal receipts')
        self.report.update(backup=backup, live_export=raw, history=view,
                           finished_at=utc(), finished_monotonic_ns=time.monotonic_ns(),
                           health_after=self.client.json('/api/v1/health'))
        if view['phase'] == 'completed':
            result = self.client.json(job_path + '/result')
            if not isinstance(result.get('metrics'), dict) or not isinstance(result.get('enforcement'), dict) \
                    or type(view.get('source_boundary')) is not int \
                    or not isinstance(view.get('source_sha256'), str) or len(view['source_sha256']) != 64:
                raise BenchmarkError('invalid_response', 'Completed History job lacks original worker/source receipts')
            self.report['research_result'] = dict(sha256=hashlib.sha256(encode(result)).hexdigest(),
                metrics=result.get('metrics'), enforcement=result.get('enforcement'))
        if view['phase'] != 'completed' or backup.get('phase') != 'completed' or raw['phase'] != 'completed':
            raise BenchmarkError('background_failed', 'An original background operation failed or was cancelled')
        if self.report['overlap'] is None:
            raise BenchmarkError('background_overlap_unmeasured', 'Simultaneous active owners were not observed')
        self.report['status'] = 'passed'

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            phases = self.report['history_phases']
            terminal = bool(phases and phases[-1]['view']['phase'] in ('completed', 'failed', 'cancelled'))
            job = self.job if not self.cancel_sent and not terminal and self.node.owner is not None else None
            if job is not None:
                self.cancel_sent = True
        if job is not None:
            try:
                self.client.json('/api/v1/history/jobs/' + segment(job) + '/cancel', method='POST', payload={})
            except BenchmarkError as error:
                self.report['errors'].append(dict(code=error.code, message=error.message))

    def join(self):
        for thread in (self.thread, self.export_thread):
            if thread is not None:
                thread.join(self.config.bulk_timeout + self.config.request_timeout + 2)
                if thread.is_alive():
                    raise BenchmarkError('background_cleanup_failed', 'Original background thread has not JOINED')
        self.report['threads_joined'] = True

    def wait(self):
        if not self.done.wait(self.config.bulk_timeout):
            raise BenchmarkError('background_incomplete', 'Original background controller deadline exceeded')


def restore(node, output, timeout):
    from tests.release_process_ownership import OwnedCommand
    restored = output / 'restored'
    restored.mkdir(mode=0o700)
    receipt = restored / 'receipt.json'
    log = (restored / 'process.log').open('xb')
    owner = None
    try:
        node.unchanged()
        command = [str(node.binary), 'restore', str(node.bundle), str(restored / 'archive.sqlite'), str(receipt)]
        owner = OwnedCommand.start(command, environment=dict(os.environ), registry=node.registry,
                                   root=ROOT, log=log, shutdown_timeout=15)
        code = owner.wait(timeout)
        owner.release(15)
        owner = None
        node.unchanged()
        if code != 0:
            raise BenchmarkError('restore_failed', 'Public Archive verification/restore did not succeed')
        result = bounded_json(receipt)
        if result.get('status') != 'passed' or result.get('manifest') != node.backup_state().get('manifest'):
            raise BenchmarkError('restore_failed', 'Verified restore differs from the original backup receipt')
        return result | dict(joined=True, physical_carrier_independence='unmeasured')
    finally:
        if owner is not None:
            owner.stop(15)
        log.close()


def run_concurrent(output, binary, python, *, cgroup=None, best_effort=False,
                   run_id=None, smoke_seconds=None, observer_node='node'):
    output = output_directory(output).resolve(strict=True)
    report = dict(report_version=1, successful=False, run_status='failed', errors=[],
                  hardware_tested=False, pi_acceptance_claimed=False, physical_carrier_independence='unmeasured')
    node = work = None
    status = 1
    try:
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise BenchmarkError('candidate_unavailable', 'Supply the immutable trusted fixture executable')
        if os.name != 'posix' or sys.platform not in ('linux', 'darwin'):
            raise BenchmarkError('candidate_unavailable', 'Existing managed-process backend requires Unix')
        options = dict(observer_node=observer_node)
        if smoke_seconds is not None:
            if not 1 <= smoke_seconds <= 60:
                raise BenchmarkError('invalid_config', 'Explicit smoke duration must be1..60 seconds')
            options.update(duration_seconds=smoke_seconds, audio_segment_seconds=min(10, smoke_seconds))
        initial = reference_config('http://127.0.0.1:1', output / 'producer',
                                   run_id or 'concurrent-' + uuid.uuid4().hex, **options)
        node = BackgroundCandidate(binary.resolve(strict=True), output, python, cgroup=cgroup, best_effort=best_effort)
        origin = node.start()
        config = replace(initial, base_url=origin)
        client = Client(config)
        capabilities = client.json('/api/v1/capabilities')
        candidate_identity(capabilities, client.json('/api/v1/health'))
        if capabilities.get('candidate', {}).get('history_worker_configured') is not True:
            raise BenchmarkError('history_worker_unavailable', 'Trusted research containment/worker is unavailable; no fallback was selected')
        owned_pid = node.owner.status().get('child_pid')
        if type(owned_pid) is not int or owned_pid <= 0:
            raise BenchmarkError('candidate_unavailable', 'Original managed node has no validated process root')
        config = replace(config, pids={'owned_node_tree': owned_pid})
        report.update(workload=config.public(), candidate_sha256=node.sha256,
            worker_provenance=dict(program=str(python), sha256=node.python_sha256,
                args=['-I', '-m', 'radioinspector.research_rpc'], history_args=['-I', '-m', 'ori_research'],
                deadline_ms=30000, require_hard_memory=not best_effort, best_effort=best_effort),
            full_30_minute_reference=smoke_seconds is None, actual_30_minute_run_observed=False)
        report['resource_scope'] = 'Original managed adapter and descendants; separate Core-only RSS acceptance is unmeasured'
        work = BackgroundWork(config, node, config.duration_seconds / 2)
        work.start()
        produced, producer_status = run(config)
        report.update(producer_report='producer/report.json', producer_status=producer_status,
                      producer_complete_delivery=produced['checks']['complete_delivery'])
        work.wait()
        work.cancel()
        node.close()
        work.join()
        report['background'] = work.report
        report['restore'] = restore(node, output, config.bulk_timeout)
        if digest(python) != node.python_sha256:
            raise BenchmarkError('worker_changed', 'Trusted Python executable changed during the run')
        report['successful'] = producer_status == 0 and work.report['status'] == 'passed'
        report['actual_30_minute_run_observed'] = report['successful'] and smoke_seconds is None \
            and produced['production']['production_elapsed_seconds'] >= 1800
        report['run_status'] = 'passed' if report['successful'] else 'failed'
        status = 0 if report['successful'] else 1
    except Exception as error:
        report['errors'].append(dict(code=getattr(error, 'code', 'fixture_failed'), message=str(error)))
    finally:
        if work is not None:
            work.cancel()
        if node is not None:
            try:
                node.close(expect_success=False)
            except Exception as error:
                report['errors'].append(dict(code='cleanup_failed', message=str(error)))
            report['owned_nodes'] = node.records
        if work is not None:
            try:
                work.join()
            except Exception as error:
                report['errors'].append(dict(code='background_cleanup_failed', message=str(error)))
            report['background'] = work.report
        if report['errors']:
            report.update(successful=False, run_status='failed')
            status = 1
        atomic_report(output, report)
    return report, status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--analysis-python', type=Path, required=True)
    policy = parser.add_mutually_exclusive_group(required=True)
    policy.add_argument('--research-cgroup', type=Path)
    policy.add_argument('--research-best-effort', action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--smoke-seconds', type=float)
    parser.add_argument('--observer-node', default=os.environ.get('ORI_BENCH_NODE', 'node'))
    args = parser.parse_args(argv)
    report, status = run_concurrent(args.output_dir, args.candidate, args.analysis_python,
        cgroup=args.research_cgroup, best_effort=args.research_best_effort, run_id=args.run_id,
        smoke_seconds=args.smoke_seconds, observer_node=args.observer_node)
    print(encode(dict(report=str(args.output_dir.absolute() / 'report.json'),
                      run_status=report['run_status'], successful=report['successful'])).decode())
    return status


if __name__ == '__main__':
    sys.exit(main())
