"""Controlled Windows reset models and real forced-CRLF synthetic producers."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import owned_fixture_io as fixture
from tools.modernization.api_parity import probe_cli


def reset(code=10054):
    error = ConnectionResetError("controlled private socket reset")
    error.winerror = code
    return error


class OwnedFixtureIOTests(unittest.TestCase):
    def test_only_windows10054_after_independent_join_is_closed(self):
        connection = Mock()
        connection.recv.side_effect = reset()
        with patch.object(fixture.sys, "platform", "win32"):
            try:
                closed = fixture.owned_control_closed(connection, joined=True)
            except ConnectionResetError as error:
                self.fail("A joined private Windows control socket reset was not recognized: " + str(error))
        self.assertTrue(closed)

    def test_unproved_closure_never_reads_or_accepts_reset(self):
        for joined in (False, None, 1):
            connection = Mock()
            connection.recv.side_effect = reset()
            with self.subTest(joined=joined), self.assertRaises(AssertionError):
                fixture.owned_control_closed(connection, joined=joined)
            connection.recv.assert_not_called()

    def test_other_platform_reset_error_or_timeout_is_not_eof(self):
        for platform, error in (("linux", reset()), ("win32", reset(10053)),
                                ("win32", ConnectionResetError("no native code")),
                                ("win32", TimeoutError("still waiting")),
                                ("win32", ConnectionAbortedError("not the closed reset"))):
            connection = Mock()
            connection.recv.side_effect = error
            with self.subTest(platform=platform, error=type(error).__name__), patch.object(fixture.sys, "platform", platform):
                with self.assertRaises(type(error)):
                    fixture.owned_control_closed(connection, joined=True)

    def test_received_data_is_not_closed_and_orderly_eof_stays_closed(self):
        self.assertFalse(fixture.owned_control_closed(Mock(recv=lambda _n: b"x"), joined=True))
        self.assertTrue(fixture.owned_control_closed(Mock(recv=lambda _n: b""), joined=True))

    def test_original_join_proof_requires_released_owners_or_empty_closed_jobs(self):
        self.assertFalse(fixture.command_joined([], []))
        self.assertFalse(fixture.command_joined([SimpleNamespace(_released=False)], []))
        self.assertFalse(fixture.command_joined([], [SimpleNamespace(closed_empty=False)]))
        self.assertFalse(fixture.command_joined([SimpleNamespace(_released=1)], []))
        self.assertFalse(fixture.command_joined([SimpleNamespace(_released=True)], [SimpleNamespace(closed_empty=False)]))
        self.assertTrue(fixture.command_joined([SimpleNamespace(_released=True)], []))
        self.assertTrue(fixture.command_joined([], [SimpleNamespace(closed_empty=True)]))

    def test_job_observer_requires_zero_accounting_before_original_close(self):
        for accounting in (0, 1, False, OSError("unknown Job accounting")):
            original_closed = []

            class Job:
                def active(self):
                    if isinstance(accounting, Exception):
                        raise accounting
                    return accounting

                def close(self):
                    original_closed.append(True)

            release = SimpleNamespace(WindowsJob=Job, process_ownership=SimpleNamespace(OwnedCommand=type("UnusedOwner", (), {})))
            with self.subTest(accounting=accounting), fixture.record_command_closure(release) as proof:
                job = release.WindowsJob()
                self.assertFalse(proof.joined())
                if isinstance(accounting, Exception):
                    with self.assertRaises(OSError):
                        job.close()
                else:
                    job.close()
                self.assertEqual(proof.joined(), type(accounting) is int and accounting == 0)
                self.assertEqual(original_closed, [True])

    def test_posix_observer_uses_the_original_handle_release(self):
        class OriginalOwner:
            def __init__(self):
                self._released = False

            @classmethod
            def start(cls, *_args, **_kwargs):
                return cls()

        release = SimpleNamespace(WindowsJob=type("UnusedJob", (), {}),
                                  process_ownership=SimpleNamespace(OwnedCommand=OriginalOwner))
        with fixture.record_command_closure(release) as proof:
            captured = release.process_ownership.OwnedCommand.start(["original-model"])
            self.assertIsInstance(captured, OriginalOwner)
            self.assertFalse(proof.joined())
            captured._released = True
            self.assertTrue(proof.joined())

    def test_owner_admitted_before_interrupted_start_remains_observable(self):
        admitted = []

        class OriginalOwner:
            def __init__(self):
                self._released = False

            @classmethod
            def start(cls):
                owner = cls()
                admitted.append(owner)
                raise KeyboardInterrupt

        release = SimpleNamespace(WindowsJob=type("UnusedJob", (), {}),
                                  process_ownership=SimpleNamespace(OwnedCommand=OriginalOwner))
        with fixture.record_command_closure(release) as proof:
            with self.assertRaises(KeyboardInterrupt):
                OriginalOwner.start()
            self.assertFalse(proof.joined())
            admitted[0]._released = True  # Original recovery/cleanup confirmed release.
            self.assertTrue(proof.joined(), "observer lost the original admitted handle when start was interrupted")

    def test_original_synthetic_stdout_is_binary_lf_even_with_crlf_text_wrapper(self):
        for producer, wanted in ((fixture.READY_STDOUT, "ready\n"), (fixture.CAPTURED_STDOUT, "captured\n")):
            command = "import io,sys;sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',newline='\\r\\n',write_through=True);" + producer
            with self.subTest(wanted=wanted):
                result = probe_cli([sys.executable, "-c", command])
                self.assertTrue(result["successful"], result)
                self.assertEqual(result["stdout"], wanted)
                self.assertEqual(result["stderr"], "")


if __name__ == "__main__":
    unittest.main()
