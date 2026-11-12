"""Actual legacy Handler/HTTP parity against the installed one-shot worker.

Run with ORI_CORE_TEST_BINARY and ORI_TEST_ANALYSIS_PYTHON after cargo build.
The Python driver uses the repository's unchanged legacy authority; its worker
runs with -I from the installed environment. No hardware or real archive is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_name] = "1"
ROOT = Path(os.environ.get("ORI_TEST_LEGACY_SOURCE_ROOT", Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(ROOT))
from tests.test_legacy_rpc import create_pan, direct_get, direct_threshold, source_rows
from radioinspector.web.server import ApplicationState
from radioinspector.jsonio import to_jsonable


def normalized(value):
    if isinstance(value, dict):
        return {key: ("__ORIGINAL_PAN__" if key == "database" and item is not None
                      else normalized(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


class LegacyHttpParity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ori-legacy-http-")
        self.addCleanup(self.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "candidate-é-e\u0301-😀.pan"
        self.reference_source = self.root / "reference-é-e\u0301-😀.pan"
        create_pan(self.source)
        create_pan(self.reference_source)
        self.original = self.audit(self.source)
        self.reference = ApplicationState(library=str(self.root))
        binary = Path(os.environ.get("ORI_CORE_TEST_BINARY", Path(__file__).resolve().parents[1] / "target/debug/ori-core"))
        worker = Path(os.path.abspath(os.environ.get("ORI_TEST_ANALYSIS_PYTHON", sys.executable)))
        self.data = self.root / "core"
        self.data.mkdir(mode=0o700)
        ready = self.data / "ready.url"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ORI_RUNTIME_")}
        self.child = subprocess.Popen([str(binary), "--mode", "replay", "--data-dir", str(self.data),
            "--port", "0", "--ready-file", str(ready), "--stdin-lifeline", "--analysis-python", str(worker),
            "--research-best-effort", "--legacy-library", str(self.root)], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if self.child.poll() is not None:
                self.fail("Owned candidate exited before readiness: " + self.child.stderr.read().decode())
            if time.monotonic() >= deadline:
                self.fail("Owned candidate did not publish readiness")
            time.sleep(0.01)
        self.url = ready.read_text().strip()

    def cleanup(self):
        abnormal = False
        child = getattr(self, "child", None)
        if child:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                abnormal = True
                child.kill()  # Only this fixture's unreaped owned process.
                child.wait(timeout=5)
            abnormal = abnormal or child.returncode != 0
            child.stderr.close()
        if hasattr(self, "reference"):
            self.reference.close()
        self.temp.cleanup()
        self.assertFalse(abnormal, "Owned candidate did not complete graceful cleanup")

    @staticmethod
    def audit(path):
        with sqlite3.connect(path) as db:
            lines = db.execute("SELECT * FROM threshold_line WHERE line_index<>0 ORDER BY line_index").fetchall()
            schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        return source_rows(path), lines, schema

    def exact(self, expected, actual, path="$"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(set(expected), set(actual), path)
            for key in expected:
                self.exact(expected[key], actual[key], f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            self.assertEqual(len(expected), len(actual), path)
            for index, (left, right) in enumerate(zip(expected, actual)):
                self.exact(left, right, f"{path}[{index}]")
        elif isinstance(expected, float):
            self.assertIsInstance(actual, (int, float), path)
            self.assertEqual(struct.pack("!d", expected), struct.pack("!d", actual), path)
        else:
            self.assertEqual(expected, actual, path)

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        request = Request(self.url + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as response:
            return response.code, json.loads(response.read())
        with response:
            return response.status, json.loads(response.read())

    def test_all_legacy_actions_match_actual_handler_and_preserve_original_source(self):
        for route in ("/api/state", "/api/signals", "/api/coverage", "/api/occupancy", "/api/efficiency"):
            status, expected = direct_get(self.reference, route)
            actual_status, actual = self.request("GET", route)
            self.assertEqual(actual_status, status, route)
            self.exact(to_jsonable(expected), actual, route)
        status, loaded = self.request("POST", "/api/database", {"path": str(self.source)})
        if os.name == "nt":
            self.assertEqual(status, 503, loaded)
            self.assertEqual(loaded["ok"], False)
            self.assertEqual(self.audit(self.source), self.original)
            with sqlite3.connect(self.data / "sessions.sqlite3") as db:
                self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            return
        self.reference.load_database(str(self.reference_source))
        self.assertEqual(status, 200, loaded)
        self.exact({"ok": True, "version": self.reference.version}, loaded)
        self.assertEqual(self.request("GET", "/api/state")[1]["database"], str(self.source))
        for route in ("/api/state", "/api/signals", "/api/coverage?rows=3&cols=4", "/api/occupancy",
                      "/api/occupancy?threshold=-30", "/api/efficiency", "/api/keywords",
                      "/api/measure?start=100000000.625&stop=100000002.125&norm=1"):
            status, expected = direct_get(self.reference, route)
            actual_status, actual = self.request("GET", route)
            self.assertEqual(actual_status, status, route)
            self.exact(normalized(to_jsonable(expected)), normalized(actual), route)
        for payload in ({"offset_db": 3.5}, {"offset_db": 4., "frequencies": [100000000.125, 100000002.625],
                        "levels": [-0., -45.]}, {}, {"frequencies": [1, 1], "levels": [-80, -70]}, {"offset_db": "bad"}):
            status, expected = direct_threshold(self.reference, payload)
            actual_status, actual = self.request("POST", "/api/threshold", payload)
            self.assertEqual(actual_status, status, payload)
            self.exact(to_jsonable(expected), actual)
            self.exact(normalized(to_jsonable(self.reference.snapshot())), normalized(self.request("GET", "/api/state")[1]))
            self.assertEqual(self.audit(self.source), self.original)


if __name__ == "__main__":
    unittest.main()
