import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('acceptance_builder',ROOT/'packaging/macos/build_acceptance_kit.py')
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class AcceptanceKitTests(unittest.TestCase):
    def test_relocated_payload_can_launch_and_join_an_actual_isolated_owned_child(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);payload=root/'payload';builder.copy_payload(payload)
            code='''import os,sys; from pathlib import Path
sys.path.insert(0,sys.argv[1])
from tests.release_process_ownership import OwnedCommand
r=Path(sys.argv[2]);r.mkdir(mode=0o700)
with (r/'process.log').open('xb') as log:
 owner=OwnedCommand.start([sys.executable,'-I','-S','-c','print("actual child")'],environment=dict(os.environ,PYTHONPATH=sys.argv[1],PYTHONDONTWRITEBYTECODE='1'),registry=r,root=Path(sys.argv[1]),log=log,shutdown_timeout=5)
 try:
  assert owner.wait(10)==0
  owner.release(5)
 except BaseException:
  owner.stop(5);raise
'''
            result=subprocess.run([sys.executable,'-I','-S','-c',code,str(payload),str(root/'registry')],
                                  cwd=root,capture_output=True,text=True,timeout=35)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr+(root/'registry/process.log').read_text())

    def test_developer_library_cannot_silently_become_a_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);prefix=root/'Python';prefix.mkdir();source=prefix/'bin/python3';source.parent.mkdir();source.touch()
            library=prefix/'lib/Python';library.parent.mkdir();library.touch()
            self.assertEqual(builder.resolve_dependency(str(library),source,prefix,[]),library)
            self.assertIsNone(builder.resolve_dependency('/usr/lib/libSystem.B.dylib',source,prefix,[]))
            external=root/'Homebrew/libcrypto.dylib';external.parent.mkdir();external.touch()
            with self.assertRaises(ValueError):builder.resolve_dependency(str(external),source,prefix,[])
            with self.assertRaises(ValueError):builder.resolve_dependency('@rpath/missing.dylib',source,prefix,[])

    def test_payload_is_self_contained_and_does_not_include_test_archives_or_bytecode(self):
        with tempfile.TemporaryDirectory() as d:
            destination=Path(d)/'payload';builder.copy_payload(destination)
            self.assertTrue((destination/'tests/release_process_ownership.py').is_file())
            self.assertTrue((destination/'benchmarks/ori_bench/mac_acceptance/entry.py').is_file())
            self.assertFalse(any(destination.rglob('*.pyc')))
            self.assertFalse((destination/'radioinspector').exists())

    def test_kit_manifest_excludes_only_itself_and_detects_new_or_changed_files(self):
        from benchmarks.ori_bench.mac_acceptance.entry import verify_kit
        from tools.release import macos_rc_package as package
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'source.py').write_text('original')
            package.write_json(root/'kit-manifest.json',dict(files=package.inventory(root)))
            verify_kit(root)
            (root/'source.py').write_text('changed')
            with self.assertRaises(ValueError):verify_kit(root)

    def test_acceptance_evidence_overrides_only_the_clock_for_live_samples(self):
        import json
        from benchmarks.ori_bench.mac_acceptance.load import Events
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'events';events=Events(path,'run');events.emit('sample',{'at_ns':9007199254740993});events.close()
            self.assertEqual(json.loads(path.read_text())['at_ns'],9007199254740993)
