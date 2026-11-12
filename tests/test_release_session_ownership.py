"""Owned process-group escape regressions; no names, devices or foreign kills."""

from __future__ import annotations

import io
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout, suppress
from pathlib import Path
from unittest.mock import patch

from tests import run_release_checks as release


class EscapedCommandTests(unittest.TestCase):
    def scenario(self, mode: str) -> None:
        with tempfile.TemporaryDirectory(prefix="ori-session-escape-") as temporary:
            directory = Path(temporary)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(20)
            self.addCleanup(listener.close)
            port = listener.getsockname()[1]
            ready = directory / "child-ready"
            # This private socket is also a safe last-resort fixture lifeline.
            # Closing it releases the child even when testing the broken runner.
            child = textwrap.dedent(f"""
                import json,os,signal,socket,sys
                from pathlib import Path
                if os.name=='posix':os.setpgrp()
                if {mode == "kill"!r}:signal.signal(signal.SIGTERM,signal.SIG_IGN)
                connection=socket.create_connection(('127.0.0.1',{port}),timeout=5)
                connection.settimeout(30)
                record=dict(pid=os.getpid(),ppid=os.getppid(),pgid=os.getpgid(0) if os.name=='posix' else None,sid=os.getsid(0) if os.name=='posix' else None)
                connection.sendall(json.dumps(record).encode()+b'\\n')
                assert connection.recv(1)==b'g'
                Path({str(ready)!r}).write_text('ready')
                print('ready',flush=True)
                connection.recv(1)
                connection.close()
            """)
            parent = (
                "import subprocess,sys; "
                f"child=subprocess.Popen([sys.executable,'-u','-c',{child!r}],stdout=subprocess.PIPE); "
                "assert child.stdout.readline()==b'ready\\n'; "
                + ("child.stdout.close()" if mode == "zero" else "child.wait()")
            )
            report = directory / "outcome.json"
            controller = textwrap.dedent(f"""
                import _thread,json,os,subprocess,sys,threading,time
                from pathlib import Path
                from tests import run_release_checks as release
                def control():
                    if sys.stdin.buffer.readline()==b'interrupt\\n':_thread.interrupt_main()
                if {mode in ("interrupt", "kill")!r}:threading.Thread(target=control).start()
                environment=release.child_environment()
                root=Path({str(directory)!r})
                registry=root/'registry';registry.mkdir()
                environment['ORI_RELEASE_PROCESS_REGISTRY']=str(registry)
                target=release.process_ownership.OwnedCommand if os.name=='posix' else subprocess.Popen
                original_wait=target.wait
                def wait_after_readiness(owner,timeout=None):
                    if timeout!=10:return original_wait(owner,timeout=timeout)
                    deadline=time.monotonic()+20
                    while not Path({str(ready)!r}).exists():
                        if time.monotonic()>=deadline:raise RuntimeError('owned child did not become ready')
                        time.sleep(.005)
                    return original_wait(owner,timeout={0.1 if mode == "timeout" else 10!r})
                target.wait=wait_after_readiness
                try:
                    release.run_command([sys.executable,'-u','-c',{parent!r}],name='owned-escape',environment=environment,directory=root,timeout=10,shutdown_timeout=2)
                    outcome=dict(kind='returned')
                except BaseException as error:
                    outcome=dict(kind=type(error).__name__,message=str(error))
                Path({str(report)!r}).write_text(json.dumps(outcome))
            """)
            sibling_body = (
                "import socket; "
                f"connection=socket.create_connection(('127.0.0.1',{port}),timeout=5); "
                "connection.settimeout(30); connection.sendall(b'sibling\\n'); connection.recv(1)"
            )
            sibling = subprocess.Popen(
                [sys.executable, "-c", sibling_body],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **release._process_options(),
            )
            sibling_connection, _ = listener.accept()
            sibling_connection.settimeout(5)
            self.assertEqual(sibling_connection.recv(64), b"sibling\n")
            connection = None
            process = None
            log = (directory / "controller.log").open("w")
            try:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", controller],
                    cwd=release.ROOT,
                    env=release.child_environment(),
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **release._process_options(),
                )
                connection, _ = listener.accept()
                connection.settimeout(5)
                buffered = b""
                while b"\n" not in buffered:
                    chunk = connection.recv(1024)
                    self.assertTrue(chunk, "owned child exited before readiness")
                    buffered += chunk
                    self.assertLessEqual(len(buffered), 4096)
                identity = json.loads(buffered.split(b"\n", 1)[0])
                if os.name == "posix":
                    self.assertNotEqual(identity["pgid"], identity["sid"])
                    self.assertEqual(os.getsid(identity["pid"]), identity["sid"])
                connection.sendall(b"g")
                if mode in ("interrupt", "kill"):
                    process.stdin.write(b"interrupt\n")
                    process.stdin.flush()
                process.wait(timeout=15)
                self.assertEqual(process.returncode, 0)
                outcome = json.loads(report.read_text())
                readable, _, _ = select.select([connection], [], [], 0.1)
                child_closed = bool(readable) and connection.recv(1) == b""
                sibling_alive = sibling.poll() is None
                print(
                    json.dumps(
                        {
                            "mode": mode,
                            "identity": identity,
                            "outcome": outcome,
                            "child_control_closed": child_closed,
                            "sibling_alive": sibling_alive,
                        }
                    ),
                    flush=True,
                )
                expected = {
                    "timeout": "TimeoutExpired",
                    "interrupt": "KeyboardInterrupt",
                    "kill": "KeyboardInterrupt",
                    "zero": "ReleaseCheckError",
                }[mode]
                self.assertEqual(outcome["kind"], expected)
                self.assertTrue(
                    child_closed,
                    "an active owned child survived in a separate process group",
                )
                self.assertTrue(
                    sibling_alive, "cleanup affected an unrelated sibling session"
                )
            finally:
                if connection is not None:
                    with suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
                    connection.close()
                if process is not None:
                    with suppress(OSError):
                        process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                with suppress(OSError):
                    sibling_connection.shutdown(socket.SHUT_RDWR)
                sibling_connection.close()
                sibling.wait(timeout=5)
                log.close()

    def test_timeout_cleans_reparented_group_and_preserves_sibling(self):
        self.scenario("timeout")

    def test_interrupt_cleans_reparented_group_and_preserves_sibling(self):
        self.scenario("interrupt")

    def test_zero_exit_with_a_live_child_is_failure_and_owned_cleanup(self):
        self.scenario("zero")

    def test_interrupt_cleans_a_term_ignoring_child_and_preserves_sibling(self):
        self.scenario("kill")


if os.name == "posix" and sys.platform in ("linux", "darwin"):

    class DurableOwnershipTests(unittest.TestCase):
        def test_private_records_bound_bytes_and_reject_ambiguous_or_public_evidence(
            self,
        ):
            ownership = release.process_ownership
            with tempfile.TemporaryDirectory(prefix="ori-owner-records-") as temporary:
                directory = Path(temporary)
                path = directory / "record.json"
                ownership.write_record(path, {"version": 1, "owner": "a" * 32})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                with self.assertRaises(ownership.OwnershipError):
                    ownership.write_record(
                        path, {"version": 1, "data": "x" * ownership.MAX_RECORD_BYTES}
                    )
                self.assertEqual(ownership.read_record(path)["owner"], "a" * 32)
                path.chmod(0o644)
                with self.assertRaises(ownership.OwnershipError):
                    ownership.read_record(path)
                path.chmod(0o600)
                for raw in (
                    b'{"version":1,"version":1}',
                    b'{"version":true}',
                    b'{"version":1,"deadline":NaN}',
                    b"x" * (ownership.MAX_RECORD_BYTES + 1),
                ):
                    path.write_bytes(raw)
                    with self.assertRaises(ownership.OwnershipError):
                        ownership.read_record(path)
                link = directory / "link.json"
                link.symlink_to(path)
                with self.assertRaises(OSError):
                    ownership.read_record(link)
                directory.chmod(0o755)
                with self.assertRaises(ownership.OwnershipError):
                    ownership.read_record(path)
                directory.chmod(0o700)

        def test_failed_cleanup_retains_recovery_after_caller_directory_removal(self):
            owner = None
            start = release.process_ownership.OwnedCommand.start

            def remember(*args, **kwargs):
                nonlocal owner
                owner = start(*args, **kwargs)
                return owner

            try:
                with tempfile.TemporaryDirectory(
                    prefix="ori-owner-failed-"
                ) as temporary:
                    registry = Path(temporary)
                    with (
                        patch.object(
                            release.process_ownership.OwnedCommand,
                            "start",
                            side_effect=remember,
                        ),
                        patch.object(
                            release.process_ownership.OwnedCommand,
                            "release",
                            side_effect=release.process_ownership.OwnershipError(
                                "synthetic control failure"
                            ),
                        ),
                        patch.object(
                            release.process_ownership.OwnedCommand,
                            "stop",
                            side_effect=release.process_ownership.OwnershipError(
                                "synthetic cleanup failure"
                            ),
                        ),
                        redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            release.ReleaseCheckError, "recovery record:"
                        ),
                    ):
                        release.run_command(
                            [sys.executable, "-c", "pass"],
                            name="failed-cleanup",
                            environment=release.child_environment(),
                            directory=registry,
                            timeout=5,
                            shutdown_timeout=2,
                        )
                self.assertFalse(registry.exists())
                self.assertTrue((owner.directory / "active.session.json").exists())
                self.assertTrue((owner.directory / "recovery.json").exists())
                self.assertIs(
                    release.process_ownership._retained_commands[owner.directory], owner
                )
                owner.stop(2)
                self.assertFalse(owner.directory.exists())
            finally:
                if owner is not None and owner.directory.exists():
                    owner.stop(2)

        def test_actual_child_keeps_stdin_eof_and_negative_signal_exit(self):
            with tempfile.TemporaryDirectory(prefix="ori-owner-contract-") as temporary:
                registry = Path(temporary)
                with redirect_stdout(io.StringIO()):
                    release.run_command(
                        [
                            sys.executable,
                            "-c",
                            "import sys; assert sys.stdin.buffer.read()==b''",
                        ],
                        name="stdin-eof",
                        environment=release.child_environment(),
                        directory=registry,
                        timeout=5,
                        shutdown_timeout=2,
                    )
                    with self.assertRaisesRegex(
                        release.ReleaseCheckError, "status -15$"
                    ):
                        release.run_command(
                            [
                                sys.executable,
                                "-c",
                                "import os,signal; os.kill(os.getpid(),signal.SIGTERM)",
                            ],
                            name="negative-exit",
                            environment=release.child_environment(),
                            directory=registry,
                            timeout=5,
                            shutdown_timeout=2,
                        )

        def test_actual_child_resets_inherited_ignored_and_blocked_shutdown_signals(
            self,
        ):
            with tempfile.TemporaryDirectory(prefix="ori-owner-signals-") as temporary:
                body = textwrap.dedent(f"""
                    import signal,sys
                    from pathlib import Path
                    from tests import run_release_checks as release
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM, signal.SIGINT])
                    release.run_command([sys.executable, '-c',
                        'import signal; assert signal.getsignal(signal.SIGTERM)==signal.SIG_DFL; assert not ({{signal.SIGTERM,signal.SIGINT}} & signal.pthread_sigmask(signal.SIG_BLOCK, []))'],
                        name='shutdown-signals', environment=release.child_environment(),
                        directory=Path({temporary!r}), timeout=5, shutdown_timeout=2)
                """)
                completed = subprocess.run(
                    [sys.executable, "-c", body],
                    cwd=release.ROOT,
                    env=release.child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout.decode("utf-8", errors="replace"),
                )

        def test_scope_outlives_the_callers_temporary_registry(self):
            owner = None
            try:
                with tempfile.TemporaryDirectory(
                    prefix="ori-owner-caller-"
                ) as temporary:
                    registry = Path(temporary)
                    with (registry / "command.log").open("w") as log:
                        owner = release.process_ownership.OwnedCommand.start(
                            [sys.executable, "-c", "pass"],
                            environment=release.child_environment(),
                            registry=registry,
                            root=release.ROOT,
                            log=log,
                            shutdown_timeout=2,
                        )
                        self.assertEqual(owner.wait(5), 0)
                        self.assertNotIn(
                            registry,
                            owner.directory.parents,
                            "ownership evidence disappears with caller cleanup",
                        )
                        self.assertEqual(
                            list(
                                release.process_ownership.registered_commands(registry)
                            ),
                            [owner],
                        )
                self.assertFalse(registry.exists())
                recovery = release.process_ownership.read_record(
                    owner.directory / "recovery.json"
                )
                self.assertEqual(recovery["owner"], owner.meta["owner"])
                self.assertEqual(recovery["directory"], str(owner.directory))
                recovered = release.process_ownership.OwnedCommand(owner.directory)
                self.assertIs(recovered.process, owner.process)
                recovered.stop(2)
                owner.process.stdin.close()
                owner.process.wait(timeout=2)
                self.assertFalse(owner.directory.exists())
            finally:
                if owner is not None and owner.directory.exists():
                    owner.stop(2)


if sys.platform.startswith("linux"):

    class LivePinnedGroupTests(unittest.TestCase):
        def test_kernel_join_pins_group_through_term_kill_and_unreaped_closure(self):
            sibling = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import sys; print('ready',flush=True); sys.stdin.buffer.read()",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                self.assertEqual(sibling.stdout.readline(), b"ready\n")
                body = textwrap.dedent(f"""
                    import errno,json,os,signal,sys,time
                    from tests.release_process_ownership import LinuxKernel,PinnedProcessGroups
                    kernel=LinuxKernel()
                    anchor=kernel.identity(os.getpid())
                    groups=PinnedProcessGroups(kernel,anchor)
                    input_read,input_write=os.pipe()
                    ready_read,ready_write=os.pipe()
                    target=None; joined=False
                    try:
                        target=os.posix_spawnp(sys.executable,[sys.executable,'-I','-S','-c',
                            "import os,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b'ready'+bytes([10])); os.read(0,1)"],
                            os.environ,setpgroup=0,setsigmask=(),
                            file_actions=[(os.POSIX_SPAWN_DUP2,input_read,0),(os.POSIX_SPAWN_DUP2,ready_write,1),
                                          (os.POSIX_SPAWN_CLOSE,input_read),(os.POSIX_SPAWN_CLOSE,ready_write)])
                        os.close(input_read); input_read=None
                        os.close(ready_write); ready_write=None
                        assert os.read(ready_read,32)==b'ready\\n'
                        member=kernel.identity(target)
                        deadline=time.monotonic()+2
                        groups.signal(member,signal.SIGTERM,deadline)
                        held=groups.current[member.pgid]
                        keeper=kernel.identity(held.pid)
                        assert held.ready and held.identity.same_process(keeper) and not keeper.zombie
                        assert keeper.ppid==anchor.pid and keeper.pgid==member.pgid and keeper.sid==anchor.sid
                        assert os.waitpid(held.pid,os.WNOHANG)==(0,0)
                        try:
                            groups.spawn_keeper({sibling.pid})
                        except OSError as error:
                            assert error.errno==errno.EPERM
                        else:
                            raise AssertionError('kernel allowed a foreign-session group join')
                        groups.signal(member,signal.SIGKILL,deadline)
                        actual,status=os.waitpid(target,0); joined=True
                        assert actual==target and os.waitstatus_to_exitcode(status)==-signal.SIGKILL
                        until=time.monotonic()+1
                        while not kernel.identity(held.pid).zombie:
                            assert time.monotonic()<until
                            time.sleep(.005)
                        keeper=kernel.identity(held.pid)
                        assert held.identity.same_process(keeper) and keeper.pgid==member.pgid and keeper.sid==anchor.sid
                        groups.release(deadline)
                        try:os.waitpid(held.pid,os.WNOHANG)
                        except ChildProcessError:pass
                        else:raise AssertionError('keeper was not joined after verified closure')
                        print(json.dumps(dict(sid=anchor.sid,pgid=member.pgid,keeper=held.pid,
                                              kernel_rejected_foreign_join=True,joined_after_closure=True)))
                    finally:
                        os.close(input_write)
                        if input_read is not None:os.close(input_read)
                        if ready_write is not None:os.close(ready_write)
                        os.close(ready_read)
                        if target is not None and not joined:os.waitpid(target,0)
                        groups.release(time.monotonic()+2)
                """)
                completed = subprocess.run(
                    [sys.executable, "-c", body],
                    cwd=release.ROOT,
                    env=release.child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout.decode("utf-8", errors="replace"),
                )
                record = json.loads(completed.stdout)
                self.assertTrue(record["kernel_rejected_foreign_join"])
                self.assertTrue(record["joined_after_closure"])
                print(json.dumps(record), flush=True)
                self.assertIsNone(
                    sibling.poll(), "group signaling reached an unrelated session"
                )
            finally:
                sibling.stdin.close()
                sibling.stdout.close()
                sibling.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
