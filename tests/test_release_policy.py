"""Public tests must not implicitly discover proprietary research material."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_analysis_tools import locate_distribution


class TestResearchIsolation(unittest.TestCase):
    def test_default_does_not_use_in_repository_distribution(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(locate_distribution())

    def test_explicit_external_directory_can_be_used(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'ORI_RESEARCH_DISTRIBUTION': root}):
                self.assertEqual(locate_distribution(), Path(root).resolve())

    def test_invalid_explicit_directory_fails(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'ORI_RESEARCH_DISTRIBUTION': root + '/absent'}):
                with self.assertRaises(ValueError): locate_distribution()


class TestPublicArtifactPolicy(unittest.TestCase):
    def run_scan(self, root, blocked):
        import subprocess
        import sys
        script = Path(__file__).resolve().parents[1] / 'tools/release/check_public_tree.py'
        return subprocess.run([sys.executable, str(script), '--repository', str(root),
                               '--blocked-blobs', str(blocked)], capture_output=True, text=True)

    def test_renamed_research_blob_is_rejected(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'renamed.dat').write_text('synthetic proprietary fixture')
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@example.invalid',
                            'commit','-qm','fixture'],check=True)
            oid = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD:renamed.dat'],text=True).strip()
            blocked = root/'blocked.txt'
            blocked.write_text(oid+'\n')
            result=self.run_scan(root,blocked)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertIn('renamed.dat',result.stdout)

    def test_clean_repository_passes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'own.py').write_text('print(1)\n')
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@example.invalid',
                            'commit','-qm','fixture'],check=True)
            blocked=root/'blocked.txt';blocked.write_text('')
            result=self.run_scan(root,blocked)
            self.assertEqual(result.returncode,0,result.stderr)


class TestDistributionArtifacts(unittest.TestCase):
    def test_archive_with_renamed_known_blob_is_rejected(self):
        import hashlib
        import subprocess
        import sys
        import zipfile
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            data=b'synthetic blocked fixture'
            oid=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            blocked=root/'blocked.txt';blocked.write_text(oid+'\n')
            artifact=root/'example.whl'
            with zipfile.ZipFile(artifact,'w') as out:out.writestr('package/resource.bin',data)
            script=Path(__file__).resolve().parents[1]/'tools/release/check_public_tree.py'
            result=subprocess.run([sys.executable,str(script),'--artifact',str(artifact),
                                   '--blocked-blobs',str(blocked)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertIn('resource.bin',result.stdout)
