"""RSS admission must remain effective when the Windows ABI is used."""
from contextlib import ExitStack
import ctypes
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ori_research import resources
from ori_research.protocol import DEFAULT_LIMITS, WorkerError


class WindowsRssTests(unittest.TestCase):
    def windows(self, *, succeeds=True, peak=123_456_789):
        # Independent Windows LLP64 declaration: DWORD stays 32 bits on Linux.
        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_uint32), ('faults', ctypes.c_uint32)] + [
                (name, ctypes.c_size_t) for name in ('peak', 'current', 'a', 'b', 'c', 'd', 'e', 'f')]

        class Function:
            argtypes = None
            restype = ctypes.c_int

            def __init__(self, call):
                self.call = call

            def __call__(self, *args):
                value = self.call(*args)
                return self.restype(value).value

        handle = ctypes.c_void_p(-1).value
        get_current = Function(lambda: -1)

        def read(process, pointer, size):
            # Untyped GetCurrentProcess truncates this pseudo-handle on Win64.
            if process != handle:
                return 0
            self.assertEqual(get_current.argtypes, [])
            self.assertIs(get_current.restype, ctypes.c_void_p)
            self.assertIs(get_memory.argtypes[0], ctypes.c_void_p)
            self.assertIs(get_memory.argtypes[2], ctypes.c_uint32)
            self.assertIs(get_memory.restype, ctypes.c_int)
            self.assertEqual(size, 72 if ctypes.sizeof(ctypes.c_void_p) == 8 else 40)
            data = ctypes.cast(pointer, ctypes.POINTER(Counters)).contents
            self.assertEqual(data.cb, size)
            data.peak, data.current = peak, 123
            return int(succeeds)

        get_memory = Function(read)
        libraries = {'kernel32': SimpleNamespace(GetCurrentProcess=get_current),
                     'psapi': SimpleNamespace(GetProcessMemoryInfo=get_memory)}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(resources, 'os', SimpleNamespace(name='nt')))
        stack.enter_context(patch.object(resources, 'Path', Mock(return_value=Mock(exists=lambda: False))))
        stack.enter_context(patch.dict('sys.modules', {'resource': None}))
        stack.enter_context(patch.object(ctypes, 'windll', SimpleNamespace(**libraries), create=True))
        stack.enter_context(patch.object(ctypes, 'WinDLL',
            lambda name, **kwargs: libraries[name], create=True))

    def test_windows_peak_rss_uses_full_width_handle_and_documented_layout(self):
        self.windows()
        self.assertEqual(resources.resident_bytes(), 123_456_789)

    def test_windows_rss_admits_no_job_when_api_fails_or_reports_zero(self):
        for succeeds, peak in ((False, 123_456_789), (True, 0)):
            with self.subTest(succeeds=succeeds, peak=peak):
                self.windows(succeeds=succeeds, peak=peak)
                budget = resources.Budget(DEFAULT_LIMITS)
                with self.assertRaises(WorkerError) as caught:
                    budget.checkpoint()
                self.assertEqual(caught.exception.code, 'rss_measurement_unavailable')

    def test_windows_one_byte_budget_refuses_realistic_peak(self):
        self.windows()
        budget = resources.Budget(DEFAULT_LIMITS | {'worker_rss_bytes': 1})
        with self.assertRaises(WorkerError) as caught:
            budget.checkpoint()
        self.assertEqual(caught.exception.code, 'rss_limit_exceeded')
        self.assertEqual(budget.metrics['peak_rss_bytes'], 123_456_789)


class RssFailureTests(unittest.TestCase):
    def test_measurement_failure_returns_structured_worker_error_without_false_zero(self):
        from ori_research import __main__ as entry
        from ori_research import worker
        error = WorkerError('rss_measurement_unavailable', 'Worker memory usage is unavailable')
        request = {'version': 1, 'request_id': 'rss-test', 'operation': 'summary',
                   'snapshot_path': str(Path.cwd().resolve() / 'not-opened'),
                   'cache_directory': str(Path.cwd().resolve() / 'not-created')}
        output = io.BytesIO()
        with patch.object(entry.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                patch.object(entry.sys, 'stdout', SimpleNamespace(buffer=output)), \
                patch.object(resources, 'resident_bytes', side_effect=error), \
                patch.object(worker, 'run', side_effect=lambda request, budget: budget.checkpoint()):
            self.assertEqual(entry.main(), 2)
        response = json.loads(output.getvalue())
        self.assertFalse(response['ok'])
        self.assertEqual(response['error']['code'], 'rss_measurement_unavailable')
        self.assertIsNone(response['metrics']['peak_rss_bytes'])
        self.assertEqual(response['metrics']['rss_measurement_error'], 'rss_measurement_unavailable')

    def test_rss_watch_interrupts_query_and_surfaces_measurement_failure(self):
        from ori_research.queries import memory_watch
        import threading
        connection = SimpleNamespace(interrupt=Mock())
        interrupted = threading.Event()
        connection.interrupt.side_effect = interrupted.set
        error = WorkerError('rss_measurement_unavailable', 'Worker memory usage is unavailable')
        with patch('ori_research.queries.resident_bytes', side_effect=error):
            with self.assertRaises(WorkerError) as caught:
                with memory_watch(connection, resources.Budget(DEFAULT_LIMITS)):
                    self.assertTrue(interrupted.wait(1), 'Unavailable RSS must interrupt the query')
        self.assertEqual(caught.exception.code, 'rss_measurement_unavailable')
        connection.interrupt.assert_called_once_with()

    def test_successful_result_is_refused_if_final_rss_probe_fails(self):
        from ori_research import __main__ as entry
        from ori_research import worker
        error = WorkerError('rss_measurement_unavailable', 'Worker memory usage is unavailable')
        request = {'version': 1, 'request_id': 'rss-final', 'operation': 'summary',
                   'snapshot_path': str(Path.cwd().resolve() / 'not-opened'),
                   'cache_directory': str(Path.cwd().resolve() / 'not-created')}
        output = io.BytesIO()
        with patch.object(entry.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                patch.object(entry.sys, 'stdout', SimpleNamespace(buffer=output)), \
                patch.object(resources, 'resident_bytes', side_effect=error), \
                patch.object(worker, 'run', return_value={'rows': []}):
            self.assertEqual(entry.main(), 2)
        response = json.loads(output.getvalue())
        self.assertEqual(response['request_id'], 'rss-final')
        self.assertFalse(response['ok'])
        self.assertNotIn('result', response)
        self.assertEqual(response['error']['code'], 'rss_measurement_unavailable')

    def test_error_metrics_retain_later_valid_final_measurement(self):
        budget = resources.Budget(DEFAULT_LIMITS)
        with patch.object(resources, 'resident_bytes', return_value=123):
            budget.checkpoint()
        with patch.object(resources, 'resident_bytes', return_value=54321):
            self.assertEqual(budget.final_metrics(require_measurement=True)['peak_rss_bytes'], 54321)
        error = WorkerError('rss_measurement_unavailable', 'Worker memory usage is unavailable')
        with patch.object(resources, 'resident_bytes', side_effect=error):
            metrics = budget.final_metrics()
        self.assertEqual(metrics['peak_rss_bytes'], 54321)
        self.assertEqual(metrics['rss_measurement_error'], 'rss_measurement_unavailable')
