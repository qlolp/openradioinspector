"""Controlled API-tool children retain actual status and descendant authority."""

import json
import os
import signal
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.owned_fixture_io import CAPTURED_STDOUT, READY_STDOUT, owned_control_closed
from tests.release_process_ownership import OwnershipError
from tools.modernization import owned_process
from tools.modernization.api_parity import NodeSpec, OwnedNode, ProtocolError, probe_cli


def inherited_pipe_scenario(self, *, changed_group):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        port = listener.getsockname()[1]
        child = (
            "import json,os,socket;"
            f"os.setpgrp() if {changed_group!r} and os.name=='posix' else None;"
            f"connection=socket.create_connection(('127.0.0.1',{port}),timeout=10);"
            "connection.settimeout(15);"
            "connection.sendall(json.dumps({'pid':os.getpid(),'sid':os.getsid(0) if os.name=='posix' else None,'pgid':os.getpgrp() if os.name=='posix' else None}).encode()+b'\\n');"
            + READY_STDOUT + ";connection.recv(1);connection.close()"
        )
        parent = (
            "import subprocess,sys;"
            f"child=subprocess.Popen([sys.executable,'-u','-c',{child!r}],stdout=subprocess.PIPE);"
            "assert child.stdout.readline()==b'ready\\n';child.stdout.close()"
        )
        results, errors, processes = [], [], []
        fixture_ready = threading.Event()
        wait = owned_process.ManagedProcess.wait

        def wait_after_fixture_ready(process, timeout):
            processes.append(process)
            if not fixture_ready.wait(10):
                raise AssertionError("controlled child did not reach readiness")
            return wait(process, timeout)

        def run():
            try:
                results.append(
                    probe_cli([sys.executable, "-u", "-c", parent], timeout=1)
                )
            except Exception as error:  # noqa: BLE001 - propagate thread errors to the test
                errors.append(error)

        running = threading.Thread(target=run)
        wait_patch = patch.object(
            owned_process.ManagedProcess, "wait", wait_after_fixture_ready
        )
        wait_patch.start()
        running.start()
        connection = None
        try:
            connection, _ = listener.accept()
            connection.settimeout(10)
            with connection.makefile("rb") as record_stream:
                record = json.loads(record_stream.readline())
            fixture_ready.set()
            self.assertGreater(record["pid"], 0)
            if os.name == "posix":
                if changed_group:
                    self.assertEqual(record["pid"], record["pgid"])
                    self.assertNotEqual(record["pgid"], record["sid"])
                else:
                    self.assertNotEqual(record["pid"], record["pgid"])
            running.join(7)
            self.assertFalse(
                running.is_alive(), "probe did not bound its owned cleanup"
            )
            self.assertEqual(errors, [])
            self.assertFalse(results[0]["successful"])
            self.assertEqual(results[0]["error"], "command_deadline")
            self.assertNotIn("cleanup_error", results[0])
            self.assertNotIn("recovery", results[0])
            self.assertEqual(results[0]["exit_code"], 0)
            joined = bool(processes) and all(process.closed is True for process in processes)
            self.assertTrue(joined, "original managed session/Job did not prove closure")
            connection.settimeout(0.2)
            self.assertTrue(
                owned_control_closed(connection, joined=joined),
                "owned changed-group descendant survived cleanup",
            )
        finally:
            if connection is not None:
                connection.close()  # Explicit fixture lifeline; no PID/name signals.
            fixture_ready.set()
            running.join(10)
            wait_patch.stop()
            self.assertFalse(running.is_alive())


class ActualProbeOwnershipTests(unittest.TestCase):
    def test_reports_the_actual_native_child_exit(self):
        script = (
            "import os,signal;os.kill(os.getpid(),signal.SIGTERM)"
            if os.name == "posix"
            else "raise SystemExit(3)"
        )
        result = probe_cli([sys.executable, "-c", script])
        self.assertFalse(result["successful"])
        self.assertEqual(
            result["exit_code"], -signal.SIGTERM if os.name == "posix" else 3
        )

    def test_changed_group_descendant_pipe_is_closed_after_leader_exit(self):
        inherited_pipe_scenario(self, changed_group=True)


class ManagedProcessTests(unittest.TestCase):
    def test_probe_reports_unknown_cleanup_and_keeps_recoverable_original_authority(
        self,
    ):
        processes = []
        start = owned_process.ManagedProcess.start

        def remember(process):
            processes.append(process)
            return start(process)

        try:
            with (
                patch.object(owned_process.ManagedProcess, "start", remember),
                patch.object(
                    owned_process.ManagedProcess,
                    "finish",
                    side_effect=OwnershipError("controlled release uncertainty"),
                ),
                patch.object(
                    owned_process.ManagedProcess,
                    "stop",
                    side_effect=OwnershipError("controlled cleanup uncertainty"),
                ),
            ):
                result = probe_cli([sys.executable, "-c", CAPTURED_STDOUT])
            self.assertFalse(result["successful"])
            self.assertIn("controlled release uncertainty", result["error"])
            self.assertIn("controlled cleanup uncertainty", result["cleanup_error"])
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "captured\n")
            self.assertTrue(Path(result["recovery"]).is_file())
            self.assertFalse(processes[0].closed)
        finally:
            for process in processes:
                process.stop(2)
                process.dispose()

    def test_merged_node_output_has_one_aggregate_budget(self):
        process = owned_process.ManagedProcess(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('o'*150000);sys.stderr.write('e'*150000)",
            ],
            merge=True,
        ).start()
        try:
            with self.assertRaisesRegex(owned_process.ManagedError, "output_budget"):
                process.wait(5)
            process.stop(2)
            stdout, stderr = process.outputs()
            self.assertEqual(len(stdout), 256 * 1024)
            self.assertEqual(stderr, b"")
        finally:
            process.stop(2)
            process.dispose()

    def test_stream_caps_are_separate_and_native_stderr_is_not_merged(self):
        result = probe_cli(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('o'*262144);sys.stderr.write('e'*262144)",
            ]
        )
        self.assertTrue(
            result["successful"],
            {key: result[key] for key in ("state", "error", "exit_code")},
        )
        self.assertEqual(result["stdout"], "o" * 262144)
        self.assertEqual(result["stderr"], "e" * 262144)

    def test_failed_release_retains_an_independent_lease_then_original_authority_recovers(
        self,
    ):
        caller = tempfile.TemporaryDirectory()
        process = owned_process.ManagedProcess(
            [sys.executable, "-c", "print('complete')"], cwd=caller.name
        ).start()
        try:
            self.assertEqual(process.wait(5), 0)
            authority_handle = (
                process.owner if process.owner is not None else process.job
            )
            method = "release" if process.owner is not None else "close"
            with (
                patch.object(
                    authority_handle,
                    method,
                    side_effect=OwnershipError("controlled unknown release"),
                ),
                self.assertRaises(OwnershipError),
            ):
                process.finish()
            self.assertFalse(process.closed)
            lease = Path(process.recovery)
            caller.cleanup()
            self.assertFalse(Path(caller.name).exists())
            self.assertTrue(lease.is_file())
            authority = json.loads((process.directory / "authority.json").read_text())
            if process.owner is not None:
                self.assertEqual(authority["directory"], str(process.owner.directory))
            else:
                self.assertEqual(authority["kind"], "retained_windows_job")
            with self.assertRaises(owned_process.ManagedError):
                process.dispose()
            process.stop(2)
            process.stop(2)
            self.assertTrue(process.closed)
            self.assertEqual(process.returncode, 0)
            process.dispose()
            process.dispose()
            self.assertFalse(lease.exists())
        finally:
            caller.cleanup()
            process.stop(2)
            process.dispose()

    def test_windows_assignment_precedes_go_and_native_dword_is_preserved(self):
        calls = []
        bootstrap = Mock()
        bootstrap.stdin.write.side_effect = lambda _value: calls.append("go")
        bootstrap.poll.return_value = 0
        job = Mock()
        job.assign.side_effect = lambda _process: calls.append("assign")
        job.active.return_value = 0
        process = owned_process.ManagedProcess(["native-test-child"])
        fake_os = Mock(wraps=os)
        fake_os.name = "nt"
        try:
            with (
                patch.object(owned_process, "os", fake_os),
                patch("tests.run_release_checks.WindowsJob", return_value=job),
                patch.object(owned_process.subprocess, "Popen", return_value=bootstrap),
                patch.object(
                    owned_process.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                process.start()
                self.assertEqual(calls, ["assign", "go"])
                owned_process._publish(
                    process.directory / "exit.json",
                    {"nonce": process.nonce, "code": 0xC0000005},
                )
                owned_process._publish(
                    process.directory / "complete.json",
                    {"nonce": process.nonce, "streams_closed": True},
                )
                self.assertEqual(process.wait(1), 0xC0000005)
                process.finish()
                job.terminate.assert_not_called()
                job.close.assert_called_once()
        finally:
            if not process.closed:
                process.stop(2)
            process.dispose()

    def test_windows_failed_assignment_never_releases_the_child_gate(self):
        bootstrap, job = Mock(), Mock()
        bootstrap.stdin.closed = False
        job.assign.side_effect = OSError("controlled assignment failure")
        job.active.return_value = 0
        process = owned_process.ManagedProcess(["never-executed-child"])
        fake_os = Mock(wraps=os)
        fake_os.name = "nt"
        try:
            with (
                patch.object(owned_process, "os", fake_os),
                patch("tests.run_release_checks.WindowsJob", return_value=job),
                patch.object(owned_process.subprocess, "Popen", return_value=bootstrap),
                patch.object(
                    owned_process.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
                self.assertRaises(OSError),
            ):
                process.start()
            bootstrap.stdin.write.assert_not_called()
            bootstrap.stdin.close.assert_called_once()
            bootstrap.terminate.assert_called_once()
            job.terminate.assert_not_called()
            self.assertTrue(process.closed)
        finally:
            if not process.closed:
                process.stop(2)
            process.dispose()

    def test_windows_unknown_accounting_retains_the_original_job_for_late_cleanup(self):
        bootstrap, job = Mock(), Mock()
        bootstrap.poll.return_value = 0
        process = owned_process.ManagedProcess(["owned-child"])
        fake_os = Mock(wraps=os)
        fake_os.name = "nt"
        try:
            with (
                patch.object(owned_process, "os", fake_os),
                patch("tests.run_release_checks.WindowsJob", return_value=job),
                patch.object(owned_process.subprocess, "Popen", return_value=bootstrap),
                patch.object(
                    owned_process.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    512,
                    create=True,
                ),
            ):
                process.start()
            with self.assertRaisesRegex(
                owned_process.ManagedError, "incomplete_child_closure"
            ):
                process.wait(1)
            job.active.side_effect = OSError("controlled unknown accounting")
            with self.assertRaises(OSError):
                process.stop(1)
            self.assertFalse(process.closed)
            self.assertTrue(Path(process.recovery).is_file())
            self.assertIs(owned_process._retained[process.directory], process)
            job.close.assert_not_called()
            job.active.side_effect = None
            job.active.return_value = 0
            process.stop(1)
            self.assertTrue(process.closed)
            self.assertIsNone(
                process.returncode, "bootstrap exit cannot invent a child status"
            )
            job.close.assert_called_once()
        finally:
            job.active.side_effect = None
            job.active.return_value = 0
            if not process.closed:
                process.stop(2)
            process.dispose()


class ActualNodeOwnershipTests(unittest.TestCase):
    def test_leader_zero_and_removed_readiness_do_not_claim_join_with_a_live_descendant(
        self,
    ):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as temporary:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(10)
            port = listener.getsockname()[1]
            child = (
                "import os,socket;os.setpgrp() if os.name=='posix' else None;"
                f"connection=socket.create_connection(('127.0.0.1',{port}),timeout=10);"
                "connection.settimeout(15);" + READY_STDOUT + ";connection.recv(1)"
            )

            class FixtureNode(OwnedNode):
                def _command(self, _data):
                    parent = (
                        "import os,subprocess,sys,time;from pathlib import Path;"
                        f"child=subprocess.Popen([sys.executable,'-u','-c',{child!r}],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);"
                        "assert child.stdout.readline()==b'ready\\n';child.stdout.close();"
                        f"ready=Path({str(self.ready)!r});"
                        "pending=ready.with_suffix('.pending');"
                        "stream=pending.open('w',encoding='utf-8');time.sleep(.2);"
                        "stream.write('http://127.0.0.1:12345\\n');stream.close();"
                        "os.replace(pending,ready);"
                        "sys.stdin.buffer.read();ready.unlink()"
                    )
                    return [sys.executable, "-u", "-c", parent]

            root = Path(__file__).resolve().parents[1]
            node = FixtureNode(
                NodeSpec("core", Path(sys.executable), root), Path(temporary) / "node"
            )
            connection = None
            try:
                node.__enter__()
                connection, _ = listener.accept()
                connection.settimeout(1)
                with self.assertRaises(ProtocolError):
                    node.close()
                self.assertEqual(node.process.returncode, 0)
                self.assertTrue(
                    node.joined,
                    "forced cleanup must still prove every owned member closed",
                )
                self.assertTrue(owned_control_closed(connection, joined=node.joined is True and node.process.closed is True))
                self.assertFalse(node.ready.exists())
            finally:
                if connection is not None:
                    connection.close()
                if not node.joined:
                    node.close()


if __name__ == "__main__":
    unittest.main()
