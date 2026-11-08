"""Timer lifetime regressions using Tcl only, plus opt-in withdrawn Tk roots.

No hardware, network, user startup profiles or native dialogs are required.
Tcl bgerror is captured separately: report_callback_exception cannot observe a
timer whose Python command has already been deleted.
"""

from __future__ import annotations

import os
import queue
import threading
import unittest
import weakref
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

from radioinspector.windows.app import WindowsApp
from radioinspector.windows.tk_timers import OwnedTimers


class TclRoot:
    """Real Tcl timers with a small, non-graphical root-destruction model."""

    def __init__(self):
        import tkinter as tk

        # Avoid Tk.__init__/readprofile completely, even outside the runner's
        # isolated HOME. Tcl alone never loads Tk or presents a dialog.
        import _tkinter
        self.interpreter = tk.Tk.__new__(tk.Tk)
        self.interpreter.master = None
        self.interpreter.tk = _tkinter.create(None, "ori-timer-test", "Tk", False,
                                             True, False, False, None)
        self.interpreter._tclCommands = None
        self.tk = self.interpreter.tk
        self.alive = True
        self.destroy_count = 0
        self.owner_thread = threading.get_ident()
        self.disposed = False
        self.handlers = []
        self.errors = []
        self.python_errors = []
        self.interpreter.report_callback_exception = lambda *error: self.python_errors.append(error)
        # Register directly, so Misc.destroy leaves this test-only error sink
        # alive long enough to observe callbacks from the destroyed root.
        self.tk.createcommand("bgerror", lambda message: self.errors.append(message))

    def protocol(self, *_args):
        pass

    def bind(self, event, callback, add=None):
        assert event == "<Destroy>"
        self.handlers.append(callback)
        return str(len(self.handlers))

    def after(self, delay, callback, *args):
        return self.interpreter.after(delay, callback, *args)

    def after_cancel(self, identifier):
        return self.interpreter.after_cancel(identifier)

    def pending(self):
        return self.tk.splitlist(self.tk.call("after", "info"))

    def destroy(self):
        import tkinter as tk

        self.destroy_count += 1
        self.alive = False
        for callback in tuple(self.handlers):
            callback(SimpleNamespace(widget=self))
        tk.Misc.destroy(self.interpreter)

    def pump(self, delay=0):
        if delay:
            self.tk.call("after", delay)
        self.tk.call("update")

    def dispose(self):
        if self.disposed:
            return
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("TclRoot must be disposed on its creator thread")
        for identifier in self.pending():
            self.after_cancel(identifier)
        if self.alive:
            self.destroy()
        self.tk.deletecommand("bgerror")
        self.handlers.clear()
        self.interpreter.report_callback_exception = None
        # Tcl interpreters must be released on their creator thread, even when
        # an assertion/mock/callback leaves the Python fixture in a GC cycle.
        self.interpreter.tk = None
        self.tk = None
        self.interpreter = None
        self.disposed = True


class WindowsTimerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = TclRoot()
        self.addCleanup(self.root.dispose)
        with patch.object(WindowsApp, "_build"), patch("radioinspector.windows.app.ThreadPoolExecutor"):
            self.app = WindowsApp(self.root, Mock(), Mock(), refresh_ms=1)
        self.app.refresh = Mock()
        self.app._render_controls = Mock()
        self.app._apply_snapshot = Mock()
        self.app.action_var = Mock()
        self.app.connection_var = Mock()

    def test_manual_drains_and_polls_keep_one_pending_timer_each(self):
        self.assertEqual(len(self.root.pending()), 2)
        for _ in range(5):
            self.app._drain_messages()
            self.app._poll()
            self.assertEqual(len(self.root.pending()), 2)

    def test_close_before_first_tick_cancels_tcl_callbacks(self):
        self.app.close()
        self.assertEqual(self.root.pending(), ())
        self.root.pump(110)
        self.assertEqual(self.root.errors, [])
        self.assertEqual(self.root.python_errors, [])
        self.app.close()
        self.assertEqual(self.root.destroy_count, 1)
        self.app._executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_close_after_tick_cancels_rescheduled_callbacks(self):
        self.root.pump(2)
        self.assertEqual(len(self.root.pending()), 2)
        self.app.close()
        self.root.pump(110)
        self.assertEqual(self.root.errors, [])
        self.assertEqual(self.root.pending(), ())

    def test_direct_root_destruction_shuts_down_without_destroying_again(self):
        self.root.destroy()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.root.pending(), ())
        self.root.pump(110)
        self.assertEqual(self.root.errors, [])
        self.app.close()
        self.assertEqual(self.root.destroy_count, 1)
        self.app._executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_child_destroy_event_does_not_close_app(self):
        self.assertTrue(self.root.handlers)
        for callback in self.root.handlers:
            callback(SimpleNamespace(widget=object()))
        self.assertFalse(self.app._closed)
        self.assertEqual(len(self.root.pending()), 2)

    def test_success_callback_can_close_app_mid_drain(self):
        future = Future()
        future.set_result("done")
        self.app._action_callbacks[future] = lambda _result: self.app.close()
        self.app._messages.put(("action", future, self.app._revision, "test"))
        self.app._drain_messages()
        self.app.action_var.set.assert_not_called()
        self.app.refresh.assert_not_called()
        self.assertEqual(self.root.pending(), ())

    def test_scheduled_success_callback_can_destroy_root_during_dispatch(self):
        future = Future()
        future.set_result("done")
        self.app._action_callbacks[future] = lambda _result: self.app.close()
        self.app._messages.put(("action", future, self.app._revision, "test"))
        self.root.pump()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.root.errors, [])
        self.assertEqual(self.root.python_errors, [])
        self.assertEqual(self.root.pending(), ())

    def test_reconnect_discards_old_result_without_duplicate_timers(self):
        self.app.server_var = Mock()
        self.app._show_archive_capacity = Mock()
        self.app.client_factory = Mock()
        old_revision = self.app._revision
        self.app.connect()
        self.app.refresh.reset_mock()
        future = Future()
        future.set_result("old snapshot")
        self.app._refresh_running = True
        self.app._messages.put(("snapshot", future, old_revision, ""))
        self.app._drain_messages()
        self.app._apply_snapshot.assert_not_called()
        self.assertFalse(self.app._refresh_running)
        self.app.refresh.assert_called_once()
        self.assertEqual(len(self.root.pending()), 2)

    def test_session_switch_resets_context_and_discards_pending_completion(self):
        from radioinspector.windows.client import PlatformSnapshot

        self.app.client = Mock()
        self.app.session_id = "old"
        self.app.snapshot = PlatformSnapshot({}, {}, [{"id": "old"}, {"id": "new"}],
                                            session={"id": "old", "streams": [{"id": "stream"}]})
        self.app.session_box = Mock()
        self.app.session_box.current.return_value = 1
        for name in ("streams_tree", "candidates_tree", "investigations_tree"):
            tree = Mock()
            tree.get_children.return_value = ("stale",)
            setattr(self.app, name, tree)
        self.app.next_investigation_var = Mock()
        self.app._clear_spectrum = Mock()
        self.app._destroy_form = Mock()
        self.app._active_form = form = object()
        self.app._last_selected_stream_id = "stream"
        self.app.sweep_context_panel = Mock()
        future = Future()
        self.app._executor.submit.return_value = future
        success = Mock()
        self.app._submit_action("old action", Mock(), success)
        revision = self.app._revision

        self.app._select_session()
        self.assertEqual(self.app._revision, revision + 1)
        self.assertEqual(self.app.session_id, "new")
        self.assertIsNone(self.app.snapshot.session)
        self.assertIsNone(self.app._last_selected_stream_id)
        self.app._destroy_form.assert_called_once_with(form)
        self.app.streams_tree.delete.assert_called_once_with("stale")
        self.app._clear_spectrum.assert_called_once()
        self.app.sweep_context_panel.update.assert_called_once()
        self.app.action_var.reset_mock()
        future.set_result("old result")
        self.app._drain_messages()
        success.assert_not_called()
        self.app.action_var.set.assert_not_called()
        self.assertEqual(self.app._action_callbacks, {})
        self.assertEqual(len(self.root.pending()), 2)
        self.app.close()
        self.assertEqual(self.root.pending(), ())

    def test_reconnect_blocks_stale_stream_and_policy_then_close_drops_completion(self):
        from radioinspector.windows.client import PlatformSnapshot

        old_client = Mock()
        self.app.client = old_client
        self.app.session_id = "old"
        self.app.snapshot = PlatformSnapshot({}, {}, [], session={
            "id": "old", "streams": [{"id": "same-stream"}]})
        self.app.selected_stream_id = Mock(return_value="same-stream")
        future = Future()
        self.app._executor.submit.return_value = future
        success = Mock()
        self.app._submit_action("old action", Mock(), success)
        self.app.server_var = Mock()
        self.app._show_archive_capacity = Mock()
        self.app.client_factory = Mock(return_value=Mock())
        self.app.connect()
        self.app.session_id = "new"
        self.assertIsNone(self.app._selected_stream())
        self.app._require_session = Mock(return_value=True)
        with patch.object(self.app, "_submit_action") as submit:
            self.app._stream_action("start")
            self.app._disable_policy("detection", session_id="old", client=old_client)
            self.app._disable_policy("detection", session_id="new", client=old_client)
            submit.assert_not_called()
        self.app.close()
        future.set_result("old result")
        self.app._drain_messages()
        success.assert_not_called()
        self.assertTrue(self.app._messages.empty())
        self.assertEqual(self.root.pending(), ())

    def test_late_worker_results_after_close_never_touch_tk(self):
        self.app.client = Mock()
        future = Future()
        self.app._executor.submit.return_value = future
        success = Mock()
        self.app._submit_action("test", Mock(), success)
        self.app.close()
        self.app.action_var.reset_mock()
        self.app._render_controls.reset_mock()
        future.set_result("late")
        self.app._drain_messages()
        success.assert_not_called()
        self.app.action_var.set.assert_not_called()
        self.app._render_controls.assert_not_called()
        self.assertEqual(self.root.pending(), ())
        self.assertEqual(self.app._action_callbacks, {})
        self.assertTrue(self.app._messages.empty())

    def test_reconnect_discards_stale_action_success_and_failure(self):
        self.app.server_var = Mock()
        self.app._show_archive_capacity = Mock()
        self.app.client_factory = Mock()
        old_revision = self.app._revision
        self.app.connect()
        for failure in (False, True):
            future = Future()
            if failure:
                future.set_exception(RuntimeError("old failure"))
            else:
                future.set_result("old success")
            callback = Mock()
            self.app._action_running = True
            self.app._action_callbacks[future] = callback
            self.app._messages.put(("action", future, old_revision, "test"))
            self.app._drain_messages()
            callback.assert_not_called()
            self.app.action_var.set.assert_not_called()
            self.assertFalse(self.app._action_running)
            self.assertEqual(self.app._action_callbacks, {})
            self.assertEqual(len(self.root.pending()), 2)

    def test_late_snapshot_result_after_direct_destroy_never_touches_tk(self):
        self.app.client = Mock()
        self.app._spectrum_stream_id = Mock(return_value=None)
        future = Future()
        self.app._executor.submit.return_value = future
        WindowsApp.refresh(self.app)
        self.root.destroy()
        future.set_exception(RuntimeError("late failure"))
        self.app._drain_messages()
        self.app._apply_snapshot.assert_not_called()
        self.app.connection_var.set.assert_not_called()
        self.assertTrue(self.app._messages.empty())

    def test_shutdown_cannot_leave_an_inflight_publication_in_the_queue(self):
        entered, release, closing = (threading.Event() for _ in range(3))
        lock = threading.Lock()
        main_thread = threading.current_thread()

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is main_thread:
                    closing.set()
                lock.acquire()

            def __exit__(self, *_args):
                lock.release()

        class PausingQueue(queue.Queue):
            def put(self, message):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test publication was not released")
                super().put(message)

        self.app._message_lock = ObservedLock()
        self.app._messages = PausingQueue()
        publisher = threading.Thread(target=self.app._queue_message,
                                     args=("snapshot", Future(), 0, ""))

        def unblock_publication():
            closing.wait(2)
            release.set()

        unblocker = threading.Thread(target=unblock_publication)
        publisher.start()
        try:
            self.assertTrue(entered.wait(2))
            unblocker.start()
            self.app.close()
        finally:
            release.set()
            closing.set()
            publisher.join(2)
            if unblocker.ident is not None:
                unblocker.join(2)
        self.assertFalse(publisher.is_alive())
        self.assertFalse(unblocker.is_alive())
        self.assertTrue(self.app._messages.empty())

    def test_shutdown_can_cancel_future_with_synchronous_done_callback(self):
        self.app.client = Mock()
        future = Future()
        self.app._executor.submit.return_value = future
        self.app._submit_action("test", Mock(), Mock())
        self.app._executor.shutdown.side_effect = lambda **_kwargs: future.cancel()
        self.app.close()
        self.assertTrue(future.cancelled())
        self.assertTrue(self.app._messages.empty())

    def test_closed_app_does_not_reconnect_or_submit(self):
        self.app.close()
        self.app.server_var = Mock(side_effect=AssertionError("destroyed variable"))
        self.app.client_factory = Mock()
        self.app.connect()
        WindowsApp.refresh(self.app)
        self.app._poll()
        self.app._submit_action("late", Mock())
        self.app.client_factory.assert_not_called()
        self.app._executor.submit.assert_not_called()
        self.assertEqual(self.root.pending(), ())


class OwnedTimerTests(unittest.TestCase):
    def setUp(self):
        self.root = TclRoot()
        self.addCleanup(self.root.dispose)

    def test_closing_owner_leaves_other_owners_and_unrelated_timers(self):
        first = OwnedTimers(self.root, Mock())
        second = OwnedTimers(self.root, Mock())
        calls = []
        self.root.after(0, lambda: calls.append("unrelated"))
        first.schedule("work", 0, lambda: calls.append("first"))
        second.schedule("work", 0, lambda: calls.append("second"))
        first.close()
        first.schedule("work", 0, lambda: calls.append("closed"))
        self.assertEqual(len(self.root.pending()), 2)
        self.root.pump()
        self.assertEqual(calls, ["unrelated", "second"])
        self.assertEqual(second.pending, {})

    def test_timer_replacement_and_rescheduling_keep_one_id(self):
        owner = OwnedTimers(self.root, Mock())
        calls = []

        def tick():
            calls.append(True)
            self.assertEqual(owner.pending, {})
            owner.schedule("work", 100, tick)

        for _ in range(4):
            owner.schedule("work", 0, tick)
        self.assertEqual(len(self.root.pending()), 1)
        self.root.pump()
        self.assertEqual(calls, [True])
        self.assertEqual(len(self.root.pending()), 1)
        owner.close()
        self.assertEqual(self.root.pending(), ())

    def test_root_destroy_notifies_each_open_owner_once(self):
        first, second = Mock(), Mock()
        one, two = OwnedTimers(self.root, first), OwnedTimers(self.root, second)
        one.schedule("a", 0, Mock())
        two.schedule("b", 0, Mock())
        self.root.destroy()
        first.assert_called_once_with()
        second.assert_called_once_with()
        self.assertEqual(self.root.pending(), ())

    def test_closed_timer_owner_releases_gui_and_root_references(self):
        class Owner:
            def close(self):
                pass

        owner = Owner()
        timers = OwnedTimers(self.root, owner.close)
        reference = weakref.ref(owner)
        timers.close()
        del owner
        self.assertIsNone(reference())
        self.assertIsNone(timers.root)
        self.assertIsNone(timers.on_destroy)
        # The retained Destroy command is inert, but remains valid for Tk.
        self.root.destroy()
        self.assertEqual(self.root.errors, [])

    def test_dispose_releases_native_interpreter_even_if_wrapper_is_retained(self):
        self.root.dispose()
        self.assertIsNone(self.root.interpreter)
        self.assertIsNone(self.root.tk)
        self.assertEqual(self.root.handlers, [])
        self.root.dispose()

    def test_python_callback_error_reaches_python_sink_not_tcl_bgerror(self):
        def fail():
            raise RuntimeError("synthetic callback failure")

        self.root.after(0, fail)
        self.root.pump()
        self.assertEqual(len(self.root.python_errors), 1)
        exception_type, exception, traceback = self.root.python_errors[0]
        self.assertIs(exception_type, RuntimeError)
        self.assertEqual(str(exception), "synthetic callback failure")
        self.assertIsNotNone(traceback)
        self.assertEqual(self.root.errors, [])
        self.assertEqual(self.root.pending(), ())


class CompletionOwnershipTests(unittest.TestCase):
    def test_unresolved_snapshot_and_action_futures_do_not_retain_closed_app(self):
        for kind in ("snapshot", "action"):
            with self.subTest(kind=kind):
                holder = [Future()]
                app = WindowsApp.__new__(WindowsApp)
                app.root = SimpleNamespace(destroy=lambda: None)
                app.client = SimpleNamespace(snapshot=lambda *_args, **_kwargs: None)
                app._closed = app._action_running = app._refresh_running = False
                app._revision = 0
                app._action_callbacks = {}
                app._messages = queue.Queue()
                app._message_lock = threading.Lock()
                app._messages_closed = threading.Event()
                app.session_id = None
                app._spectrum_stream_id = lambda: None
                app.action_var = SimpleNamespace(set=lambda _value: None)
                app._render_controls = lambda: None
                app._executor = SimpleNamespace(submit=lambda *_args, **_kwargs: holder[0],
                                                shutdown=lambda **_kwargs: None)
                released = []
                creator = threading.get_ident()
                reference = weakref.ref(app)
                weakref.finalize(app, lambda: released.append(threading.get_ident()))
                if kind == "snapshot":
                    app.refresh()
                else:
                    app._submit_action("probe", lambda: None)
                messages = app._messages
                app.close()
                del app
                self.assertIsNone(reference(), "An unresolved completion callback retained the GUI")
                self.assertEqual(released, [creator])

                def complete():
                    future = holder.pop()
                    future.set_result("late")

                worker = threading.Thread(target=complete)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(messages.empty())


class PanelTimerLifecycleTests(unittest.TestCase):
    def test_all_panel_close_paths_are_widget_free_and_cancel_owned_timers(self):
        from radioinspector.windows.frequency_rounding import FrequencyRoundingPanel
        from radioinspector.windows.spectral_mask import SpectralMaskPanel
        from radioinspector.windows.sweep_average import AveragePanel
        from radioinspector.windows.sweep_comparison import ComparisonPanel
        from radioinspector.windows.sweep_context import SweepContextPanel
        from radioinspector.windows.sweep_references import NamedReferenceCatalog
        from radioinspector.windows.sweep_three_thresholds import ThreeThresholdsPanel
        from radioinspector.windows.sweep_threshold import ThresholdPanel

        for cls in (ComparisonPanel, ThresholdPanel, AveragePanel, SpectralMaskPanel,
                    SweepContextPanel, ThreeThresholdsPanel, FrequencyRoundingPanel,
                    NamedReferenceCatalog):
            for direct_destroy in (False, True):
                with self.subTest(panel=cls.__name__, direct_destroy=direct_destroy):
                    root = TclRoot()
                    try:
                        panel = cls.__new__(cls)
                        panel.closed = False
                        panel.generation = 0
                        panel.app = SimpleNamespace(root=root, server_var=Mock())
                        panel.executor = Mock()
                        panel.io_executor = Mock()
                        panel.io_guard = Mock()
                        panel.future, panel.retired, panel.io_future = (Future() for _ in range(3))
                        futures = (panel.future, panel.retired, panel.io_future)
                        for future in futures:
                            future.set_running_or_notify_cancel()
                        panel._init_timers(root)
                        panel._schedule_after(root, "analysis", 1, panel.finish, 0)
                        if cls in (FrequencyRoundingPanel, SpectralMaskPanel):
                            panel._schedule_after(root, "io", 1, panel.finish_io, 0, "load")
                        if direct_destroy:
                            root.destroy()
                        else:
                            panel.close()
                            root.destroy()
                        self.assertTrue(panel.closed)
                        self.assertEqual(root.pending(), ())
                        for future in futures:
                            future.set_result("late")
                        panel.finish(0)
                        if cls in (FrequencyRoundingPanel, SpectralMaskPanel):
                            panel.finish_io(0, "load")
                            panel.io_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
                        panel.close()
                        panel.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
                        root.pump(2)
                        self.assertEqual(root.errors, [])
                        self.assertEqual(root.python_errors, [])
                    finally:
                        root.dispose()


@unittest.skipUnless(os.getenv("ORI_WINDOWS_RELEASE_TK") == "1", "Opt-in hidden Tk desktop test")
class HiddenTkTimerLifecycleTests(unittest.TestCase):
    def test_real_scheduler_root_close_and_destroy_cancel_every_owned_timer(self):
        # Keep real Tk graph ownership inside a bounded child. Closed app/panel
        # graphs have older variable-trace cycles unrelated to this timer fix.
        from tests.test_windows_tk_ownership import run_owned_python

        output = run_owned_python(
            "from tests.test_windows_tk_lifecycle import HiddenTkTimerLifecycleTests\n"
            "case = HiddenTkTimerLifecycleTests()\n"
            "case._exercise_real_scheduler_root_close_and_destroy()\n"
            "print('hidden scheduler lifecycle complete', flush=True)\n",
            name="hidden-scheduler-lifecycle", timeout=30,
        )
        self.assertIn("hidden scheduler lifecycle complete", output)

    def _exercise_real_scheduler_root_close_and_destroy(self):
        import tkinter as tk
        from tkinter import ttk
        from radioinspector.windows.scheduler_app import SchedulerWindowsApp

        class NoProfileTk(tk.Tk):
            def readprofile(self, *_args):
                pass

        for direct_destroy in (False, True):
            with self.subTest(direct_destroy=direct_destroy):
                root = NoProfileTk()
                root.withdraw()
                errors = []
                root.report_callback_exception = lambda *error: errors.append(error)
                root.tk.createcommand("bgerror", lambda error: errors.append((error, root.tk.call("set", "::errorInfo"))))

                def unexpected_dialog(*_args):
                    errors.append("unexpected native dialog")
                    raise tk.TclError("native dialogs are forbidden in this test")

                for name in ("tk_getOpenFile", "tk_getSaveFile", "tk_messageBox", "tk_chooseDirectory"):
                    root.tk.createcommand(name, unexpected_dialog)
                app = None
                try:
                    app = SchedulerWindowsApp(root, tk, ttk, refresh_ms=1)
                    app.refresh = Mock()
                    # Tk 8.6.12 queues a ttk theme idle during construction.
                    # Exercise a fully initialized GUI before close; after(1)
                    # application timers remain pending and must be cancelled.
                    root.update_idletasks()
                    self.assertEqual(errors, [])
                    panels = [app.comparison_panel, app.threshold_panel, app.mask_panel,
                              app.average_panel, app.three_thresholds_panel, app.sweep_context_panel,
                              app.frequency_rounding_panel, app.comparison_panel.catalog]
                    executors = []
                    for panel in panels:
                        panel.executor = Mock(wraps=panel.executor)
                        executors.append(panel.executor)
                        panel._schedule_after(root, "analysis", 1, panel.finish, panel.generation)
                        if hasattr(panel, "io_executor"):
                            panel.io_executor = Mock(wraps=panel.io_executor)
                            executors.append(panel.io_executor)
                            panel._schedule_after(root, "io", 1, panel.finish_io, panel.generation, "load")
                    for _ in range(3):
                        app._drain_messages()
                        app._poll()
                    # Child Destroy events bubble to the root binding too.
                    child = ttk.Frame(root)
                    child.destroy()
                    self.assertFalse(app._closed)
                    if direct_destroy:
                        root.destroy()
                    else:
                        app.close()
                    self.assertTrue(app._closed)
                    self.assertTrue(all(panel.closed for panel in panels))
                    self.assertTrue(all(not panel._tk_timers.pending for panel in panels))
                    self.assertEqual(app._tk_timers.pending, {})
                    for executor in executors:
                        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
                    # Tcl remains usable after the windows disappear. Drain old
                    # events with our non-modal bgerror sink still registered.
                    root.tk.call("after", 110)
                    root.tk.call("update")
                    self.assertEqual(errors, [])
                    app.close()
                finally:
                    if app is not None:
                        app.close()
                    else:
                        root.destroy()
                    for name in ("bgerror", "tk_getOpenFile", "tk_getSaveFile", "tk_messageBox", "tk_chooseDirectory"):
                        root.tk.deletecommand(name)


if __name__ == "__main__":
    unittest.main()
