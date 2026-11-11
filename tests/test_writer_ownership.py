"""Owned temporary paths and native cross-process writer-lock compatibility."""
from contextlib import ExitStack
import gc
import os
from pathlib import Path
import queue
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import weakref

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.writer_ownership import archive_writer_owner


# fs2::try_lock_exclusive uses flock on Unix and this exact LockFileEx range on
# Windows. Exercise the OS operation independently of the Python owner/helper.
LOCK_PROCESS = r'''
import ctypes, os, sys
from pathlib import Path
mode, argument = sys.argv[1:]
BridgeError = ()
if mode == 'owner':
    from radioinspector.runtime_bridge import RuntimeBridgeError as BridgeError
    from radioinspector.writer_ownership import archive_writer_owner
lease = None
try:
    if mode == 'owner':
        lease = archive_writer_owner(argument)
    elif mode != 'idle':
        path = Path(argument)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == 'nt':
            import msvcrt
            from ctypes import wintypes
            class OVERLAPPED(ctypes.Structure):
                _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
            api.LockFileEx.restype = wintypes.BOOL
            overlap = OVERLAPPED()
            if not api.LockFileEx(msvcrt.get_osfhandle(descriptor), 3, 0, 0xffffffff, 0xffffffff, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BridgeError as error:
    if error.status != 409: raise
    print('busy', flush=True)
    raise SystemExit(20)
except OSError as error:
    import errno
    if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES): raise
    print('busy', flush=True)
    raise SystemExit(20)
print('held', flush=True)
if mode != 'probe': sys.stdin.readline()
if lease is not None: lease.release_after_archive_closed()
'''


class WriterOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.database = self.directory / 'sessions.sqlite3'
        self.lock_path = self.directory / '.ori-runtime' / 'writer.lock'

    def owner(self, path=None):
        owner = archive_writer_owner(self.database if path is None else path)
        self.addCleanup(owner.release_after_archive_closed)
        return owner

    def child(self, mode, path, *, inherit=False):
        environment = {key:value for key,value in os.environ.items() if not key.startswith('ORI_RUNTIME_')}
        environment.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        process = subprocess.Popen([sys.executable, '-u', '-c', LOCK_PROCESS, mode, str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment, close_fds=not inherit)
        self.addCleanup(self.close_child, process)
        lines = queue.Queue()
        threading.Thread(target=lambda:lines.put(process.stdout.readline()), daemon=True).start()
        self.assertEqual(lines.get(timeout=5), 'held\n')
        return process

    @staticmethod
    def close_child(process):
        if process.poll() is None:
            try:
                process.communicate('\n', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        else:
            process.communicate(timeout=5)

    def test_memory_factory_is_inert_and_persistent_factory_creates_no_database(self):
        self.assertIsNone(archive_writer_owner(None))
        self.assertIsNone(archive_writer_owner(':memory:'))
        self.assertEqual(list(self.directory.iterdir()), [])
        for invalid in ('', '   ', self.directory):
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(invalid)
        self.assertEqual(list(self.directory.iterdir()), [])
        owner = self.owner()
        self.assertEqual(owner.database_path, self.database)
        self.assertEqual(owner.data_directory, self.directory)
        self.assertTrue(self.lock_path.is_file())
        self.assertFalse(self.database.exists())
        self.assertEqual([path.name for path in self.directory.iterdir()], ['.ori-runtime'])

    def test_relative_and_home_paths_have_one_canonical_parent_and_private_new_directories(self):
        code = ('from radioinspector.writer_ownership import archive_writer_owner; import sys; '
                'owner=archive_writer_owner(sys.argv[1]); print(owner.database_path); '
                'owner.release_after_archive_closed()')
        environment = dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                           HOME=str(self.directory),USERPROFILE=str(self.directory),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        for source,target in [('nested/child/sessions.sqlite3',self.directory/'nested'/'child'/'sessions.sqlite3'),
                              ('~/home-child/sessions.sqlite3',self.directory/'home-child'/'sessions.sqlite3')]:
            completed=subprocess.run([sys.executable,'-c',code,source],cwd=self.directory,
                env=environment,capture_output=True,text=True,timeout=5)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(completed.stdout.strip(),str(target))
            owner=self.owner(target)
            self.assertEqual(owner.database_path,target)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode),0o700)
                self.assertEqual(stat.S_IMODE((target.parent/'.ori-runtime').stat().st_mode),0o700)
            owner.release_after_archive_closed()

    def test_owned_existing_755_directory_is_kept_and_archive_content_is_not_opened(self):
        self.directory.chmod(0o755)
        self.database.write_bytes(b'not a SQLite archive; ownership must not inspect contents')
        self.database.chmod(0o200 if os.name != 'nt' else 0o600)
        self.owner()
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        self.database.chmod(0o600)
        self.assertEqual(self.database.read_bytes(), b'not a SQLite archive; ownership must not inspect contents')

    def test_owner_and_independent_native_fs2_lock_exclude_each_other(self):
        native = self.child('native', self.lock_path)
        with self.assertRaises(RuntimeBridgeError) as busy:
            archive_writer_owner(self.database)
        self.assertEqual(busy.exception.status, 409)
        native.communicate('\n', timeout=5)
        self.assertEqual(native.returncode, 0)
        owner = self.owner()
        probe = subprocess.run([sys.executable, '-c', LOCK_PROCESS, 'probe', str(self.lock_path)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual((probe.returncode,probe.stdout), (20,'busy\n'), probe.stderr)
        owner.release_after_archive_closed()
        probe = subprocess.run([sys.executable, '-c', LOCK_PROCESS, 'probe', str(self.lock_path)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual((probe.returncode,probe.stdout), (0,'held\n'), probe.stderr)

    def test_independent_python_owner_blocks_before_any_archive_file(self):
        process = self.child('owner', self.database)
        self.assertFalse(self.database.exists())
        with self.assertRaises(RuntimeBridgeError):
            archive_writer_owner(self.database)
        process.communicate('\n', timeout=5)
        self.assertEqual(process.returncode, 0)
        self.owner()

    def test_descriptor_is_not_inherited_by_an_unrelated_owned_child(self):
        owner = self.owner()
        child = self.child('idle', self.database, inherit=True)
        owner.release_after_archive_closed()
        self.owner()
        self.assertIsNone(child.poll())

    def test_successful_exit_stack_releases_only_after_the_archive_closes(self):
        owner = self.owner()
        with ExitStack() as stack:
            stack.enter_context(owner)
            connection = sqlite3.connect(self.database)
            def close_archive():
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(self.database)
                connection.close()
            stack.callback(close_archive)
        self.owner()
        with self.assertRaises(RuntimeBridgeError):
            owner.__enter__()

    def test_exception_retains_lease_after_context_gc_until_explicit_proved_close(self):
        owner = archive_writer_owner(self.database)
        reference = weakref.ref(owner)
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        def finish():
            connection.close()
            retained = reference()
            if retained is not None:
                retained.release_after_archive_closed()
        self.addCleanup(finish)
        stack = ExitStack()
        stack.enter_context(owner)
        def failed_close(): raise RuntimeError('synthetic archive still open')
        stack.callback(failed_close)
        with self.assertRaisesRegex(RuntimeError, 'still open'):
            stack.close()
        del owner, stack
        gc.collect()
        retained = reference()
        self.assertIsNotNone(retained)
        with self.assertRaises(RuntimeBridgeError):
            archive_writer_owner(self.database)
        with self.assertRaises(RuntimeBridgeError):
            retained.close()
        connection.execute('CREATE TABLE synthetic (value INTEGER)')
        connection.commit()
        connection.close()
        retained.release_after_archive_closed()
        retained.close()
        self.owner()

    def test_unsafe_database_or_lock_aliases_refuse_before_database_creation(self):
        source = self.directory/'source'
        source.write_bytes(b'original')
        os.link(source,self.database)
        with self.assertRaises(RuntimeBridgeError):
            archive_writer_owner(self.database)
        self.assertFalse(self.lock_path.exists())
        self.database.unlink()
        self.lock_path.parent.mkdir(mode=0o700)
        os.link(source,self.lock_path)
        with self.assertRaises(RuntimeBridgeError):
            archive_writer_owner(self.database)
        self.assertFalse(self.database.exists())
        self.assertEqual(source.read_bytes(),b'original')
        self.lock_path.unlink()
        if os.name != 'nt':
            for target in (self.database,self.lock_path):
                target.symlink_to(source)
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(self.database)
                target.unlink()
            linked = self.directory/'linked'
            linked.symlink_to(self.directory, target_is_directory=True)
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(linked/'sessions.sqlite3')

    def test_untrusted_read_only_ancestor_cannot_replace_the_owned_data_directory(self):
        if os.name != 'nt':
            ancestor = self.directory/'other-owner'
            ancestor.mkdir(mode=0o755)
            data = ancestor/'mine'
            data.mkdir(mode=0o700)
            original = Path.lstat
            def metadata(path, *args, **kwargs):
                value = original(path,*args,**kwargs)
                if path == ancestor:
                    fields = list(value)
                    fields[4] = os.geteuid()+1
                    return os.stat_result(fields)
                return value
            with patch.object(Path,'lstat',metadata):
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(data/'sessions.sqlite3')
            self.assertFalse((data/'.ori-runtime').exists())

    def test_untrusted_parent_and_database_permissions_fail_before_lease_acquisition(self):
        if os.name != 'nt':
            self.directory.chmod(0o777)
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(self.database)
            self.assertFalse(self.lock_path.exists())
            self.directory.chmod(0o1777)
            if os.geteuid() != 0:
                with patch('os.geteuid',return_value=os.geteuid()+1):
                    with self.assertRaises(RuntimeBridgeError):
                        archive_writer_owner(self.directory/'untrusted-child'/'sessions.sqlite3')
                self.assertFalse((self.directory/'untrusted-child').exists())
            self.directory.chmod(0o700)
            self.database.write_bytes(b'synthetic')
            self.database.chmod(0o666)
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(self.database)
            self.assertFalse(self.lock_path.exists())
            self.database.chmod(0o600)
            with patch('os.geteuid',return_value=os.geteuid()+1):
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(self.database)
            self.assertFalse(self.lock_path.exists())
        with self.assertRaises(RuntimeBridgeError):
            archive_writer_owner(self.directory/'..'/'ambiguous.sqlite3')
