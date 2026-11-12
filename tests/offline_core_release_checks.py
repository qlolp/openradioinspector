"""Filesystem-negative fixtures are synthetic, never portability acceptance.

Real relocated Python + Supervisor acceptance is exercised separately with
explicit locally prepared artifacts; these fixtures must not pass full probes.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.release import offline_core as release


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi ELF and POSIX release checks')
class FilesystemSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ori-offline-fs-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.payload=self.root/'payload';self.payload.mkdir()
        (self.payload/'original.txt').write_bytes(b'public fixture')

    def test_absolute_and_escaping_symlinks_are_rejected_without_reading_target(self):
        secret=self.root/'private';secret.write_bytes(b'private-data-must-stay-out')
        for target in [str(secret),'../private','a/../../private']:
            with self.subTest(target=target):
                link=self.payload/'link';link.symlink_to(target)
                with self.assertRaises(release.ReleaseError):release.inventory(self.payload)
                self.assertEqual(secret.read_bytes(),b'private-data-must-stay-out')
                link.unlink()

    def test_internal_links_preserve_exact_target_and_unicode_bytes(self):
        name='e\u0301 source';(self.payload/name).write_bytes(b'public')
        (self.payload/'link').symlink_to(name)
        inv=release.inventory(self.payload)
        item=next(x for x in inv if x['path']=='link')
        self.assertEqual(item['target'],name)
        self.assertEqual(item['kind'],'symlink')
        self.assertEqual(next(x for x in inv if x['path']==name)['sha256'],hashlib.sha256(b'public').hexdigest())

    def test_inventory_refuses_a_symlinked_component_ancestor(self):
        directory=self.root/'outside';directory.mkdir();(directory/'nested').mkdir();(directory/'nested/public').write_bytes(b'fixture')
        alias=self.root/'alias';alias.symlink_to(directory,target_is_directory=True)
        with self.assertRaises(release.ReleaseError):release.inventory(alias/'nested')

    def test_broken_and_cyclic_links_fail_closed(self):
        for target in ['absent','link']:
            (self.payload/'link').symlink_to(target)
            with self.assertRaises(release.ReleaseError):release.inventory(self.payload)
            (self.payload/'link').unlink()

    @unittest.skipUnless(hasattr(os,'mkfifo'),'Linux/POSIX filesystem test')
    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.payload/'pipe')
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload)

    def test_hardlinked_published_payload_is_rejected(self):
        os.link(self.payload/'original.txt',self.root/'alias')
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload)

    def test_private_state_and_archives_are_not_packaged(self):
        for name in ['sessions.sqlite3','private.key','.env','credentials.json']:
            path=self.payload/name;path.write_bytes(b'synthetic private state')
            with self.subTest(name=name):
                with self.assertRaises(release.ReleaseError):release.inventory(self.payload)
            path.unlink()
        (self.payload/'.ori-runtime').mkdir()
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload)

    def test_inventory_limits_apply_before_unbounded_copy(self):
        (self.payload/'too-big').write_bytes(b'123456789')
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload,release.Limits(max_file_bytes=8))
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload,release.Limits(max_entries=1))
        with self.assertRaises(release.ReleaseError):release.inventory(self.payload,release.Limits(max_total_bytes=10))

    def test_unsafe_manifest_paths_duplicate_entries_and_declared_sizes_fail(self):
        base={'format':'ori-offline-core-release','manifest_version':1,'release_version':'fixture-1',
              'source_revision':'1'*40,'architecture':'x86_64','components':{k:{'version':'0.1.0','sha256':'a'*64,'build_revision':None} for k in ('ori_core','ori_supervisor')},'runtimes':{'legacy':{'python_version':'3.12.14','packages':{'openradioinspector':'0.4.0.dev0','numpy':'2.5.4','pyserial':'3.5'}},'research':{'python_version':'3.15.1','packages':{'openradioinspector':'0.4.0.dev0','ori-research':'0.2.1','numpy':'2.5.4','duckdb':'2.0.0','pyarrow':'26.0.1'}}},
              'wheelhouse':'not_supplied','inventory':[]}
        entry={'path':'file','kind':'file','size':1,'mode':0o444,'sha256':'a'*64}
        for path in ['/absolute','../outside','a/../../outside','a//b','a/./b','a\\b','C:/file','bad\nname']:
            value=base|{'inventory':[entry|{'path':path}]}
            with self.subTest(path=path):
                with self.assertRaises(release.ReleaseError):release.validate_manifest(value)
        with self.assertRaises(release.ReleaseError):release.validate_manifest(base|{'inventory':[entry,entry]})
        with self.assertRaises(release.ReleaseError):release.validate_manifest(base|{'inventory':[entry|{'size':-1}]})
        with self.assertRaises(release.ReleaseError):release.validate_manifest(base|{'inventory':[entry|{'mode':0o777}]})

    def test_copy_refuses_symlinked_ancestor_even_when_final_file_is_regular(self):
        private=self.root/'private';private.mkdir();(private/'value').write_bytes(b'private fixture')
        (self.payload/'alias').symlink_to(private,target_is_directory=True)
        with self.assertRaises(release.ReleaseError):release.copy_regular(self.payload/'alias/value',self.root/'copied')
        self.assertFalse((self.root/'copied').exists())

    def test_partial_stage_is_not_a_release(self):
        (self.payload/'manifest.json').write_text('{')
        with self.assertRaises(release.ReleaseError):release.verify_inventory(self.payload)

    def test_duplicate_json_keys_and_nonfinite_manifest_values_fail(self):
        for value in ['{"a":1,"a":2}','{"x":{"a":1,"a":2}}','{"x":NaN}','{"x":1e999}']:
            (self.payload/'manifest.json').write_text(value)
            with self.assertRaises(release.ReleaseError):release.verify_inventory(self.payload)

    def test_no_replace_publication_preserves_previous_even_if_empty(self):
        stage=self.root/'stage';stage.mkdir();(stage/'content').write_bytes(b'new')
        previous=self.root/'version';previous.mkdir()
        with self.assertRaises((release.ReleaseError,FileExistsError)):release.publish_noreplace(stage,previous)
        self.assertTrue(stage.is_dir());self.assertEqual(list(previous.iterdir()),[])

    def test_elf_architecture_mismatch_is_rejected_without_running_it(self):
        binary=self.root/'synthetic-elf';header=bytearray(64);header[:7]=b'\x7fELF\x02\x01\x01';struct.pack_into('<H',header,18,183);binary.write_bytes(header)
        with self.assertRaises(release.ReleaseError):release.elf_architecture(binary,'x86_64')
        self.assertEqual(release.elf_architecture(binary,'aarch64'),'aarch64')

    def test_real_external_venv_is_not_mistaken_for_a_portable_runtime(self):
        prefix=self.root/'venv'
        subprocess.run([sys.executable,'-I','-m','venv','--without-pip',str(prefix)],check=True,capture_output=True)
        with self.assertRaises(release.ReleaseError):release.probe_python(prefix,'legacy',{'openradioinspector':'0.4.0.dev0','numpy':'2.5.4','pyserial':'3.5'},self.root/'diagnostics')


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi ELF and POSIX release checks')
class NegativeAssembly(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ori-offline-build-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.out=self.root/'releases';self.out.mkdir(mode=0o700)
        self.spec=self.make_spec()

    def make_spec(self):
        # Synthetic ELF headers are filesystem-only inputs. They cannot pass
        # the mandatory real executable/runtime relocation acceptance probes.
        def executable(path):
            path.parent.mkdir(parents=True,exist_ok=True)
            header=bytearray(64);header[:7]=b'\x7fELF\x02\x01\x01';struct.pack_into('<H',header,18,62)
            path.write_bytes(header);path.chmod(0o700);return path
        core=executable(self.source/'ori-core');supervisor=executable(self.source/'ori-supervisor')
        for role in ['legacy','research']:executable(self.source/role/'bin/python3')
        for name in ['ui','notices']:(self.source/name).mkdir()
        (self.source/'ui/index.html').write_text('<script src="./app.js"></script>')
        (self.source/'ui/app.js').write_text('/* synthetic filesystem fixture */')
        (self.source/'notices/LICENSE.txt').write_text('Synthetic filesystem-only license')
        (self.source/'notices/PYTHON-LICENSE.txt').write_text('Synthetic filesystem-only license')
        return {'release_version':'fixture-1','source_revision':'1'*40,'architecture':'x86_64',
          'ori_core':{'path':str(core),'version':'0.1.0','sha256':digest(core)},
          'ori_supervisor':{'path':str(supervisor),'version':'0.1.0','sha256':digest(supervisor)},
          'legacy_runtime':{'path':str(self.source/'legacy'),'python_version':'3.12.14',
                            'packages':{'openradioinspector':'0.4.0.dev0','numpy':'2.5.4','pyserial':'3.5'}},
          'research_runtime':{'path':str(self.source/'research'),'python_version':'3.15.1',
                            'packages':{'openradioinspector':'0.4.0.dev0','ori-research':'0.2.1','numpy':'2.5.4','duckdb':'2.0.0','pyarrow':'26.0.1'}},
          'ui_assets':str(self.source/'ui'),'notices':str(self.source/'notices')}

    def test_diagnostics_cannot_modify_a_supplied_runtime(self):
        diagnostics=self.source/'legacy/diagnostics'
        with self.assertRaisesRegex(release.ReleaseError,'diagnostics_overlap_payload'):
            release.build(self.spec,self.out,diagnostics)
        self.assertFalse(diagnostics.exists());self.assertEqual(list(self.out.iterdir()),[])

    def test_copy_failure_publishes_nothing_and_retains_sanitized_diagnostics(self):
        with patch.object(release,'copy_regular',side_effect=OSError('credential=not-for-reports')):
            with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertEqual(list(self.out.iterdir()),[])
        report=(self.root/'diagnostics/report.json').read_text()
        self.assertNotIn('not-for-reports',report);self.assertIn('failed',report)

    def test_interruption_does_not_leave_a_partial_published_version(self):
        with patch.object(release,'copy_regular',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertFalse((self.out/'fixture-1').exists());self.assertEqual(list(self.out.iterdir()),[])

    def test_missing_executable_and_bad_binary_hash_are_refused(self):
        Path(self.spec['ori_core']['path']).unlink()
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertEqual(list(self.out.iterdir()),[])
        self.spec=self.make_spec_existing()
        self.spec['ori_core']['sha256']='0'*64
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertEqual(list(self.out.iterdir()),[])

    def make_spec_existing(self):
        shutil.rmtree(self.source);self.source.mkdir();return self.make_spec()

    def test_combined_payload_limit_is_checked_before_any_copy(self):
        with patch.object(release,'copy_regular',side_effect=AssertionError('copy must not precede inventory budget')):
            with self.assertRaisesRegex(release.ReleaseError,'inventory_size_limit'):
                release.build(self.spec,self.out,self.root/'diagnostics',release.Limits(max_total_bytes=100))
        self.assertEqual(list(self.out.iterdir()),[])

    def test_existing_version_and_data_remain_unchanged(self):
        previous=self.out/'fixture-1';previous.mkdir();(previous/'latest.sqlite3').write_bytes(b'untouched synthetic existing archive')
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertEqual((previous/'latest.sqlite3').read_bytes(),b'untouched synthetic existing archive')

    def test_diagnostics_cannot_truncate_a_hardlinked_archive(self):
        archive=self.root/'existing.sqlite3';archive.write_bytes(b'original private fixture')
        diagnostics=self.root/'diagnostics';diagnostics.mkdir(mode=0o700)
        os.link(archive,diagnostics/'report.json')
        self.spec['release_version']='../invalid'
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,diagnostics)
        self.assertEqual(archive.read_bytes(),b'original private fixture')

    def test_source_destination_overlap_is_refused_before_copy(self):
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.source/'legacy/releases',self.root/'diagnostics')

    def test_incomplete_repair_wheelhouse_is_not_required_or_claimed_complete(self):
        self.assertNotIn('wheels',self.spec)
        # The filesystem fixture still fails mandatory real runtime probes.
        with self.assertRaises(release.ReleaseError):release.build(self.spec,self.out,self.root/'diagnostics')
        self.assertFalse((self.out/'fixture-1').exists())



@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi ELF and POSIX release checks')
class ProbeSafety(unittest.TestCase):
    def test_probe_argv_is_literal_and_environment_has_no_inherited_credentials(self):
        from tools.release import _offline_process as process
        with tempfile.TemporaryDirectory(prefix='ori-offline-argv-') as directory:
            root=Path(directory);marker=root/'must-not-exist'
            argument=f'; touch {marker}; $(echo unsafe)'
            env=process.environment(root,root)
            self.assertNotIn('ORI_RUNTIME_CONTROL_TOKEN',env)
            self.assertNotIn('PYTHONPATH',env)
            result=process.run([sys.executable,'-I','-B','-c','import sys; print(sys.argv[1])',argument],cwd=root,env=env)
            self.assertEqual(result.code,0);self.assertEqual(result.stdout.decode().strip(),argument)
            self.assertFalse(marker.exists())

    def test_output_and_timeout_limits_reap_only_the_owned_probe_group(self):
        from tools.release import _offline_process as process
        with tempfile.TemporaryDirectory(prefix='ori-offline-probe-') as directory:
            root=Path(directory);env=process.environment(root,root)
            foreign=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(20)'],start_new_session=True)
            try:
                with self.assertRaises(process.ProbeError):process.run([sys.executable,'-I','-c','print("x"*65536)'],cwd=root,env=env,output_limit=128)
                with self.assertRaises(process.ProbeError):process.run([sys.executable,'-I','-c','import time;time.sleep(20)'],cwd=root,env=env,timeout=.05)
                self.assertIsNone(foreign.poll())
            finally:foreign.terminate();foreign.wait(timeout=5)

    def test_missing_real_numpy_native_dependency_is_rejected_before_import(self):
        import numpy
        with tempfile.TemporaryDirectory(prefix='ori-offline-needed-') as directory:
            root=Path(directory)
            source=next(Path(numpy.__file__).parent.rglob('_multiarray_umath*.so'))
            relative=Path('runtimes/legacy/lib/python3.12/site-packages/numpy/_core')/source.name
            (root/relative).parent.mkdir(parents=True)
            shutil.copyfile(source,root/relative)
            manifest={'architecture':platform.machine(),'inventory':[{'path':relative.as_posix(),'kind':'file'}]}
            with self.assertRaisesRegex(release.ReleaseError,'missing_native_dependency'):
                release._elf_dependencies(root,manifest,root/'diagnostics')

@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi ELF and POSIX release checks')
class CertificatePayloadSafety(unittest.TestCase):
    def test_private_key_disguised_as_public_ca_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='ori-offline-ca-') as directory:
            root=Path(directory);(root/'cacert.pem').write_bytes(b'-----BEGIN PRIVATE KEY-----\nsynthetic-private-fixture\n-----END PRIVATE KEY-----\n')
            with self.assertRaises(release.ReleaseError):release.inventory(root)

    def test_real_public_ca_certificate_is_inventory_data_not_a_private_key(self):
        import ssl
        # Use a real public CA from the OS store; no network or TLS connection.
        candidates=Path('/etc/ssl/certs').glob('*.pem')
        source=next(p for p in candidates if p.is_file())
        with tempfile.TemporaryDirectory(prefix='ori-offline-public-ca-') as directory:
            root=Path(directory);shutil.copyfile(source,root/'ca.pem')
            items=release.inventory(root)
            self.assertEqual(items[0]['sha256'],digest(root/'ca.pem'))

@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi ELF and POSIX release checks')
class SealedMetadataSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ori-offline-seal-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.payload=self.root/'bundle';self.payload.mkdir()
        (self.payload/'plain').write_bytes(b'public fixture');(self.payload/'plain').chmod(0o444)
        self.manifest={'format':'ori-offline-core-release','manifest_version':1,'release_version':'fs-only-1',
          'source_revision':'1'*40,'architecture':'x86_64',
          'components':{k:{'version':'0.1.0','sha256':'a'*64,'build_revision':None} for k in ('ori_core','ori_supervisor')},
          'runtimes':{'legacy':{'python_version':'3.12.14','packages':{'openradioinspector':'0.4.0.dev0','numpy':'2.5.4','pyserial':'3.5'}},
             'research':{'python_version':'3.15.1','packages':{'openradioinspector':'0.4.0.dev0','ori-research':'0.2.1','numpy':'2.5.4','duckdb':'2.0.0','pyarrow':'26.0.1'}}},
          'wheelhouse':'not_supplied','inventory':[{'path':'plain','kind':'file','mode':0o444,'size':14,'sha256':hashlib.sha256(b'public fixture').hexdigest()}]}
        release._write_manifest(self.payload,self.manifest,release.Limits())
        self.addCleanup(self.make_writable)

    def make_writable(self):
        if self.payload.exists():
            for p in self.payload.iterdir():
                if not p.is_symlink():p.chmod(0o600)
            self.payload.chmod(0o700)

    def test_checksum_and_payload_tampering_are_detected(self):
        release.verify_inventory(self.payload) # Filesystem integrity only.
        (self.payload/'plain').chmod(0o600);(self.payload/'plain').write_bytes(b'private tamper');(self.payload/'plain').chmod(0o444)
        with self.assertRaises(release.ReleaseError):release.verify_inventory(self.payload)

    def test_seal_metadata_must_itself_remain_read_only(self):
        (self.payload/'manifest.json').chmod(0o600)
        with self.assertRaises(release.ReleaseError):release.verify_inventory(self.payload)

    def test_template_rendering_cannot_write_inside_data_directory(self):
        data=self.root/'data';data.mkdir();(data/'sentinel').write_bytes(b'unchanged')
        with self.assertRaises(release.ReleaseError):release.render(self.payload,data,8770,data/'configs')
        self.assertEqual(sorted(p.name for p in data.iterdir()),['sentinel'])

    def test_writable_ancestor_cannot_host_a_published_release(self):
        unsafe=self.root/'unsafe';unsafe.mkdir();unsafe.chmod(0o777)
        with self.assertRaises(release.ReleaseError):release._private_directory(unsafe/'releases')

    def test_corrupt_optional_repair_wheel_is_not_advertised_as_valid(self):
        import zipfile
        repair=self.root/'repair';(repair/'wheels').mkdir(parents=True)
        wheel=repair/'wheels/example.whl'
        with zipfile.ZipFile(wheel,'w',compression=zipfile.ZIP_STORED) as archive:archive.writestr('package/file.py',b'PAYLOAD-DATA')
        content=wheel.read_bytes();wheel.write_bytes(content.replace(b'PAYLOAD-DATA',b'PAYLOAD-FAIL'))
        with self.assertRaises(release.ReleaseError):release._verify_wheels(repair,release.Limits())

    def test_diagnostics_retain_failure_after_a_later_success(self):
        report=self.root/'diagnostics'
        release._report(report,'build','failed','component_hash_mismatch')
        release._report(report,'build','passed')
        events=[json.loads(row) for row in (report/'events.ndjson').read_text().splitlines()]
        self.assertEqual([e['status'] for e in events],['failed','passed'])

@unittest.skipUnless(sys.platform.startswith('linux'),'Linux/Pi listener ownership check')
class ReleasedListenerSafety(unittest.TestCase):
    def test_live_listener_is_never_treated_as_time_wait(self):
        import socket
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',0));listener.listen()
            with self.assertRaises(release.ReleaseError):release.check_released_listener(listener.getsockname()[1])

    def test_closed_server_time_wait_does_not_fake_a_leaked_service(self):
        import socket
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.listen()
        client=socket.create_connection(('127.0.0.1',port));accepted,_=listener.accept()
        accepted.close();self.assertEqual(client.recv(1),b'');client.close();listener.close()
        release.check_released_listener(port)


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux AF_UNIX supervisor contract')
class SupervisorPathSafety(unittest.TestCase):
    def test_overlong_admin_socket_path_fails_before_install_or_start(self):
        with tempfile.TemporaryDirectory(prefix='ori-offline-sun-') as directory:
            root=Path(directory);scratch=root/('s'*90)
            with self.assertRaisesRegex(release.ReleaseError,'supervisor_socket_path_too_long'):
                release.rehearse(root/'missing-a',root/'missing-b',scratch,root/'diagnostics')
            self.assertFalse(scratch.exists())

if __name__=='__main__':unittest.main()
