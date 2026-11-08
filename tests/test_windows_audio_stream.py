"""Windows audio-stream panel: delivery counted honestly, nothing recorded."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.audio_stream import (
    AudioStreamPanel, pump_text, status_text, validate_status,
)


def state(subscribers=1, dropped=3):
    return {"session_id": "session", "stream_id": "tiny",
            "subscribers": subscribers, "max_subscribers": 4,
            "queued_blocks": 2, "dropped_blocks": dropped}


class FakeConnection:
    def __init__(self, blocks):
        self.blocks = list(blocks)
        self.closed = False
        self.content_type = "audio/pcm"

    def read_block(self, max_bytes=8192):
        if self.closed:
            return b""
        return self.blocks.pop(0) if self.blocks else b""

    def cancel_read(self):
        self.closed = True

    def close(self):
        self.closed = True


def panel():
    p = AudioStreamPanel.__new__(AudioStreamPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session",
                            selected_stream_id=lambda: "tiny", root=Mock(),
                            snapshot=SimpleNamespace(session={"id": "session"}))
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = None
    p.closed = False
    p.state = None
    p.pump_generation = None
    p.pump_stats = None
    p.stop_event = None
    p.pump_future = None
    p.connection = None
    p._connection_lock = __import__("threading").Lock()
    p.executor = Mock()
    p.executor.submit.side_effect = lambda *args: Future()
    for key in ("connect_button", "disconnect_button", "info", "details"):
        setattr(p, key, Mock())
    return p


class AudioStreamValidationTests(unittest.TestCase):
    def test_status_and_text(self):
        self.assertEqual(validate_status(state(), "session", "tiny"), state())
        text = status_text(state())
        self.assertIn("Подписчиков: 1 из 4", text)
        self.assertIn("потеряно 3 блоков", text)
        self.assertIn("архив не изменяются", text)

    def test_status_rejections(self):
        cases = [{"session_id": "other"}, {"stream_id": "other"}, {"subscribers": "1"},
                 {"subscribers": 5}, {"max_subscribers": 0}, {"dropped_blocks": -1},
                 {"queued_blocks": True}]
        for changes in cases:
            bad = state(subscribers=1)
            bad["max_subscribers"] = 4
            bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_status(bad, "session", "tiny")

    def test_pump_text_reports_delivery_not_recording(self):
        stats = {"received_bytes": 12, "blocks": 1, "error": None, "done": False}
        self.assertIn("Трансляция открыта: получено 12 байт PCM16LE (1 блоков)",
                      pump_text(stats, True))
        self.assertIn("Трансляция завершена", pump_text(stats, False))
        self.assertIn("Трансляция прервана: сбой", pump_text({"received_bytes": 0, "blocks": 0, "error": "сбой", "done": True}, False))


class AudioStreamPumpTests(unittest.TestCase):
    def test_pump_counts_bytes_until_end(self):
        p = panel()
        connection = FakeConnection([b"\x01\x02", b"\x03\x04\x05"])
        p.app.client.audio_stream_open.return_value = connection
        import threading
        stats = {"received_bytes": 0, "blocks": 0, "error": None, "done": False}
        p.generation = 1
        p._pump("session", "tiny", 1, stats, threading.Event())
        self.assertEqual(stats["received_bytes"], 5)
        self.assertEqual(stats["blocks"], 2)
        self.assertTrue(stats["done"])
        self.assertIsNone(stats["error"])
        self.assertTrue(connection.closed)

    def test_disconnect_stops_and_close_unblocks(self):
        import threading
        p = panel()
        connection = FakeConnection([b"\x00" * 4])
        p.app.client.audio_stream_open.return_value = connection
        stats = {"received_bytes": 0, "blocks": 0, "error": None, "done": False}
        p.generation = 1
        stop = threading.Event()
        stop.set()
        p._pump("session", "tiny", 1, stats, stop)
        self.assertEqual(stats["blocks"], 0, "stop before first read: no delivery")
        self.assertTrue(stats["done"])

    def test_open_error_reported_not_hidden(self):
        import threading
        p = panel()
        p.app.client.audio_stream_open.side_effect = ValueError("Нет активного сеанса и потока")
        stats = {"received_bytes": 0, "blocks": 0, "error": None, "done": False}
        p.generation = 1
        p._pump("session", "tiny", 1, stats, threading.Event())
        self.assertIn("Нет активного сеанса", stats["error"])
        self.assertTrue(stats["done"])

    def test_connect_submits_pump_and_no_stream_never_reaches_the_core(self):
        p = panel()
        p.app.client = None
        p.connect()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.selected_stream_id = lambda: ""
        p.connect()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_scope_change_stops_pump_and_clears_state(self):
        p = panel()
        p.state = state()
        p.show_details = Mock()
        p.pump_stats = {"received_bytes": 4, "blocks": 1, "error": None, "done": False}
        p.app.session_id = "other"
        p.update()
        self.assertIsNone(p.state)
        self.assertIsNone(p.pump_stats)
        self.assertEqual(p.scope, (id(p.app.client), "other", "tiny"))
        self.assertIsNone(p.future)


class AudioCancellationTests(unittest.TestCase):
    def test_cancellation_interrupts_real_blocked_read_without_closing_on_gui(self):
        import socket, http.client, threading, time
        from radioinspector.windows.client import AudioStreamConnection
        client, peer = socket.socketpair()
        client.settimeout(2)
        peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: audio/pcm\r\nConnection: close\r\n\r\n')
        response = http.client.HTTPResponse(client); response.begin()
        connection = AudioStreamConnection(response)
        entered = threading.Event()
        def read():
            entered.set()
            try: connection.read_block(8192)
            except OSError: pass
            finally: connection.close()
        worker = threading.Thread(target=read)
        try:
            worker.start(); self.assertTrue(entered.wait(1))
            p = panel(); p.connection = connection
            started = time.monotonic(); p.stop_pump()
            self.assertLess(time.monotonic() - started, 0.5)
            worker.join(1); self.assertFalse(worker.is_alive())
        finally:
            connection.cancel_read(); worker.join(3)
            response.close(); client.close(); peer.close()

    def test_stream_change_invalidates_pending_status(self):
        p = panel(); future = Future(); future.set_running_or_notify_cancel()
        p.executor.submit.side_effect = lambda *args: future
        p.refresh(); current = p.generation
        p.app.selected_stream_id = lambda: 'other'; p.update()
        future.set_result(state()); p.finish(current, 'session', 'tiny')
        self.assertIsNone(p.state)
        self.assertGreater(p.generation, current)


class AudioStreamStatusTests(unittest.TestCase):
    def test_refresh_roundtrip_and_forged_state(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.refresh()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.audio_stream_status)
        self.assertEqual(args[1:], ("session", "tiny"))
        future.set_result(state())
        p.deadline = p.deadline + 100
        p.finish(p.generation, "session", "tiny")
        self.assertEqual(validate_status(p.state, "session", "tiny"), state())
        self.assertIn("Подписчиков", p.details.insert.call_args[0][1])
        # A forged state never freezes the panel.
        p2 = panel()
        forged = state(subscribers=9)
        future2 = Future()
        future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.refresh()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation, "session", "tiny")
        self.assertIsNone(p2.state)
        self.assertIn("недоступен", p2.info.set.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
