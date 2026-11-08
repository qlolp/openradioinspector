"""Installed-app core lifecycle; actual loopback HTTP, temporary data, no devices."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch


class DesktopCoreTests(unittest.TestCase):
    def test_owned_core_is_ready_and_archive_survives_restart(self):
        from radioinspector.launcher import LocalCore
        with tempfile.TemporaryDirectory() as root:
            core=LocalCore(Path(root))
            try:
                url=core.start()
                self.assertEqual(json.load(urlopen(url+'/api/v1/health'))['data']['status'],'ok')
                from tests.test_device_workers import simulated_session_payload
                request=Request(url+'/api/v1/sessions',data=json.dumps(simulated_session_payload()).encode(),headers={'Content-Type':'application/json'})
                created=json.load(urlopen(request))['data']['id']
                self.assertEqual(core.server.worker_manager.active_count,0)
            finally:core.close()
            core.close()
            again=LocalCore(Path(root))
            try:
                url=again.start()
                sessions=json.load(urlopen(url+'/api/v1/sessions'))['data']
                self.assertIn(created,[s['id'] for s in sessions])
            finally:again.close()

    def test_second_core_cannot_take_same_data_directory(self):
        from radioinspector.launcher import LocalCore
        with tempfile.TemporaryDirectory() as root:
            first=LocalCore(Path(root));second=LocalCore(Path(root))
            try:
                first.start()
                with self.assertRaises(RuntimeError):second.start()
                self.assertIsNone(second.server)
                self.assertTrue(first.thread.is_alive())
            finally:first.close();second.close()

    def test_startup_failure_releases_directory_lease(self):
        from radioinspector.launcher import LocalCore
        with tempfile.TemporaryDirectory() as root:
            failed=LocalCore(Path(root))
            with patch('radioinspector.launcher.create_server',side_effect=OSError('bind failed')):
                with self.assertRaises(OSError):failed.start()
            healthy=LocalCore(Path(root))
            try:healthy.start()
            finally:healthy.close()

    def test_remote_desktop_never_opens_local_archive(self):
        from radioinspector.launcher import main
        with patch('radioinspector.launcher.LocalCore') as local, patch('radioinspector.windows.scheduler_app.main',return_value=0) as gui:
            self.assertEqual(main(['--server','http://127.0.0.1:9999']),0)
        local.assert_not_called();gui.assert_called_once_with(['--server','http://127.0.0.1:9999'])

    def test_default_data_paths_are_platform_specific(self):
        from radioinspector.launcher import default_data_directory
        home=Path(tempfile.gettempdir()).resolve()/'example-home'
        self.assertEqual(default_data_directory('darwin',{},home),home/'Library/Application Support/OpenRadioInspector')
        self.assertEqual(default_data_directory('win32',{'LOCALAPPDATA':str(home/'local')},home),home/'local/OpenRadioInspector')
        self.assertEqual(default_data_directory('linux',{'XDG_DATA_HOME':str(home/'xdg')},home),home/'xdg/openradioinspector')

class CoreProcessTests(unittest.TestCase):
    def test_ready_file_and_stdin_eof_own_only_the_child(self):
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory)/'ready'
            child = subprocess.Popen([sys.executable, '-m', 'radioinspector.launcher',
                '--data-dir', directory, '--port', '0', '--ready-file', str(ready),
                '--stdin-lifeline'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+15
                while not ready.exists() and child.poll() is None and time.monotonic()<deadline:
                    time.sleep(.02)
                startup_error = child.stderr.read().decode() if child.poll() is not None else 'process has not exited'
                self.assertTrue(ready.exists(), 'core did not become ready: '+startup_error)
                url = ready.read_text().strip()
                self.assertEqual(json.load(urlopen(url+'/api/v1/health'))['data']['status'], 'ok')
                result = subprocess.run([sys.executable, '-m', 'radioinspector.launcher',
                    '--check-ready', '--port', url.rsplit(':',1)[1]],
                    capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                child.stdin.close()
                child.stdin = None
                stdout, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, stderr.decode())
                self.assertFalse(ready.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

class InstalledSmokeTests(unittest.TestCase):
    def test_desktop_self_check_never_opens_gui_or_devices(self):
        from radioinspector.launcher import main
        with tempfile.TemporaryDirectory() as directory:
            with patch('radioinspector.windows.scheduler_app.main', side_effect=AssertionError('GUI opened')):
                self.assertEqual(main(['--data-dir',directory,'--self-check']),0)
            self.assertTrue((Path(directory)/'sessions.sqlite3').is_file())

class OwnedReadinessTests(unittest.TestCase):
    def test_readiness_does_not_use_global_proxy_or_urlopen(self):
        from radioinspector.launcher import LocalCore
        with tempfile.TemporaryDirectory() as directory:
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('global opener used')):
                with LocalCore(Path(directory)) as core:
                    self.assertTrue(core.url.startswith('http://127.0.0.1:'))

class NumericBindTests(unittest.TestCase):
    def test_startup_does_not_require_reverse_dns(self):
        from radioinspector.launcher import LocalCore
        with tempfile.TemporaryDirectory() as directory:
            with patch('socket.getfqdn', side_effect=AssertionError('reverse DNS during bind')):
                with LocalCore(Path(directory)) as core:
                    self.assertTrue(core.url.startswith('http://127.0.0.1:'))

class InstalledDiagnosticsTests(unittest.TestCase):
    def test_startup_failure_is_visible_and_recorded_in_private_log(self):
        from radioinspector import launcher
        with tempfile.TemporaryDirectory() as directory:
            with patch('radioinspector.windows.scheduler_app.main',side_effect=RuntimeError('synthetic startup failure')):
                with patch('tkinter.messagebox.showerror') as show:
                    status=launcher.installed_main(['--server','http://localhost:9999','--data-dir',directory])
            self.assertEqual(status,1)
            show.assert_called_once()
            logs=list((Path(directory)/'logs').glob('desktop.log*'))
            self.assertTrue(logs)
            self.assertIn('synthetic startup failure',logs[0].read_text())
            self.assertFalse((Path(directory)/'sessions.sqlite3').exists())

class NonInteractiveSmokeTests(unittest.TestCase):
    def test_failed_self_check_exits_without_a_modal_dialog(self):
        from radioinspector.launcher import installed_main
        with tempfile.TemporaryDirectory() as directory:
            with patch('radioinspector.launcher.LocalCore.start',side_effect=RuntimeError('synthetic core error')):
                with patch('tkinter.messagebox.showerror') as show:
                    self.assertEqual(installed_main(['--self-check','--data-dir',directory]),1)
            show.assert_not_called()

class AtomicReadinessTests(unittest.TestCase):
    def test_ready_target_is_invisible_until_url_is_complete(self):
        import io
        import threading
        from contextlib import contextmanager
        from types import SimpleNamespace
        from radioinspector.launcher import core_main
        with tempfile.TemporaryDirectory() as directory:
            ready=Path(directory)/'ready'
            entered=threading.Event();release=threading.Event();errors=[]
            actual_fdopen=os.fdopen
            def delayed_open(descriptor, mode, *args, **kwargs):
                stream=actual_fdopen(descriptor,mode,*args,**kwargs)
                if mode=='w' and kwargs.get('encoding')=='ascii':
                    @contextmanager
                    def held():
                        try:
                            entered.set();release.wait(10)
                            yield stream
                        finally:stream.close()
                    return held()
                return stream
            def start():
                try:core_main(['--data-dir',directory,'--port','0','--ready-file',str(ready),'--stdin-lifeline'])
                except BaseException as error:errors.append(error)
            with patch('os.fdopen',side_effect=delayed_open),patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO())):
                thread=threading.Thread(target=start)
                try:
                    thread.start();self.assertTrue(entered.wait(5))
                    self.assertFalse(ready.exists(),'readiness became visible before the URL write')
                finally:
                    release.set();thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])

    def test_ready_publication_preserves_an_existing_owner_file(self):
        from radioinspector.launcher import _publish_ready_file
        with tempfile.TemporaryDirectory() as directory:
            ready=Path(directory)/'ready';ready.write_text('existing owner')
            with self.assertRaises(FileExistsError):
                _publish_ready_file(ready,'http://127.0.0.1:12345')
            self.assertEqual(ready.read_text(),'existing owner')
            self.assertEqual(list(Path(directory).iterdir()),[ready])
