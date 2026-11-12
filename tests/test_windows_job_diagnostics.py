"""Bounded original-Job diagnostics; fake Win32 calls never grant cleanup authority."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import ctypes
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import run_release_checks as release


class ProcessList32(ctypes.Structure):
    _fields_ = [("assigned", ctypes.c_uint32), ("listed", ctypes.c_uint32),
                ("pids", ctypes.c_size_t * 32)]


class FileTime32(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class FakeJobApi:
    """Win32 boundary model with independent, fixed-width ABI assertions."""
    job_handle = 77

    def __init__(self, pids=(101,), *, query_error=0, **members):
        self.pids = pids
        self.query_error = query_error
        self.error = 0
        self.members = members
        self.events = []
        self.CreateJobObjectW = Mock(return_value=self.job_handle)
        self.SetInformationJobObject = Mock(return_value=1)
        self.AssignProcessToJobObject = Mock(return_value=1)
        self.TerminateJobObject = Mock(return_value=1)
        self.QueryInformationJobObject = Mock(side_effect=self.query)
        self.OpenProcess = Mock(side_effect=self.open)
        self.IsProcessInJob = Mock(side_effect=self.member)
        self.GetProcessTimes = Mock(side_effect=self.times)
        self.QueryFullProcessImageNameW = Mock(side_effect=self.image)
        self.WaitForSingleObject = Mock(side_effect=self.wait)
        self.GetExitCodeProcess = Mock(side_effect=self.exit_code)
        self.CloseHandle = Mock(side_effect=self.close)

    def query(self, handle, info, target, size, returned):
        assert handle == self.job_handle and info == 3
        assert size == 8 + 32 * ctypes.sizeof(ctypes.c_void_p)
        assert ProcessList32.pids.offset == 8
        value = ctypes.cast(target, ctypes.POINTER(ProcessList32)).contents
        value.assigned, value.listed = len(self.pids), len(self.pids)
        for index, pid in enumerate(self.pids[:32]):
            value.pids[index] = pid
        self.events.append("list")
        self.error = self.query_error
        return not self.query_error

    def data(self, handle):
        return self.members.get(str(handle - 1000), {})

    def open(self, access, inherit, pid):
        assert access == 0x00101000 and not inherit
        self.events.append(("open", pid))
        self.error = self.members.get(str(pid), {}).get("open_error", 0)
        return 0 if self.error else 1000 + pid

    def member(self, handle, job, result):
        assert job == self.job_handle
        self.events.append(("membership", handle))
        data = self.data(handle)
        self.error = data.get("membership_error", 0)
        ctypes.cast(result, ctypes.POINTER(ctypes.c_int32)).contents.value = data.get("member", True)
        return not self.error

    def _confirmed(self, handle, operation):
        assert self.data(handle).get("member", True), "foreign handle details were queried"
        self.events.append((operation, handle))

    def times(self, handle, created, exited, kernel, user):
        self._confirmed(handle, "birth")
        self.error = self.data(handle).get("birth_error", 0)
        value = ctypes.cast(created, ctypes.POINTER(FileTime32)).contents
        value.low, value.high = 123, 7
        return not self.error

    def image(self, handle, flags, buffer, length):
        self._confirmed(handle, "image")
        assert flags == 0 and len(buffer) == 1024
        count = ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents
        assert count.value == 1024
        image = "C:\\owned\\compiler.exe"
        buffer.value = image
        count.value = self.data(handle).get("image_length", len(image))
        self.error = self.data(handle).get("image_error", 0)
        return not self.error

    def wait(self, handle, milliseconds):
        self._confirmed(handle, "wait")
        assert milliseconds == 0
        self.error = self.data(handle).get("wait_error", 0)
        return self.data(handle).get("wait", 258)

    def exit_code(self, handle, result):
        self._confirmed(handle, "exit")
        ctypes.cast(result, ctypes.POINTER(ctypes.c_uint32)).contents.value = self.data(handle).get("exit", 259)
        self.error = self.data(handle).get("exit_error", 0)
        return not self.error

    def close(self, handle):
        self.events.append(("close", handle))
        self.error = self.data(handle).get("close_error", 0)
        return not self.error


class WindowsJobMemberDiagnosticsTests(unittest.TestCase):
    def snapshot(self, api):
        with patch.object(ctypes, "WinDLL", return_value=api, create=True), \
                patch.object(ctypes, "get_last_error", side_effect=lambda: api.error, create=True):
            job = release.WindowsJob()
            self.addCleanup(job.close)
            self.assertTrue(callable(getattr(job, "snapshot_diagnostics", None)),
                            "the original Windows Job has no bounded member diagnostic")
            return job.snapshot_diagnostics()

    def test_fixed_width_list_and_same_handle_birth_image_and_live_state(self):
        api = FakeJobApi()
        report = self.snapshot(api)
        self.assertEqual(report["status"], "complete")
        self.assertEqual((report["capacity"], report["assigned_count"], report["listed_count"]), (32, 1, 1))
        self.assertEqual(report["members"], [{"pid": 101, "membership": "confirmed",
            "birth_filetime": (7 << 32) | 123, "image": "C:\\owned\\compiler.exe",
            "wait": "not_signaled", "exit_code": 259}])
        self.assertEqual(api.events[1:4], [("open", 101), ("membership", 1101), ("birth", 1101)])
        self.assertEqual(api.events[-1], ("close", 1101))
        api.TerminateJobObject.assert_not_called()

    def test_signaled_exit_259_is_not_misreported_as_a_live_process(self):
        report = self.snapshot(FakeJobApi(**{"101": {"wait": 0, "exit": 259}}))
        self.assertEqual(report["members"][0]["wait"], "signaled")
        self.assertEqual(report["members"][0]["exit_code"], 259)

    def test_foreign_or_reused_pid_has_no_image_birth_or_wait_details(self):
        for data, expected in (({"member": False}, "not_in_original_job"),
                               ({"membership_error": 5}, "query_failed"),
                               ({"open_error": 5}, "open_failed")):
            with self.subTest(data=data):
                api = FakeJobApi(**{"101": data})
                row = self.snapshot(api)["members"][0]
                self.assertEqual(row["membership"], expected)
                self.assertFalse({"image", "birth_filetime", "wait", "exit_code"} & row.keys())
                self.assertEqual(api.GetProcessTimes.call_count, 0)
                self.assertEqual(api.QueryFullProcessImageNameW.call_count, 0)
                self.assertEqual(api.CloseHandle.call_count, 0 if "open_error" in data else 1)

    def test_overflow_and_failed_or_invalid_list_are_bounded_without_opening_pids(self):
        for api, status in ((FakeJobApi(tuple(range(100, 133)), query_error=234), "overflow"),
                            (FakeJobApi(query_error=5), "query_failed"),
                            (FakeJobApi(tuple(range(100, 133))), "invalid_count")):
            with self.subTest(status=status):
                report = self.snapshot(api)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["members"], [])
                self.assertEqual(api.QueryInformationJobObject.call_count, 1)
                api.OpenProcess.assert_not_called()

    def test_detail_errors_remain_uncertain_and_handles_always_close(self):
        for data, operation in (({"birth_error": 5}, "GetProcessTimes"),
                                ({"image_error": 122}, "QueryFullProcessImageNameW"),
                                ({"image_length": 1025}, "QueryFullProcessImageNameW"),
                                ({"wait": 0xFFFFFFFF, "wait_error": 6}, "WaitForSingleObject"),
                                ({"exit_error": 6}, "GetExitCodeProcess"),
                                ({"close_error": 6}, "CloseHandle")):
            with self.subTest(data=data):
                api = FakeJobApi(**{"101": data})
                row = self.snapshot(api)["members"][0]
                self.assertIn(operation, [error["operation"] for error in row["errors"]])
                api.CloseHandle.assert_called_once_with(1101)

    def test_unexpected_collector_exception_still_closes_the_query_handle(self):
        api = FakeJobApi()
        api.GetProcessTimes.side_effect = RuntimeError("untrusted message must not be logged")
        with self.assertRaisesRegex(RuntimeError, "untrusted"):
            self.snapshot(api)
        api.CloseHandle.assert_called_once_with(1101)

    def test_closed_original_job_is_uncertain_without_a_new_handle_lookup(self):
        api = FakeJobApi()
        with patch.object(ctypes, "WinDLL", return_value=api, create=True):
            job = release.WindowsJob()
            job.close()
            self.assertTrue(callable(getattr(job, "snapshot_diagnostics", None)))
            self.assertEqual(job.snapshot_diagnostics()["status"], "closed")
        api.QueryInformationJobObject.assert_not_called()
        api.OpenProcess.assert_not_called()


class WindowsJobGateReportTests(unittest.TestCase):
    def run_failed_gate(self, folder, *, collection_error=None, save_error=False):
        job = Mock()
        events = []
        job.active.return_value = 0
        snapshot = {"version": 1, "status": "complete", "capacity": 32,
                    "members": [{"pid": 101, "membership": "confirmed"}]}
        def collect():
            events.append("snapshot")
            if collection_error:
                raise collection_error
            return snapshot
        job.snapshot_diagnostics.side_effect = collect
        job.terminate.side_effect = lambda: events.append("terminate_original_job")
        job.close.side_effect = lambda: events.append("close_original_job")
        process = Mock(returncode=0, stdin=io.BytesIO())
        with ExitStack() as stack:
            stack.enter_context(patch.object(release.sys, "platform", "win32"))
            stack.enter_context(patch.object(release, "WindowsJob", return_value=job))
            stack.enter_context(patch.object(release, "_job_still_active", return_value=True))
            stack.enter_context(patch.object(release, "_process_options", return_value={}))
            stack.enter_context(patch.object(release.subprocess, "Popen", return_value=process))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if save_error:
                stack.enter_context(patch.object(Path, "write_text", side_effect=OSError("disk denied")))
            with self.assertRaisesRegex(release.ReleaseCheckError,
                    "^modern-owned: command left an active owned descendant$"):
                release.run_command(["private-command"], name="modern-owned", environment={},
                                    directory=folder, timeout=1, shutdown_timeout=.01)
        job.terminate.assert_called_once_with()
        job.close.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(events, ["snapshot", "terminate_original_job", "close_original_job"])
        return folder / "modern-owned.windows-job.json"

    def test_member_evidence_is_saved_before_original_job_cleanup_and_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            evidence = self.run_failed_gate(directory)
            self.assertTrue(evidence.is_file(), "strict Windows gate discarded original Job membership evidence")
            self.assertEqual(json.loads(evidence.read_text())["members"][0]["pid"], 101)
            reports = directory / "reports"
            with redirect_stdout(io.StringIO()):
                release._persist_release_report(directory, reports, failure=release.ReleaseCheckError("gate"),
                                                cleanup_errors=[], swift=False, modern=True)
            self.assertEqual(len(list(reports.glob("run-*/modern-owned.windows-job.json"))), 1)

    def test_collection_failure_is_redacted_and_does_not_replace_gate_or_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self.run_failed_gate(Path(temporary), collection_error=RuntimeError("secret child data"))
            report = json.loads(evidence.read_text())
            self.assertEqual(report["status"], "collection_failed")
            self.assertEqual(report["error_type"], "RuntimeError")
            self.assertNotIn("secret", evidence.read_text())

    def test_save_failure_does_not_replace_gate_or_original_job_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_failed_gate(Path(temporary), save_error=True)


if __name__ == "__main__":
    unittest.main()
