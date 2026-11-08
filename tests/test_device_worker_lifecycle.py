"""Детерминированные гонки lifecycle; только адаптеры-симуляторы, без портов."""

from __future__ import annotations

import threading
import unittest
from typing import Any, Callable
from unittest.mock import patch

from radioinspector.device_workers import (
    DeviceWorker, DeviceWorkerManager, TinySASimulator, WorkerError,
    create_simulated_adapter,
)
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService()
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.manager = DeviceWorkerManager(self.service)
        self.stream_id = self.session.streams[0].id
        self.calls: list[threading.Thread] = []
        self.releases: list[threading.Event] = []
        self.workers: list[DeviceWorker] = []

    def tearDown(self) -> None:
        for release in self.releases:
            release.set()
        for thread in self.calls:
            thread.join(3)
        # Track actual workers too: a broken registry must not leak test threads.
        for worker in self.workers:
            worker.stop(timeout=2)
        self.manager.close()
        self.service.close()
        self.assertTrue(all(not thread.is_alive() for thread in self.calls))
        self.assertTrue(all(not worker.alive for worker in self.workers))

    def gate(self) -> tuple[threading.Event, threading.Event]:
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        return entered, release

    def launch(self, action: Callable[[], Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"done": threading.Event(), "entered": threading.Event()}

        def run() -> None:
            result["entered"].set()
            try:
                result["value"] = action()
            except Exception as exc:
                result["error"] = exc
            finally:
                result["done"].set()

        thread = threading.Thread(target=run, daemon=True)
        self.calls.append(thread)
        thread.start()
        self.assertTrue(result["entered"].wait(2))
        return result

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        self.assertTrue(result["done"].wait(3), "Lifecycle operation did not finish")
        return result

    def start(self) -> dict[str, Any]:
        return self.manager.start_stream(self.session.id, self.stream_id)

    def live_threads(self) -> list[threading.Thread]:
        return [
            thread for thread in threading.enumerate()
            if thread.name == f"device-worker-{self.stream_id}"
        ]

    def delayed_start(self, entered: threading.Event, release: threading.Event):
        original_start = DeviceWorker.start

        def start(worker: DeviceWorker) -> None:
            self.workers.append(worker)
            if len(self.workers) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release startup")
            original_start(worker)

        return start

    def test_simultaneous_starts_have_exactly_one_owner(self) -> None:
        entered, release = self.gate()
        with patch.object(DeviceWorker, "start", self.delayed_start(entered, release)):
            first = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            second = self.launch(self.start)
            # The broken manager finishes a second start while the first is reserved.
            second["done"].wait(0.1)
            release.set()
            self.finish(first)
            self.finish(second)
        self.assertNotIn("error", first)
        self.assertIsInstance(second.get("error"), WorkerError)
        self.assertIn("уже запущен", str(second["error"]))
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(len(self.live_threads()), 1)
        self.assertEqual(self.manager.active_count, 1)
        self.manager.stop_stream(self.session.id, self.stream_id)
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)

    def test_stop_during_reserved_start_leaves_no_executor(self) -> None:
        entered, release = self.gate()
        with patch.object(DeviceWorker, "start", self.delayed_start(entered, release)):
            started = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            stopped = self.launch(
                lambda: self.manager.stop_stream(self.session.id, self.stream_id)
            )
            stopped["done"].wait(0.1)
            release.set()
            self.finish(started)
            self.finish(stopped)
        self.assertNotIn("error", stopped)
        self.assertFalse(stopped["value"]["alive"])
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)

    def test_close_during_reserved_start_prevents_late_executor(self) -> None:
        entered, release = self.gate()
        with patch.object(DeviceWorker, "start", self.delayed_start(entered, release)):
            started = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            closed = self.launch(self.manager.close)
            closed["done"].wait(0.1)
            release.set()
            self.finish(started)
            self.finish(closed)
        self.assertNotIn("error", closed)
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)
        with self.assertRaisesRegex(WorkerError, "закрыт"):
            self.start()

    def test_failed_thread_start_closes_adapter_and_allows_retry(self) -> None:
        adapter = TinySASimulator(self.session.streams[0])
        with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
            with patch.object(adapter, "close", wraps=adapter.close) as close:
                with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
                    with self.assertRaisesRegex(RuntimeError, "start failed"):
                        self.start()
                close.assert_called_once_with()
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(self.start()["alive"])

    def test_waiting_start_is_cancelled_by_stop_timeout_too(self) -> None:
        entered, release = self.gate()
        waiting = threading.Event()
        lifecycle = self.manager._lifecycle((self.session.id, self.stream_id))
        lock = lifecycle.lock

        class ObservedLock:
            def acquire(self, *args, **kwargs):
                if lock.locked():
                    waiting.set()
                return lock.acquire(*args, **kwargs)

            def release(self):
                lock.release()

            def __enter__(self):
                self.acquire()

            def __exit__(self, *args):
                self.release()

        lifecycle.lock = ObservedLock()
        with patch.object(DeviceWorker, "start", self.delayed_start(entered, release)):
            first = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            second = self.launch(self.start)
            self.assertTrue(waiting.wait(2))
            with self.assertRaisesRegex(WorkerError, "не остановился"):
                self.manager.stop_stream(self.session.id, self.stream_id, timeout=0.01)
            release.set()
            self.finish(first)
            self.finish(second)
        self.assertIsInstance(first.get("error"), WorkerError)
        self.assertIsInstance(second.get("error"), WorkerError)
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.live_threads(), [])
        self.assertTrue(self.start()["alive"])

    def test_stop_timeout_during_connect_cancels_start_and_closes_adapter(self) -> None:
        entered, release = self.gate()
        adapter = TinySASimulator(self.session.streams[0])

        def connect(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release connect")
            return adapter

        with patch.object(adapter, "close", wraps=adapter.close) as close:
            with patch("radioinspector.serial_instruments.create_device_adapter", side_effect=connect):
                started = self.launch(self.start)
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(WorkerError, "не остановился"):
                    self.manager.stop_stream(self.session.id, self.stream_id, timeout=0.01)
                release.set()
                self.finish(started)
            close.assert_called_once_with()
        self.assertIsInstance(started.get("error"), WorkerError)
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(self.start()["alive"])

    def test_close_during_connect_never_launches_a_worker(self) -> None:
        entered, release = self.gate()
        closing = threading.Event()
        adapter = TinySASimulator(self.session.streams[0])
        original_stop = self.manager._stop_worker

        def connect(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release connect")
            return adapter

        def stop(*args, **kwargs):
            closing.set()
            return original_stop(*args, **kwargs)

        with patch.object(adapter, "close", wraps=adapter.close) as close:
            with patch("radioinspector.serial_instruments.create_device_adapter", side_effect=connect):
                with patch.object(DeviceWorker, "start") as worker_start:
                    with patch.object(self.manager, "_stop_worker", side_effect=stop):
                        started = self.launch(self.start)
                        self.assertTrue(entered.wait(2))
                        closed = self.launch(self.manager.close)
                        self.assertTrue(closing.wait(2))
                        release.set()
                        self.finish(started)
                        self.finish(closed)
                    worker_start.assert_not_called()
            close.assert_called_once_with()
        self.assertIsInstance(started.get("error"), WorkerError)
        self.assertNotIn("error", closed)
        self.assertEqual(self.manager.active_count, 0)

    def test_failed_connect_releases_reservation_for_waiting_retry(self) -> None:
        entered, release = self.gate()
        calls = []

        def connect(stream, **kwargs):
            calls.append(stream.id)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release connect")
                raise WorkerError("connect failed")
            return create_simulated_adapter(stream, **kwargs)

        with patch("radioinspector.serial_instruments.create_device_adapter", side_effect=connect):
            first = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            second = self.launch(self.start)
            release.set()
            self.finish(first)
            self.finish(second)
        self.assertRegex(str(first.get("error")), "connect failed")
        self.assertNotIn("error", second)
        self.assertEqual(len(self.live_threads()), 1)
        self.assertEqual(self.manager.active_count, 1)

    def test_failed_start_does_not_remove_a_waiting_replacement(self) -> None:
        entered, release = self.gate()
        original_start = DeviceWorker.start

        def start(worker):
            self.workers.append(worker)
            if len(self.workers) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release startup")
                raise RuntimeError("start failed")
            original_start(worker)

        with patch.object(DeviceWorker, "start", start):
            first = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            with patch.object(self.workers[0].adapter, "close") as close:
                second = self.launch(self.start)
                release.set()
                self.finish(first)
                self.finish(second)
                close.assert_called_once_with()
        self.assertRegex(str(first.get("error")), "start failed")
        self.assertNotIn("error", second)
        self.assertIs(self.manager._workers[(self.session.id, self.stream_id)], self.workers[1])
        self.assertEqual(self.manager.active_count, 1)

    def test_connect_does_not_block_another_stream_or_registry_reads(self) -> None:
        entered, release = self.gate()
        receiver_id = self.session.streams[1].id

        def connect(stream, **kwargs):
            if stream.id == self.stream_id:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release connect")
            return create_simulated_adapter(stream, **kwargs)

        with patch("radioinspector.serial_instruments.create_device_adapter", side_effect=connect):
            first = self.launch(self.start)
            self.assertTrue(entered.wait(2))
            second = self.launch(lambda: self.manager.start_stream(self.session.id, receiver_id))
            self.finish(second)
            self.assertNotIn("error", second)
            counts = self.finish(self.launch(lambda: self.manager.active_count))
            self.assertEqual(counts.get("value"), 1)
            self.assertEqual(
                self.manager.tune(self.session.id, receiver_id, {"frequency_hz": 433_075_000})["tuned"]["frequency_hz"],
                433_075_000,
            )
            self.manager.stop_stream(self.session.id, receiver_id)
            release.set()
            self.finish(first)
        self.assertNotIn("error", first)
        self.assertEqual(self.manager.active_count, 1)

    def paused_poll(self, adapter, entered, release):
        original = adapter.poll

        def poll():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release poll")
            return original()

        return poll

    def test_stop_timeout_retains_live_owner_and_other_stream_is_independent(self) -> None:
        entered, release = self.gate()
        adapter = TinySASimulator(self.session.streams[0])
        with patch.object(adapter, "poll", self.paused_poll(adapter, entered, release)):
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                self.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(WorkerError, "не остановился"):
                self.manager.stop_stream(self.session.id, self.stream_id, timeout=0.01)
            self.assertEqual(self.manager.active_count, 1)
            self.assertEqual(len(self.live_threads()), 1)
            with self.assertRaisesRegex(WorkerError, "уже запущен"):
                self.start()
            receiver_id = self.session.streams[1].id
            self.manager.start_stream(self.session.id, receiver_id)
            self.manager.stop_stream(self.session.id, receiver_id)
            release.set()
            self.manager.stop_stream(self.session.id, self.stream_id)
        self.assertEqual(self.live_threads(), [])
        self.assertTrue(self.start()["alive"])

    def test_close_timeout_retains_owner_and_rejects_all_new_starts(self) -> None:
        entered, release = self.gate()
        adapter = TinySASimulator(self.session.streams[0])
        original_stop = self.manager._stop_worker
        with patch.object(adapter, "poll", self.paused_poll(adapter, entered, release)):
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                self.start()
            self.assertTrue(entered.wait(2))
            with patch.object(
                self.manager, "_stop_worker",
                side_effect=lambda *args: original_stop(*args, timeout=0.01),
            ):
                self.manager.close()
            self.assertEqual(self.manager.active_count, 1)
            with self.assertRaisesRegex(WorkerError, "закрыт"):
                self.manager.start_stream(self.session.id, self.session.streams[1].id)
            release.set()
            self.manager.close()
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)

    def test_stop_session_attempts_other_workers_after_a_stop_timeout(self) -> None:
        self.manager.start_session(self.session.id)
        worker = self.manager._workers[(self.session.id, self.stream_id)]
        with patch.object(worker, "stop", side_effect=WorkerError("stop failed")):
            with self.assertRaisesRegex(WorkerError, "stop failed"):
                self.manager.stop_session(self.session.id)
        self.assertTrue(worker.alive)
        receiver_id = self.session.streams[1].id
        self.assertFalse(self.manager._workers[(self.session.id, receiver_id)].alive)
        self.assertTrue(self.manager.start_stream(self.session.id, receiver_id)["alive"])

    def test_old_bulk_start_cannot_resume_after_session_workers_stop(self) -> None:
        entered, release = self.gate()
        original_start = self.manager._start_stream

        def start(key, lifecycle, generation):
            result = original_start(key, lifecycle, generation)
            if key[1] == self.stream_id:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Test did not release bulk startup")
            return result

        with patch.object(self.manager, "_start_stream", side_effect=start):
            started = self.launch(lambda: self.manager.start_session(self.session.id))
            self.assertTrue(entered.wait(2))
            stopped = self.manager.stop_session(self.session.id)
            self.assertTrue(all(not status["alive"] for status in stopped))
            release.set()
            self.finish(started)
        self.assertIsInstance(started.get("error"), WorkerError)
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(all(not status["alive"] for status in self.manager.list_workers()))
        self.assertEqual(self.live_threads(), [])
        self.assertTrue(all(status["alive"] for status in self.manager.start_session(self.session.id)))

    def test_failed_ready_state_write_closes_adapter_and_preserves_start_error(self) -> None:
        adapter = TinySASimulator(self.session.streams[0])
        with patch.object(adapter, "close", side_effect=RuntimeError("close failed")) as close:
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                with patch.object(self.service, "set_stream_state", side_effect=RuntimeError("state failed")):
                    with self.assertRaisesRegex(RuntimeError, "state failed"):
                        self.start()
            close.assert_called_once_with()
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(self.start()["alive"])

    def test_worker_constructor_error_closes_adapter_before_registration(self) -> None:
        adapter = TinySASimulator(self.session.streams[0])
        with patch.object(adapter, "close", wraps=adapter.close) as close:
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                with patch("radioinspector.device_workers.DeviceWorker", side_effect=RuntimeError("constructor failed")):
                    with self.assertRaisesRegex(RuntimeError, "constructor failed"):
                        self.start()
            close.assert_called_once_with()
        self.assertEqual(self.live_threads(), [])
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(self.start()["alive"])

    def test_invalid_ids_do_not_allocate_lifecycle_reservations(self) -> None:
        for index in range(25):
            for action in (self.manager.start_stream, self.manager.stop_stream):
                with self.assertRaises(WorkerError):
                    action(self.session.id, f"invalid-stream-{index}")
                with self.assertRaises(KeyError):
                    action(f"invalid-session-{index}", self.stream_id)
        self.assertEqual(self.manager._lifecycles, {})
        self.assertEqual(self.manager._workers, {})

    def test_nested_session_stop_guards_reject_start_until_outer_exit(self) -> None:
        with self.manager.stopping_session(self.session.id):
            with self.manager.stopping_session(self.session.id):
                with self.assertRaises(WorkerError):
                    self.start()
            with self.assertRaises(WorkerError):
                self.start()
            other = self.service.create_session(simulated_session_payload())
            self.service.start_session(other.id)
            self.assertTrue(self.manager.start_stream(other.id, other.streams[0].id)["alive"])
        self.assertTrue(self.start()["alive"])

    def test_adapter_close_error_is_recorded_and_does_not_leak_executor(self) -> None:
        adapter = TinySASimulator(self.session.streams[0])
        with patch.object(adapter, "close", side_effect=WorkerError("close failed")) as close:
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                self.start()
            stopped = self.manager.stop_stream(self.session.id, self.stream_id)
            close.assert_called_once_with()
        self.assertEqual(stopped["state"], "error")
        self.assertIn("close failed", stopped["last_error"])
        self.assertIsNotNone(stopped["ended_at"])
        self.assertFalse(stopped["alive"])
        self.assertEqual(self.live_threads(), [])

    def test_stop_rejects_queued_tune_and_new_commands_before_poll_finishes(self) -> None:
        entered, release = self.gate()
        queued = threading.Event()
        stream = self.session.streams[1]
        adapter = create_simulated_adapter(stream)
        with patch.object(adapter, "poll", self.paused_poll(adapter, entered, release)):
            with patch.object(adapter, "execute", wraps=adapter.execute) as execute:
                with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                    self.manager.start_stream(self.session.id, stream.id)
                self.assertTrue(entered.wait(2))
                worker = self.manager._workers[(self.session.id, stream.id)]
                put = worker._commands.put

                def enqueue(request):
                    put(request)
                    if request["command"] == "tune":
                        queued.set()

                with patch.object(worker._commands, "put", side_effect=enqueue):
                    tuned = self.launch(lambda: self.manager.tune(
                        self.session.id, stream.id, {"frequency_hz": 433_075_000}
                    ))
                    self.assertTrue(queued.wait(2))
                    with self.assertRaisesRegex(WorkerError, "не остановился"):
                        self.manager.stop_stream(self.session.id, stream.id, timeout=0.01)
                    self.finish(tuned)
                self.assertRegex(str(tuned.get("error")), "остановлен")
                with self.assertRaisesRegex(WorkerError, "не запущен"):
                    worker.submit("tune", {"frequency_hz": 433_075_000}, timeout=0.01)
                release.set()
                self.manager.stop_stream(self.session.id, stream.id)
                execute.assert_not_called()

    def test_queued_tune_timeout_does_not_execute_later(self) -> None:
        entered, release = self.gate()
        stream = self.session.streams[1]
        adapter = create_simulated_adapter(stream)
        with patch.object(adapter, "poll", self.paused_poll(adapter, entered, release)):
            with patch.object(adapter, "execute", wraps=adapter.execute) as execute:
                with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                    self.manager.start_stream(self.session.id, stream.id)
                self.assertTrue(entered.wait(2))
                worker = self.manager._workers[(self.session.id, stream.id)]
                with self.assertRaisesRegex(WorkerError, "не выполнена"):
                    worker.submit("tune", {"frequency_hz": 433_075_000}, timeout=0.01)
                release.set()
                # A subsequent command proves that the queue was actually drained.
                worker.submit("tune", {"frequency_hz": 145_600_000})
                execute.assert_called_once_with("tune", {"frequency_hz": 145_600_000})

    def test_fatal_poll_failure_completes_waiting_commands(self) -> None:
        entered, release = self.gate()
        queued = threading.Event()
        stream = self.session.streams[1]
        adapter = create_simulated_adapter(stream)
        adapter.max_consecutive_errors = 1

        def fail_poll():
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release failed poll")
            raise WorkerError("poll failed")

        with patch.object(adapter, "poll", side_effect=fail_poll):
            with patch("radioinspector.serial_instruments.create_device_adapter", return_value=adapter):
                self.manager.start_stream(self.session.id, stream.id)
            self.assertTrue(entered.wait(2))
            worker = self.manager._workers[(self.session.id, stream.id)]
            put = worker._commands.put

            def enqueue(request):
                put(request)
                queued.set()

            with patch.object(worker._commands, "put", side_effect=enqueue):
                tuned = self.launch(lambda: worker.submit("tune", {"frequency_hz": 433_075_000}))
                self.assertTrue(queued.wait(2))
                release.set()
                self.finish(tuned)
            self.manager.stop_stream(self.session.id, stream.id)
        self.assertIsInstance(tuned.get("error"), WorkerError)
        self.assertEqual(worker.status()["state"], "error")
        self.assertEqual(worker.status()["commands_completed"], 0)
        self.assertEqual(self.manager.active_count, 0)


if __name__ == "__main__":
    unittest.main()
