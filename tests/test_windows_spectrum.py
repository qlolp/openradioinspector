"""Проверка отображения исходных спектральных sweeps в Windows-клиенте."""

from __future__ import annotations

import queue
import unittest
from concurrent.futures import Future
from threading import Event, Lock

from radioinspector.windows.app import WindowsApp
from radioinspector.windows.spectrum import (
    peak_of,
    sampled_indices,
    waterfall_color,
    waterfall_levels,
)


class SpectrumPresentationTests(unittest.TestCase):
    def test_sampling_preserves_edges_and_actual_peak(self) -> None:
        indices = sampled_indices(5001, peak_index=3456)
        self.assertLessEqual(len(indices), 1000)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], 5000)
        self.assertIn(3456, indices)
        self.assertEqual(indices, sorted(set(indices)))
        self.assertEqual(sampled_indices(3, peak_index=1), [0, 1, 2])

    def test_peak_preserves_integer_hz_and_source_dbm(self) -> None:
        sweep = {
            "frequencies_hz": [100_000_000, 100_100_000, 100_200_000],
            "levels": [-91.5, -42.25, -80.0],
            "level_unit": "dBm",
        }
        self.assertEqual(peak_of(sweep), (1, -42.25, 100_100_000))

    def test_waterfall_uses_max_aggregation_and_chronological_rows(self) -> None:
        sweeps = [
            {
                "frequencies_hz": [1, 2, 3, 4],
                "levels": [-90.0, -40.0, -80.0, -30.0],
            },
            {
                "frequencies_hz": [1, 2, 3, 4],
                "levels": [-70.0, -60.0, -50.0, -20.0],
            },
        ]
        rows, minimum, maximum = waterfall_levels(sweeps, 2)
        self.assertEqual(rows, [[-40.0, -30.0], [-60.0, -20.0]])
        self.assertEqual(minimum, -90.0)
        self.assertEqual(maximum, -20.0)

    def test_waterfall_rejects_mismatched_points_and_handles_flat_scale(self) -> None:
        with self.assertRaisesRegex(ValueError, "разную длину"):
            waterfall_levels([{"frequencies_hz": [1], "levels": []}], 1)
        color = waterfall_color(-50.0, -50.0, -50.0)
        self.assertRegex(color, r"^#[0-9a-f]{6}$")
        self.assertEqual(waterfall_color(-50.0, -100.0, 0.0), "#1faab8")

    def test_draw_cache_and_batched_waterfall_pixels_avoid_repeated_tk_work(self) -> None:
        class FakeCanvas:
            def __init__(self, width, height):
                self.width = width
                self.height = height
                self.delete_calls = 0

            def winfo_width(self):
                return self.width

            def winfo_height(self):
                return self.height

            def delete(self, *_args):
                self.delete_calls += 1

            def __getattr__(self, name):
                if name.startswith("create_"):
                    return lambda *_args, **_kwargs: None
                raise AttributeError(name)

        class FakeVariable:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        class FakePhoto:
            def __init__(self, *, width, height):
                self.width = width
                self.height = height
                self.put_calls = []

            def put(self, data, **kwargs):
                self.put_calls.append((data, kwargs))

        class FakeTk:
            PhotoImage = FakePhoto

        app = WindowsApp.__new__(WindowsApp)
        app.spectrum_canvas = FakeCanvas(640, 240)
        app.waterfall_canvas = FakeCanvas(320, 120)
        app.spectrum_info_var = FakeVariable()
        app.waterfall_info_var = FakeVariable()
        app.tk = FakeTk()
        app._spectrum_draw_key = None
        app._waterfall_draw_key = None
        app._waterfall_photo = None
        sweep = {
            "id": "sweep/1",
            "frequencies_hz": [100, 200, 300],
            "levels": [-90.0, -40.0, -80.0],
        }
        app._draw_spectrum(sweep, 1, -40.0, 200)
        app._draw_spectrum(sweep, 1, -40.0, 200)
        self.assertEqual(app.spectrum_canvas.delete_calls, 1)

        app._draw_waterfall([sweep])
        app._draw_waterfall([sweep])
        self.assertEqual(app.waterfall_canvas.delete_calls, 1)
        self.assertEqual(len(app._waterfall_photo.put_calls), 1)
        pixel_data, put_options = app._waterfall_photo.put_calls[0]
        self.assertEqual(put_options, {})
        self.assertEqual(pixel_data.count("{"), 120)

        app._clear_spectrum("Очищено")
        self.assertIsNone(app._spectrum_draw_key)
        self.assertIsNone(app._waterfall_draw_key)
        self.assertIsNone(app._waterfall_photo)
        app._draw_spectrum(sweep, 1, -40.0, 200)
        app._draw_waterfall([sweep])
        self.assertEqual(app.spectrum_canvas.delete_calls, 3)
        self.assertEqual(app.waterfall_canvas.delete_calls, 3)

    def test_repeated_programmatic_stream_selection_does_not_refresh(self) -> None:
        class FakeVariable:
            def set(self, _value):
                pass

        app = WindowsApp.__new__(WindowsApp)
        app._last_selected_stream_id = "tiny/sa"
        app._render_controls = lambda: None
        app._selected_stream = lambda: {"id": "tiny/sa", "settings": {}}
        app.snapshot = type("Snapshot", (), {"workers": []})()
        app.frequency_var = FakeVariable()
        app.mode_var = FakeVariable()
        calls = []
        app._render_spectrum = lambda: calls.append("render")
        app.refresh = lambda: calls.append("refresh")
        app._select_stream()
        self.assertEqual(calls, [])

    def test_refresh_captures_session_and_spectrum_before_dispatch(self) -> None:
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def snapshot(self, session_id, *, spectrum_stream_id):
                self.calls.append((session_id, spectrum_stream_id))
                return object()

        class ImmediateExecutor:
            def submit(self, operation, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(operation(*args, **kwargs))
                except Exception as exc:
                    future.set_exception(exc)
                return future

        app = WindowsApp.__new__(WindowsApp)
        app.client = RecordingClient()
        app.session_id = "session/old"
        app._spectrum_stream_id = lambda: "tiny/old"
        app._refresh_running = False
        app._closed = False
        app._revision = 7
        app._executor = ImmediateExecutor()
        app._messages = queue.Queue()
        app._message_lock = Lock()
        app._messages_closed = Event()
        app.refresh()
        app.session_id = "session/new"
        app._spectrum_stream_id = lambda: "tiny/new"
        self.assertEqual(app.client.calls, [("session/old", "tiny/old")])
        kind, future, revision, _description = app._messages.get_nowait()
        self.assertEqual((kind, revision), ("snapshot", 7))
        self.assertIsNotNone(future.result())


if __name__ == "__main__":
    unittest.main(verbosity=2)
