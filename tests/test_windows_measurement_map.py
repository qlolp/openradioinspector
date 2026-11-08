"""Windows measurement-map panel: derived map from supplied coordinates."""
import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.measurement_map import (
    MeasurementMapPanel, result_text, validate_result,
)


def map_json():
    return json.dumps({
        "format": "openradioinspector-measurement-map", "format_version": 1, "name": "Карта",
        "points": [{"latitude_deg": 55.75, "longitude_deg": 37.62, "label": "Точка 1",
                    "sector": {"azimuth_deg": 90.0, "beamwidth_deg": 60.0, "radius_m": 1000.0}}],
    }, ensure_ascii=False)


def response():
    return {"session_id": "session",
            "geojson": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.62, 55.75]},
                 "properties": {"kind": "measurement_point", "label": "Точка 1"}},
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[37.62, 55.75], [37.63, 55.76], [37.62, 55.75]]]},
                 "properties": {"kind": "antenna_sector", "label": "Точка 1"}},
            ]},
            "summary": {"point_count": 1, "sector_count": 1,
                        "bbox": {"min_latitude_deg": 55.75, "max_latitude_deg": 55.75,
                                 "min_longitude_deg": 37.62, "max_longitude_deg": 37.62}},
            "map": {"format": "openradioinspector-measurement-map", "format_version": 1,
                    "name": "Карта", "points": []}}


def panel():
    p = MeasurementMapPanel.__new__(MeasurementMapPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session", root=Mock())
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = None
    p.closed = False
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "info", "details", "editor"):
        setattr(p, key, Mock())
    p.editor.get.return_value = map_json()
    return p


class MapPanelTests(unittest.TestCase):
    def test_result_and_text(self):
        self.assertEqual(validate_result(response(), "session"), response())
        text = result_text(response())
        self.assertIn("Точек: 1, секторов: 1", text)
        self.assertIn("не измерение и не привязка GPS", text)

    def test_result_rejections(self):
        cases = [{"session_id": "other"}, {"summary": {}},
                 {"geojson": {"type": "FeatureCollection", "features": []}},
                 {"summary": {"point_count": -1, "sector_count": 0, "bbox": {}}}]
        for changes in cases:
            bad = response(); bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(bad, "session")

    def test_analyze_roundtrip_and_forged_result(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.measurement_map)
        self.assertEqual(args[2], map_json())
        future.set_result(response())
        p.deadline = p.deadline + 100
        p.finish(p.generation, "session")
        self.assertIn("Карта построена", p.info.set.call_args[0][0])
        # A forged result never freezes.
        p2 = panel()
        forged = response(); forged["session_id"] = "other"
        future2 = Future(); future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.analyze()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation, "session")
        self.assertIn("недоступна", p2.info.set.call_args[0][0])

    def test_bad_draft_never_reaches_the_core(self):
        p = panel()
        p.editor.get.return_value = ""
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.editor.get.return_value = "{}"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.client = None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_scope_change_clears(self):
        p = panel()
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertEqual(p.scope, (id(p.app.client), "other"))


if __name__ == "__main__":
    unittest.main()
