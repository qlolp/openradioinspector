"""Планировщик заданий, протокол DOCX и веб-маршруты оператора."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from radioinspector.cli import main
from radioinspector.devices.registry import open_instrument, profile_from_idn
from radioinspector.devices.scpi import ScpiInstrument
from radioinspector.panorama import PanoramaStore, ScanSettings, generate_synthetic_trace
from radioinspector.reporting import docx_bytes, render_protocol, write_docx
from radioinspector.scheduler import TaskScheduler, advance_tasks
from radioinspector.tasks import (
    OUTCOME_APPLICATION_CRASH,
    OUTCOME_FILE_NOT_FOUND,
    OUTCOME_STOPPED_BY_TASK,
    OUTCOME_WINDOW_MISSED,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    ControlTask,
    TaskBook,
)
from radioinspector.web.server import ApplicationState, Handler
from http.server import ThreadingHTTPServer


def _book(root: Path, *tasks: ControlTask) -> TaskBook:
    book = TaskBook(root / "tasks.json")
    for task in tasks:
        book.add(task)
    return book


class TestScheduler(unittest.TestCase):
    def test_due_task_with_file_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            task = ControlTask(
                name="РКП-1",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                duration_s=3600,
            ).enqueue()
            book = _book(root, task)
            changes = advance_tasks(book)
        self.assertEqual(task.status, STATUS_RUNNING)
        self.assertEqual(changes[-1].action, "запущено")

    def test_missing_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = ControlTask(
                name="РКП-2",
                task_file="missing.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                duration_s=600,
            ).enqueue()
            advance_tasks(_book(root, task))
        self.assertEqual(task.status, STATUS_ABORTED)
        self.assertEqual(task.outcome, OUTCOME_FILE_NOT_FOUND)
        self.assertEqual(task.outcome_kind(), "ошибка")

    def test_closed_window_does_not_count_as_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            running = ControlTask(
                name="Идёт",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc),
                duration_s=3600,
            )
            running.status = STATUS_RUNNING
            missed = ControlTask(
                name="Пропуск",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(hours=3),
                duration_s=60,
            ).enqueue()
            advance_tasks(_book(root, running, missed))
        self.assertEqual(running.status, STATUS_RUNNING)
        self.assertEqual(missed.status, STATUS_ABORTED)
        self.assertEqual(missed.outcome, OUTCOME_WINDOW_MISSED)

    def test_running_task_completes_when_window_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = ControlTask(
                name="РКП-3",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(hours=2),
                duration_s=60,
            )
            task.status = STATUS_RUNNING
            book = _book(root, task)
            scheduler = TaskScheduler(book)
            changes = scheduler.tick()
            saved = json.loads(book.path.read_text(encoding="utf-8"))
        self.assertEqual(task.status, STATUS_COMPLETED)
        self.assertEqual(changes[0].action, "выполнено")
        self.assertEqual(saved["tasks"][0]["status"], STATUS_COMPLETED)

    def test_later_task_preempts_earlier_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            now = datetime.now(timezone.utc)
            early = ControlTask(
                name="Раннее",
                task_file="list.itskfcs",
                start_at=now - timedelta(minutes=30),
                duration_s=7200,
            ).enqueue()
            late = ControlTask(
                name="Позднее",
                task_file="list.itskfcs",
                start_at=now - timedelta(minutes=5),
                duration_s=7200,
            ).enqueue()
            advance_tasks(_book(root, early, late))
        self.assertEqual(early.status, STATUS_ABORTED)
        self.assertEqual(early.outcome, OUTCOME_STOPPED_BY_TASK)
        self.assertEqual(late.status, STATUS_RUNNING)

    def test_runner_failure_marks_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            task = ControlTask(
                name="Сбой",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                duration_s=3600,
            ).enqueue()

            def boom(_task: ControlTask) -> None:
                raise RuntimeError("прибор не ответил")

            changes = advance_tasks(_book(root, task), runner=boom)
        self.assertEqual(task.status, STATUS_ABORTED)
        self.assertEqual(task.outcome, OUTCOME_APPLICATION_CRASH)
        self.assertIn("прибор не ответил", changes[-1].note)

    def test_runner_counts_a_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            task = ControlTask(
                name="Цикл",
                task_file="list.itskfcs",
                start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                duration_s=3600,
            ).enqueue()
            advance_tasks(_book(root, task), runner=lambda _task: None)
        self.assertEqual(task.completed_cycles, 1)
        self.assertEqual(task.status, STATUS_RUNNING)

    def test_is_due_accepts_naive_clock(self) -> None:
        task = ControlTask(
            name="Часы",
            task_file="list.itskfcs",
            start_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            duration_s=60,
        ).enqueue()
        self.assertTrue(task.is_due(datetime(2020, 1, 2)))
        self.assertEqual(task.status, STATUS_QUEUED)

    def test_cli_add_and_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "list.itskfcs"
            source.write_text("100", encoding="utf-8")
            database = root / "tasks.json"
            start = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            code = main(
                [
                    "tasks",
                    "--database",
                    str(database),
                    "--add",
                    "Смена",
                    "--file",
                    str(source),
                    "--start",
                    start,
                    "--duration",
                    "3600",
                    "--tick",
                ]
            )
            book = TaskBook(database).load()
        self.assertEqual(code, 0)
        self.assertEqual(book.tasks[0].status, STATUS_RUNNING)


class TestDocxProtocol(unittest.TestCase):
    def test_docx_keeps_heading_and_escapes_text(self) -> None:
        text = "# ПРОТОКОЛ\nОператор: A&B <цех>"
        payload = docx_bytes(text, title="Протокол &")
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            names = set(archive.namelist())
            document = archive.read("word/document.xml").decode("utf-8")
            core = archive.read("docProps/core.xml").decode("utf-8")
        self.assertIn("[Content_Types].xml", names)
        self.assertIn("word/document.xml", names)
        self.assertIn("<w:b/>", document)
        self.assertIn("ПРОТОКОЛ", document)
        self.assertIn("A&amp;B &lt;цех&gt;", document)
        self.assertIn("Протокол &amp;", core)
        self.assertNotIn("A&B", document)

    def test_protocol_mentions_control_bandwidth(self) -> None:
        settings = ScanSettings(start_hz=100e6, stop_hz=100.4e6, step_hz=100e3, points=5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "base.pan"
            with PanoramaStore(path, settings) as store:
                store.append(generate_synthetic_trace(settings, seed=1))
                text = render_protocol(store, operator="Иванов", place="полигон")
                output = write_docx(Path(tmp) / "protocol.docx", text)
            self.assertTrue(output.is_file())
        self.assertIn("Иванов", text)
        self.assertIn("Контрольная ширина полосы", text)


class _FakeScpi:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def open(self) -> None:
        return None

    def query(self, command: str) -> str:
        self.queries.append(command)
        return "Keysight Technologies,N9020A,MY123,A.01.02\n"

    def close(self) -> None:
        return None


class TestInstrumentProfile(unittest.TestCase):
    def test_query_does_not_stick_to_port(self) -> None:
        instrument = open_instrument("scpi://192.168.1.20:5025?instrument=mx")
        self.assertEqual(instrument.host, "192.168.1.20")
        self.assertEqual(instrument.port, 5025)
        self.assertEqual(instrument.profile, "mx")

    def test_serial_baud_ignores_query(self) -> None:
        instrument = open_instrument("serial://COM3:115200?instrument=ifr")
        self.assertEqual(instrument.port, "COM3")
        self.assertEqual(instrument.baudrate, 115200)

    def test_idn_selects_profile_when_url_has_none(self) -> None:
        instrument = ScpiInstrument("10.0.0.8", connection=_FakeScpi())
        info = instrument.connect()
        self.assertEqual(info.model, "N9020A")
        self.assertEqual(instrument.profile, "keysight-mx")
        self.assertEqual(profile_from_idn("Rohde&Schwarz,FSQ-3,100,1.0"), "rohde-schwarz")
        self.assertEqual(profile_from_idn("unknown"), "")


class TestWebOperatorRoutes(unittest.TestCase):
    def test_report_tasks_and_coverage(self) -> None:
        settings = ScanSettings(start_hz=100e6, stop_hz=100.4e6, step_hz=100e3, points=5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "base.pan"
            with PanoramaStore(database, settings) as store:
                trace = generate_synthetic_trace(settings, seed=2)
                store.append(
                    trace,
                    latitude=55.75,
                    longitude=37.62,
                    recorded_at=datetime.now(timezone.utc),
                )
                store.append(
                    trace,
                    latitude=55.76,
                    longitude=37.64,
                    recorded_at=datetime.now(timezone.utc),
                )
            (root / "list.itskfcs").write_text("100", encoding="utf-8")
            tasks = root / "tasks.json"
            book = TaskBook(tasks)
            book.add(
                ControlTask(
                    name="Веб",
                    task_file=str(root / "list.itskfcs"),
                    start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                    duration_s=3600,
                ).enqueue()
            )
            book.save()
            state = ApplicationState(database=str(database), tasks=str(tasks))
            handler = type("BoundHandler", (Handler,), {"state": state})
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            try:
                operator = quote("Иванов")
                with urlopen(
                    f"http://127.0.0.1:{port}/api/report?format=md&operator={operator}"
                ) as response:
                    protocol = response.read().decode("utf-8")
                self.assertIn("Иванов", protocol)
                with urlopen(f"http://127.0.0.1:{port}/api/report?format=docx") as response:
                    payload = response.read()
                self.assertTrue(payload.startswith(b"PK"))
                with urlopen(f"http://127.0.0.1:{port}/api/coverage?rows=4&cols=4") as response:
                    coverage = json.loads(response.read().decode("utf-8"))
                self.assertEqual(coverage["type"], "FeatureCollection")
                self.assertGreater(len(coverage["features"]), 0)
                request = Request(
                    f"http://127.0.0.1:{port}/api/tasks/tick",
                    data=b"{}",
                    method="POST",
                )
                with urlopen(request) as response:
                    ticked = json.loads(response.read().decode("utf-8"))
                self.assertTrue(ticked["configured"])
                self.assertEqual(ticked["tasks"][0]["status"], STATUS_RUNNING)
                with urlopen(f"http://127.0.0.1:{port}/") as response:
                    page = response.read().decode("utf-8")
                self.assertIn("Протокол DOCX", page)
                self.assertIn("Плановые задания", page)
                self.assertIn("m-norm", page)
            finally:
                httpd.shutdown()
                thread.join(timeout=3)
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
