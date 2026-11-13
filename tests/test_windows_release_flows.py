"""Owned, hidden Tk windows exercise production callbacks without native dialogs.

Run with ORI_WINDOWS_RELEASE_TK=1 on a desktop with Tk. All API responses are
synthetic; no device ports, real archives or user-selected files are accessed.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from radioinspector.windows.scheduler_app import SchedulerWindowsApp
from radioinspector.windows.client import PlatformClient


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, operation, *args, **kwargs):
        future = Future()
        self.jobs.append((future, lambda: operation(*args, **kwargs)))
        return future

    def finish(self):
        future, operation = self.jobs.pop(0)
        try:
            future.set_result(operation())
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, **_kwargs):
        pass


@unittest.skipUnless(os.getenv("ORI_WINDOWS_RELEASE_TK") == "1", "Opt-in hidden Tk desktop test")
class WindowsReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.app = SchedulerWindowsApp(self.root, tk, ttk, refresh_ms=60000)
        self.app._executor.shutdown(wait=True)
        self.executor = DeferredExecutor()
        self.app._executor = self.executor
        self.app.refresh = Mock()
        self.app.client = Mock()
        self.app.client.create_task.side_effect = lambda payload: {
            **payload, "status": "В очереди на выполнение"
        }
        self.app.client.export_archive.return_value = {
            "sweeps": 2, "observations": 1, "sha256": "synthetic-sha"
        }
        self.app.session_id = "session/original"
        self.app._render_controls()

    def tearDown(self):
        self.app.close()
        self.assertEqual(self.callback_errors, [])

    def open_form(self, **changes):
        self.app.task_create_button.invoke()
        window = self.app._active_form
        window.withdraw()
        frame = window.winfo_children()[0]
        entries = {
            key: frame.grid_slaves(row=row, column=1)[0]
            for row, key in enumerate(("name", "task_file", "start_at", "duration_s", "equipment"))
        }
        values = {"name": "Synthetic task", "task_file": "/synthetic/not-read.json",
                  "start_at": "2026-11-07T14:00:00+02:00", **changes}
        for key, value in values.items():
            entries[key].delete(0, "end")
            entries[key].insert(0, value)
        buttons = {child.cget("text"): child for child in frame.winfo_children()
                   if child.winfo_class() == "TButton"}
        return window, entries, buttons

    def finish_action(self):
        self.executor.finish()
        self.app._drain_messages()

    def open_serial_form(self):
        self.app._open_session_form()
        window = self.app._active_form
        window.withdraw()
        frame = window.winfo_children()[0]
        for row, text in ((1, 'COM12'), (2, 'COM13')):
            entry = frame.grid_slaves(row=row, column=1)[0]
            entry.delete(0, 'end')
            entry.insert(0, text)
        serial = frame.grid_slaves(row=8, column=0)[0]
        serial.invoke()
        simulated = frame.grid_slaves(row=7, column=0)[0]
        self.assertFalse(simulated.instate(['selected']))
        self.assertTrue(serial.instate(['selected']))
        buttons = frame.grid_slaves(row=10, column=0)[0]
        create = next(child for child in buttons.winfo_children() if child.cget('text') == 'Создать сеанс')
        return window, create

    def test_serial_form_selects_com_profile_without_starting_workers(self):
        window, create = self.open_serial_form()
        self.app.client.create_session.return_value = {'id': 'serial-created', 'state': 'draft'}
        create.invoke()
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()
        payload = self.app.client.create_session.call_args.args[0]
        self.assertEqual([s['settings']['adapter'] for s in payload['streams']],
                         ['tinysa-serial', 'ardv1-serial'])
        self.app.client.start_session.assert_not_called()
        self.app.client.start_workers.assert_not_called()

    def test_serial_form_cannot_submit_to_replaced_connection_or_context(self):
        for change in (lambda: setattr(self.app, 'client', Mock()),
                       lambda: setattr(self.app, '_revision', self.app._revision + 1)):
            window, create = self.open_serial_form()
            change()
            create.invoke()
            self.assertEqual(self.executor.jobs, [])
            self.assertTrue(window.winfo_exists())

    def test_serial_form_cancel_and_repeated_create_while_pending(self):
        window, _ = self.open_serial_form()
        self.app._destroy_form(window)
        self.assertEqual(self.executor.jobs, [])
        _, create = self.open_serial_form()
        self.app.client.create_session.return_value = {'id': 'serial-created', 'state': 'draft'}
        create.invoke()
        second, create_again = self.open_serial_form()
        create_again.invoke()
        self.assertTrue(second.winfo_exists())
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()
        self.app.client.create_session.assert_called_once()

    def test_serial_creation_reply_does_not_replace_new_context(self):
        _, create = self.open_serial_form()
        self.app.client.create_session.return_value = {'id': 'serial-created', 'state': 'draft'}
        create.invoke()
        self.app._revision += 1
        self.app.session_id = 'new-context'
        self.finish_action()
        self.assertEqual(self.app.session_id, 'new-context')
        self.assertFalse(self.app._action_running)

    def test_dismissed_serial_form_ignores_late_submit_callback(self):
        callbacks = []
        original = self.app._form_buttons

        def capture(*args):
            callbacks.append(args[-1])
            original(*args)

        with patch.object(self.app, '_form_buttons', side_effect=capture):
            window, _ = self.open_serial_form()
        self.app._destroy_form(window)
        callbacks[0]()
        self.assertEqual(self.executor.jobs, [])

    def test_serial_creation_errors_do_not_crash_or_retry(self):
        from radioinspector.windows.client import PlatformClientError

        for reply in (None, {}, {'id': 'unexpected', 'state': 'running'}):
            _, create = self.open_serial_form()
            self.app.client.create_session.return_value = reply
            create.invoke()
            self.finish_action()
            self.assertIn('Некорректный ответ', self.app.action_var.get())
            self.assertFalse(self.app._action_running)
            self.assertEqual(self.executor.jobs, [])
        _, create = self.open_serial_form()
        self.app.client.create_session.side_effect = PlatformClientError('Synthetic HTTP 400')
        create.invoke()
        self.finish_action()
        self.assertIn('Synthetic HTTP 400', self.app.action_var.get())
        self.assertEqual(self.app.session_id, 'session/original')

    def test_actual_default_string_duration_submits_once_and_displays_result(self):
        window, entries, buttons = self.open_form()
        self.assertEqual(entries["duration_s"].get(), "60")
        buttons["Создать задачу"].invoke()
        self.assertEqual(len(self.executor.jobs), 1, self.app.task_create_var.get())
        self.assertFalse(window.winfo_exists())
        self.app.task_create_button.invoke()
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()
        payload = self.app.client.create_task.call_args.args[0]
        self.assertEqual(payload["duration_s"], 60.0)
        self.assertIs(type(payload["duration_s"]), float)
        self.assertEqual(payload["start_at"], "2026-11-07T12:00:00Z")
        self.assertIn("В очереди на выполнение", self.app.task_create_var.get())

    def test_invalid_duration_keeps_form_open_then_accepts_correction(self):
        window, entries, buttons = self.open_form()
        for value in ("", " ", "bad", "true", "0", "-0", "-1", "nan", "NaN", "inf", "-Infinity", "1e309", "1,5"):
            with self.subTest(value=value):
                entries["duration_s"].delete(0, "end")
                entries["duration_s"].insert(0, value)
                buttons["Создать задачу"].invoke()
                self.assertTrue(window.winfo_exists())
                self.assertIn("duration_s", self.app.task_create_var.get())
                self.assertEqual(self.executor.jobs, [])
        entries["duration_s"].delete(0, "end")
        entries["duration_s"].insert(0, " 0.25 ")
        buttons["Создать задачу"].invoke()
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()
        self.assertEqual(self.app.client.create_task.call_args.args[0]["duration_s"], 0.25)

    def test_reopen_replaces_form_and_close_does_not_submit(self):
        first, _, _ = self.open_form()
        second, _, buttons = self.open_form()
        self.assertFalse(first.winfo_exists())
        buttons["Закрыть"].invoke()
        self.assertFalse(second.winfo_exists())
        self.assertIsNone(self.app._active_form)
        self.assertEqual(self.executor.jobs, [])

    def test_task_form_cannot_submit_to_replaced_connection(self):
        window, _, buttons = self.open_form()
        self.app.client = Mock()
        self.app._revision += 1
        buttons["Создать задачу"].invoke()
        self.assertEqual(self.executor.jobs, [])
        self.assertTrue(window.winfo_exists())

    def test_dismissed_task_form_ignores_late_submit_callback(self):
        window, entries, buttons = self.open_form()
        values = {key: self.tk.StringVar(master=self.root, name=entry.cget("textvariable"))
                  for key, entry in entries.items()}
        buttons["Закрыть"].invoke()
        self.app._create_task(window, values)
        self.assertEqual(self.executor.jobs, [])

    def test_failed_create_reenables_form_for_explicit_retry(self):
        from radioinspector.windows.client import PlatformClientError

        self.app.client.create_task.side_effect = PlatformClientError("Synthetic rejection")
        _, _, buttons = self.open_form()
        buttons["Создать задачу"].invoke()
        self.finish_action()
        self.assertIn("Synthetic rejection", self.app.action_var.get())
        self.assertFalse(self.app._action_running)
        window, _, _ = self.open_form()
        self.assertTrue(window.winfo_exists())

    def test_late_created_task_does_not_replace_status_after_navigation(self):
        _, _, buttons = self.open_form()
        buttons["Создать задачу"].invoke()
        self.assertEqual(len(self.executor.jobs), 1)
        self.app._revision += 1
        self.app.task_create_var.set("New context")
        self.finish_action()
        self.assertEqual(self.app.task_create_var.get(), "New context")
        self.assertFalse(self.app._action_running)

    def test_export_imports_dialog_instead_of_relying_on_tk_attribute(self):
        from tkinter import filedialog

        # Importing the test mock must not accidentally repair the production bug.
        del self.tk.filedialog
        self.addCleanup(setattr, self.tk, "filedialog", filedialog)
        with patch.object(filedialog, "asksaveasfilename", return_value="/synthetic/export.ndjson") as dialog:
            self.app.archive_export_button.invoke()
        self.assertEqual(self.callback_errors, [])
        dialog.assert_called_once()
        self.assertIs(dialog.call_args.kwargs["parent"], self.root)
        self.assertEqual(dialog.call_args.kwargs["initialfile"], "archive-session_original.ndjson")
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()
        self.app.client.export_archive.assert_called_once_with("session/original", "/synthetic/export.ndjson")
        self.assertIn("synthetic-sha", self.app.archive_export_var.get())

    def test_export_cancel_and_repeated_clicks(self):
        from tkinter import filedialog

        initial = self.app.archive_export_var.get()
        with patch.object(filedialog, "asksaveasfilename", return_value="") as dialog:
            self.app.archive_export_button.invoke()
            self.app.archive_export_button.invoke()
        self.assertEqual(dialog.call_count, 2)
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual(self.app.archive_export_var.get(), initial)
        with patch.object(filedialog, "asksaveasfilename", return_value="/synthetic/export.ndjson") as dialog:
            self.app.archive_export_button.invoke()
            self.app.export_archive()
            dialog.assert_called_once()
        self.assertEqual(len(self.executor.jobs), 1)
        self.finish_action()

    def test_export_does_not_reenter_dialog(self):
        from tkinter import filedialog

        def choose(**_kwargs):
            self.app.export_archive()
            return "/synthetic/export.ndjson"

        with patch.object(filedialog, "asksaveasfilename", side_effect=choose) as dialog:
            self.app.archive_export_button.invoke()
            self.assertEqual(dialog.call_count, 1)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_export_drops_selection_when_context_changes_inside_dialog(self):
        from tkinter import filedialog

        for change in (lambda: setattr(self.app, "session_id", "session/new"),
                       lambda: setattr(self.app, "client", Mock()),
                       lambda: setattr(self.app, "_revision", self.app._revision + 1),
                       lambda: setattr(self.app, "_action_running", True)):
            with self.subTest(change=change):
                self.app._action_running = False

                def choose(**_kwargs):
                    change()
                    return "/synthetic/export.ndjson"

                with patch.object(filedialog, "asksaveasfilename", side_effect=choose):
                    self.app.export_archive()
                self.assertEqual(self.executor.jobs, [])

    def test_export_closed_inside_dialog_never_submits(self):
        from tkinter import filedialog

        def choose(**_kwargs):
            self.app.close()
            return "/synthetic/export.ndjson"

        with patch.object(filedialog, "asksaveasfilename", side_effect=choose):
            self.app.export_archive()
        self.assertEqual(self.executor.jobs, [])

    def test_late_export_result_is_ignored_after_session_change(self):
        from tkinter import filedialog

        client = self.app.client
        with patch.object(filedialog, "asksaveasfilename", return_value="/synthetic/export.ndjson"):
            self.app.archive_export_button.invoke()
        self.app.session_id = "session/new"
        self.app._revision += 1
        self.app.archive_export_var.set("New context")
        self.finish_action()
        client.export_archive.assert_called_once_with("session/original", "/synthetic/export.ndjson")
        self.assertEqual(self.app.archive_export_var.get(), "New context")

    def test_late_export_result_is_ignored_after_close(self):
        from tkinter import filedialog

        with patch.object(filedialog, "asksaveasfilename", return_value="/synthetic/export.ndjson"):
            self.app.archive_export_button.invoke()
        self.app.close()
        self.finish_action()
        self.assertEqual(self.app._action_callbacks, {})

    def test_dialog_error_releases_reentry_guard(self):
        from tkinter import filedialog

        with patch.object(filedialog, "asksaveasfilename", side_effect=self.tk.TclError("dialog failed")):
            self.app.archive_export_button.invoke()
        self.assertIn("dialog failed", self.app.archive_export_var.get())
        self.assertFalse(self.app._archive_export_dialog_open)
        self.assertEqual(self.app.archive_export_button.cget("state"), "normal")
        with patch.object(filedialog, "asksaveasfilename", return_value="") as dialog:
            self.app.export_archive()
            dialog.assert_called_once()

    def test_closed_dialog_error_does_not_touch_destroyed_widgets(self):
        from tkinter import filedialog

        def choose(**_kwargs):
            self.app.close()
            raise self.tk.TclError("application has been destroyed")

        with patch.object(filedialog, "asksaveasfilename", side_effect=choose):
            self.app.export_archive()
        self.assertFalse(self.app._archive_export_dialog_open)
        self.assertEqual(self.executor.jobs, [])

    def test_actual_form_and_export_with_synthetic_http_and_temporary_files(self):
        from tkinter import filedialog
        from radioinspector.session_service import SessionService
        from radioinspector.session_store import SQLiteSessionStore
        from radioinspector.task_scheduler import TaskScheduler
        from radioinspector.tasks import TaskBook
        from radioinspector.web.server import create_server
        from tests.test_persistence import observation_payload, sweep_payload
        from tests.test_platform import session_payload

        with tempfile.TemporaryDirectory() as temporary:
            service = SessionService(store=SQLiteSessionStore(str(Path(temporary) / "archive.sqlite")))
            scheduler = TaskScheduler(TaskBook(Path(temporary) / "tasks.json"), interval_seconds=60)
            server = create_server(host="127.0.0.1", port=0, sessions=service, task_scheduler=scheduler)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                self.app.client = PlatformClient(f"http://{host}:{port}")
                _, _, buttons = self.open_form(task_file=str(Path(temporary) / "not-read.json"))
                buttons["Создать задачу"].invoke()
                self.assertEqual(len(self.executor.jobs), 1)
                self.finish_action()
                tasks = self.app.client.task_scheduler_tasks()
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["duration_s"], 60.0)
                self.assertNotIn("task_file", tasks[0])
                self.assertIn("Synthetic task", self.app.task_create_var.get())

                session = service.create_session(session_payload())
                spectrum_id, receiver_id = (stream.id for stream in session.streams)
                service.ingest_sweep(sweep_payload(session.id, spectrum_id))
                service.ingest_observation(observation_payload(session.id, receiver_id))
                self.app.session_id = session.id
                before = service.health()
                destination = Path(temporary) / "output.ndjson"
                self.app._render_controls()
                with patch.object(filedialog, "asksaveasfilename", return_value=str(destination)):
                    self.app.archive_export_button.invoke()
                self.assertEqual(len(self.executor.jobs), 1)
                self.finish_action()
                exported = destination.read_text(encoding="utf-8")
                self.assertIn('"level_unit":"dBm"', exported)
                self.assertIn('"level_unit":"s_meter_count"', exported)
                self.assertIn("NDJSON сохранён: 1 sweeps, 1 observations", self.app.archive_export_var.get())
                self.assertEqual(service.health(), before)
            finally:
                server.automatic_detection.close()
                server.automatic_handoff.close()
                server.automatic_completion.close()
                server.control_scheduler.close()
                server.worker_manager.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
