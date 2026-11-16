"""Packaged CLI using the existing owned process session and EOF lifeline."""
import subprocess
from pathlib import Path
from ..config import BenchmarkError
from ..concurrent import bounded_json
from ..endurance_node import OwnedCandidate,publish_stop
from .evidence import private_json


class PackageNode(OwnedCandidate):
    def __init__(self,binary,output,run_id,*,restore_bundle=None):
        super().__init__(Path(binary),Path(output))
        self.run_id=run_id;self.restore_bundle=restore_bundle
        self.receipts=self.output/'evidence';self.receipts.mkdir(mode=0o700)
        self.bundle=self.receipts/'backup';self.backup_requested=False
        self.final_bundle=self.receipts/'final-backup';self.final_requested=False

    def _adapter_command(self,data):
        if self.restore_bundle is not None:
            result=subprocess.run([str(self.binary),'acceptance-restore',str(self.restore_bundle),
                                   str(data/'sessions.sqlite3'),str(self.receipts/'restore.json')],
                                  cwd=self.output,stdin=subprocess.DEVNULL,capture_output=True,timeout=600)
            (self.output/'restore.log').write_bytes(result.stdout+result.stderr)
            if result.returncode:raise BenchmarkError('restore_failed','Actual packaged restore failed; see restore.log')
        profile=self.receipts/'core-profile.json'
        private_json(profile,dict(version=1,run_id=self.run_id,phase='load',output_dir=str(self.receipts),
                                  data_dir=str(data),expected_core_sha256=self.sha256))
        return super()._adapter_command(data)+['--acceptance-profile',str(profile)]

    def request_backup(self):
        if self.backup_requested:raise BenchmarkError('duplicate_backup','One owner backup per run')
        publish_stop(self.receipts/'backup.request',self.run_id)
        self.backup_requested=True

    def backup_state(self):
        for name in ['backup-finished.json','backup-started.json']:
            path=self.receipts/name
            if path.exists() or path.is_symlink():return bounded_json(path)
        return dict(phase='requested' if self.backup_requested else 'not_requested')

    def request_final_backup(self):
        if self.final_requested:raise BenchmarkError('duplicate_backup','Final owner backup already requested')
        publish_stop(self.receipts/'final-backup.request',self.run_id);self.final_requested=True

    def final_backup_state(self):
        for name in ['final-backup-finished.json','final-backup-started.json']:
            path=self.receipts/name
            if path.exists() or path.is_symlink():return bounded_json(path)
        return dict(phase='requested' if self.final_requested else 'not_requested')
