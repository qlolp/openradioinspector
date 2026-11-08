"""Measurement map: points and antenna sectors as GeoJSON, nothing invented."""

from __future__ import annotations

import http.client
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.geoloc import haversine_m as haversine
from radioinspector.measurement_map import (
    MAP_FORMAT, MAX_POINTS, SECTOR_ARC_SEGMENTS, MeasurementMapError,
    build_measurement_map, parse_measurement_map, validate_map_request,
)
from radioinspector.session_service import SessionNotFoundError, SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session


def point(**changes):
    item = {"latitude_deg": 55.75, "longitude_deg": 37.62, "label": "Москва",
            "sector": {"azimuth_deg": 90.0, "beamwidth_deg": 60.0, "radius_m": 1000.0}}
    item.update(changes)
    return item


def map_document(points=None, **changes):
    document = {
        "format": MAP_FORMAT, "format_version": 1, "name": "Карта",
        "points": points if points is not None else [point()],
    }
    document.update(changes)
    return document


def map_json(points=None, **changes):
    return json.dumps(map_document(points, **changes), ensure_ascii=False)


class MapContractTests(unittest.TestCase):
    def test_request_shape_and_names(self):
        parsed = validate_map_request({"map_json": map_json()})
        self.assertEqual(parsed["name"], "Карта")
        self.assertEqual(parsed["points"], [point()])
        for bad in (None, [], True, {}, {"map_json": 1}, {"map_json": "{}", "extra": 1},
                    {"map_json": json.dumps(map_document(**{"extra": 1}))},
                    {"map_json": json.dumps(map_document(name=""))},
                    {"map_json": json.dumps(map_document(points=[]))}):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                validate_map_request(bad)
        many = [dict(point(), latitude_deg=10.0 + i) for i in range(MAX_POINTS + 1)]
        with self.assertRaises(ValueError):
            validate_map_request({"map_json": map_json(many)})

    def test_point_and_sector_ranges(self):
        cases = [
            {"latitude_deg": 91.0}, {"latitude_deg": True}, {"latitude_deg": float("nan")},
            {"longitude_deg": 181.0}, {"longitude_deg": -181.0},
            {"label": "x" * 81}, {"label": 1},
            {"sector": {}},
            {"sector": {"azimuth_deg": 360.0, "beamwidth_deg": 60.0, "radius_m": 1000.0}},
            {"sector": {"azimuth_deg": -1.0, "beamwidth_deg": 60.0, "radius_m": 1000.0}},
            {"sector": {"azimuth_deg": 0.0, "beamwidth_deg": 0.5, "radius_m": 1000.0}},
            {"sector": {"azimuth_deg": 0.0, "beamwidth_deg": 181.0, "radius_m": 1000.0}},
            {"sector": {"azimuth_deg": 0.0, "beamwidth_deg": 60.0, "radius_m": 0.5}},
            {"sector": {"azimuth_deg": 0.0, "beamwidth_deg": 60.0, "radius_m": 200_001.0}},
            {"extra": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_map_request({"map_json": map_json([point(**changes)])})
        # A point without a sector is valid and produces no polygon.
        parsed = validate_map_request({"map_json": map_json([point(sector=None)])})
        self.assertIsNone(parsed["points"][0]["sector"])

    def test_geojson_points_and_sector_geometry(self):
        result = build_measurement_map("Карта", [point(), point(label="Вторая", sector=None)])
        self.assertEqual(result["summary"], {
            "point_count": 2, "sector_count": 1,
            "bbox": {"min_latitude_deg": 55.75, "max_latitude_deg": 55.75,
                     "min_longitude_deg": 37.62, "max_longitude_deg": 37.62},
        })
        features = result["geojson"]["features"]
        self.assertEqual(len(features), 3)
        self.assertEqual(features[0]["geometry"], {"type": "Point", "coordinates": [37.62, 55.75]})
        self.assertEqual(features[0]["properties"]["kind"], "measurement_point")
        ring = features[1]["geometry"]["coordinates"][0]
        self.assertEqual(features[1]["properties"]["kind"], "antenna_sector")
        self.assertEqual(len(ring), SECTOR_ARC_SEGMENTS + 3, "apex + arc + closing apex")
        self.assertEqual(ring[0], [37.62, 55.75])
        self.assertEqual(ring[-1], [37.62, 55.75])
        # The arc stays at the sector radius on the great circle.
        apex = (55.75, 37.62)
        for vertex in ring[1:-1]:
            distance = haversine(apex[0], apex[1], vertex[1], vertex[0])
            self.assertAlmostEqual(distance, 1000.0, delta=2.0)
        # Edges sit at azimuth ± half beam width.
        left = ring[1]
        bearing = _bearing(apex[0], apex[1], left[1], left[0])
        self.assertAlmostEqual(bearing, 60.0, delta=1.0)

    def test_map_roundtrip(self):
        parsed = parse_measurement_map(map_json())
        rebuilt = build_measurement_map(parsed["name"], parsed["points"])
        again = parse_measurement_map(json.dumps(rebuilt["map"], ensure_ascii=False))
        self.assertEqual(again, parsed)


def _bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


class MapServiceMixin:
    def test_builds_map_without_writes(self):
        result = self.service.measurement_map(self.session.id, {"map_json": map_json()})
        self.assertEqual(result["session_id"], self.session.id)
        self.assertEqual(result["summary"]["point_count"], 1)
        before = (self.service.health(), self.service.events())
        self.service.measurement_map(self.session.id, {"map_json": map_json()})
        self.assertEqual((self.service.health(), self.service.events()), before)

    def test_missing_session(self):
        with self.assertRaises(SessionNotFoundError):
            self.service.measurement_map("no-such-session", {"map_json": map_json()})

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["measurement_map"])


class MemoryMapTests(MapServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteMapTests(MapServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class MapHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/measurement-map"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, body, *, path=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path or self.root, body=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_builds_over_http(self):
        status, envelope = self.request({"map_json": map_json()})
        self.assertEqual(status, 200)
        data = envelope["data"]
        self.assertEqual(data["geojson"]["type"], "FeatureCollection")
        self.assertEqual(data["summary"]["sector_count"], 1)

    def test_error_statuses(self):
        cases = [
            ({"map_json": "{}"}, 400, "invalid_request"),
            ({"map_json": map_json([point(latitude_deg=91.0)])}, 400, "invalid_request"),
            ({}, 400, "invalid_request"),
        ]
        for body, status, error in cases:
            with self.subTest(body=body):
                code, envelope = self.request(body)
                self.assertEqual(code, status)
                self.assertEqual(envelope["error"]["code"], error)
        code, envelope = self.request({"map_json": map_json()},
                                      path="/api/v1/sessions/no-such-session/measurement-map")
        self.assertEqual(code, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_query_parameters_rejected(self):
        code, envelope = self.request({"map_json": map_json()}, path=self.root + "?x=1")
        self.assertEqual(code, 400)
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_capability_advertised(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", "/api/v1/capabilities")
            response = connection.getresponse()
            envelope = json.loads(response.read())
        finally:
            connection.close()
        self.assertTrue(envelope["data"]["features"]["measurement_map"])


if __name__ == "__main__":
    unittest.main()
