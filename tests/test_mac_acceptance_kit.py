import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import subprocess
import sys
import os
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('acceptance_builder',ROOT/'packaging/macos/build_acceptance_kit.py')
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class AcceptanceKitTests(unittest.TestCase):
    def test_framework_launcher_keeps_its_app_interpreter_in_the_relocated_closure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);original=root/'Python.framework/Versions/3.12';original.mkdir(parents=True)
            launcher=original/'bin/python3';launcher.parent.mkdir()
            interpreter=original/'Resources/Python.app/Contents/MacOS/Python';interpreter.parent.mkdir(parents=True)
            library=original/'Python'
            for path in (launcher,interpreter,library):path.write_bytes(b'\xcf\xfa\xed\xfe' + b'framework fixture')
            (interpreter.parent.parent/'Info.plist').write_bytes(b'Python application metadata')
            signature=interpreter.parent.parent/'_CodeSignature';signature.mkdir();(signature/'CodeResources').write_bytes(b'old signature')
            stdlib=original/'lib/python3.12';stdlib.mkdir(parents=True);(stdlib/'codecs.py').write_text('# stdlib fixture\n')
            (original/'LICENSE.txt').write_text('Python fixture license')
            kit=root/'kit';kit.mkdir();copied=kit/'runtime/Python.framework/Versions/3.12'
            commands=[]
            def command(*arguments):
                commands.append(tuple(str(v) for v in arguments))
                if arguments[0]=='lipo':return 'arm64\n'
                if arguments[0]=='otool':
                    source=Path(arguments[-1])
                    if '-L' in arguments:return str(source)+':\n\t'+str(library)+' (compatibility version 3.12.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n'
                    if '-D' in arguments:return str(source)+':\n'+(str(library)+'\n' if source==library else '')
                return ''
            def run(arguments,**_kwargs):
                if '-c' in arguments:
                    # The framework bin/python3 launcher spawns this bundled app,
                    # rather than interpreting Python code itself.
                    application=copied/'Resources/Python.app/Contents/MacOS/Python'
                    if not application.is_file():raise FileNotFoundError(application)
                    self.assertEqual(application.read_bytes(),interpreter.read_bytes())
                return SimpleNamespace(returncode=0)
            with mock.patch.object(builder.sys,'base_prefix',str(original)),mock.patch.object(builder.sys,'executable',str(launcher)),\
                 mock.patch.object(builder.sys,'version_info',SimpleNamespace(major=3,minor=12)),\
                 mock.patch.object(builder.sysconfig,'get_config_var',return_value='Python'),\
                 mock.patch.object(builder,'command',side_effect=command),mock.patch.object(builder.subprocess,'run',side_effect=run),\
                 mock.patch.object(builder,'prime_runtime'):
                executable=builder.runtime(kit)
            self.assertEqual(executable,copied/'bin/python3')
            app=copied/'Resources/Python.app'
            self.assertEqual((app/'Contents/Info.plist').read_bytes(),b'Python application metadata')
            self.assertFalse((app/'Contents/_CodeSignature').exists())
            self.assertIn(('install_name_tool','-change',str(library),'@loader_path/../../../../Python',str(app/'Contents/MacOS/Python')),commands)
            self.assertIn(('codesign','--force','--sign','-','--timestamp=none',str(app)),commands)

    def test_child_runtime_ignores_operator_python_hooks_but_preserves_os_environment(self):
        from benchmarks.ori_bench.mac_acceptance.entry import engineering_environment
        value=engineering_environment({'PYTHONINSPECT':'1','PYTHONPATH':'unrelated','PYTHONHOME':'unrelated',
            'PYTHONPYCACHEPREFIX':'unrelated','DYLD_LIBRARY_PATH':'unrelated','HOME':'/operator','PATH':'/usr/bin'})
        self.assertEqual(value,{'HOME':'/operator','PATH':'/usr/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'})

    def test_prepared_caches_do_not_mutate_when_isolated_helpers_run_after_relocation(self):
        from tools.release import macos_rc_package as package
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);library=root/'library';library.mkdir()
            module=library/'ori_cache_probe.py';module.write_text('VALUE = 42\n')
            builder.prime_runtime(Path(sys.executable),library)
            expected=package.inventory(library)
            moved=root/'relocated';library.rename(moved)
            module=moved/module.name;os.utime(module,(module.stat().st_atime,module.stat().st_mtime+3))
            subprocess.run([sys.executable,'-I','-S','-c','import sys;sys.path.insert(0,sys.argv[1]);import ori_cache_probe;assert ori_cache_probe.VALUE==42',str(moved)],check=True,cwd=root)
            self.assertEqual(package.inventory(moved),expected)
    def test_relocated_payload_has_owner_entry_and_correct_host_support(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);payload=root/'payload';builder.copy_payload(payload)
            environment=dict(os.environ,PYTHONPATH=str(payload),PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1')
            imported=subprocess.run([sys.executable,'-B','-m','tests.run_release_checks','--help'],
                cwd=payload,env=environment,capture_output=True,text=True,timeout=10)
            self.assertEqual(imported.returncode,0,imported.stdout+imported.stderr)
            code='''import os,sys; from pathlib import Path
sys.path.insert(0,sys.argv[1])
from tests.release_process_ownership import OwnedCommand,local_kernel,OwnershipError
if os.name=='nt':
 try:local_kernel()
 except OwnershipError:sys.exit(0)
 raise AssertionError('Mac ownership must explicitly reject Windows')
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
            log=root/'registry/process.log'
            self.assertEqual(result.returncode,0,result.stdout+result.stderr+(log.read_text() if log.is_file() else ''))

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
            (root/'.DS_Store').write_bytes(b'Finder metadata')
            verify_kit(root)
            (root/'source.py').write_text('changed')
            with self.assertRaises(ValueError):verify_kit(root)

    def test_acceptance_evidence_overrides_only_the_clock_for_live_samples(self):
        import json
        from benchmarks.ori_bench.mac_acceptance.load import Events
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'events';events=Events(path,'run');events.emit('sample',{'at_ns':9007199254740993});events.close()
            self.assertEqual(json.loads(path.read_text())['at_ns'],9007199254740993)
