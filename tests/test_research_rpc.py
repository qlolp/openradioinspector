"""Pure synthetic RPC/direct-service parity; no devices, archive, or new math."""
from __future__ import annotations

import copy
import base64
import hashlib
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from radioinspector.contracts import Candidate, MonitoringSession, Sweep, to_primitive
from radioinspector.session_service import SessionService
from radioinspector import research_rpc as rpc


ROOT = Path(__file__).resolve().parents[1]
TIME = "2026-11-11T00:00:00.123456Z"
METHODS = (
    "compare_sweeps", "sweep_average_comparison", "sweep_average_change_detection",
    "sweep_threshold", "sweep_three_thresholds", "sweep_context", "sweep_occupancy",
    "sweep_mask_analysis", "candidate_frequency_rounding", "frequency_rounding",
    "sweep_quality", "frequency_counter", "dvb_evaluation", "measurement_map",
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def shared_io_capability(root, *, debt_seconds=0):
    directory=Path(tempfile.mkdtemp(prefix='shared-io-',dir=root))
    directory.chmod(0o700)
    path=directory/'ledger'
    now=time.clock_gettime_ns(time.CLOCK_MONOTONIC) if hasattr(time,'CLOCK_MONOTONIC') else time.monotonic_ns()
    nonce=bytes.fromhex('15'*16)
    raw=bytearray(128);raw[:8]=b'ORIIOV1\0';raw[8:24]=nonce
    struct.pack_into('<5Q',raw,24,20*1024**2,now+int(debt_seconds*1_000_000_000),0,now,1)
    path.write_bytes(raw);path.chmod(0o600)
    return {'version':1,'path':str(path.resolve()),'nonce':nonce.hex(),'clock':'posix-monotonic'},path


def shared_io_total(path):
    return struct.unpack_from('<Q',path.read_bytes(),40)[0]


def request(method="sweep_context", payload=None):
    session = {
        "id": "session-😀", "name": "Scientific RPC", "state": "stopped",
        "created_at": TIME, "started_at": TIME, "ended_at": TIME,
        "contract_version": 1, "metadata": {"bits": [-0.0, 1.0000000000000002]},
        "streams": [{"id": "stream", "session_id": "session-😀", "device": "archive",
                     "role": "spectrum_analyzer", "level_unit": "dBm", "settings": {}}],
    }
    sweeps = [{
        "id": f"sweep-{number}", "session_id": session["id"], "stream_id": "stream",
        "interval": {"started_at": f"2026-11-11T00:00:0{number}.123456Z",
                     "ended_at": f"2026-11-11T00:00:0{number}.223456Z"},
        "frequencies_hz": [100, 200, 300, 400],
        "levels": [-90.00000000000001, -80.0 + number, -60.0 + number, -90.0],
        "level_unit": "dBm", "settings": {"rbw_hz": 10000, "sequence": number,
                                            "extension": {"bits": [-0.0, 1.0000000000000002]}},
    } for number in range(3)]
    candidate = {
        "id": "candidate-𐀀", "session_id": session["id"], "source_stream_id": "stream",
        "interval": {"started_at": TIME, "ended_at": TIME}, "center_frequency_hz": 103,
        "level_unit": "s_meter_count", "peak_level": -0.0, "score": 1.0000000000000002,
        "observation_ids": [], "state": "new", "bandwidth_hz": None,
    }
    return {"version": 1, "request_id": "request-😀", "operation": "analysis", "method": method,
            "session": session, "sweeps": sweeps, "candidates": [candidate],
            "payload": payload if payload is not None else {"sweep_id": "sweep-1", "before": 1, "after": 1}}


def payloads():
    profile = {"format": "openradioinspector-frequency-rounding", "format_version": 1, "name": "Grid",
               "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
               "rules": [{"start_frequency_hz": 0, "end_frequency_hz": 1000, "step_hz": 10,
                          "origin_frequency_hz": 0, "max_offset_hz": 1000}]}
    raster = {"format": "openradioinspector-frequency-raster", "format_version": 1, "name": "Grid",
              "frequency_unit": "Hz", "entries": [{"name": "band", "start_hz": 0,
              "stop_hz": 1000, "grid_step_hz": 10}]}
    mask = {"format": "openradioinspector-spectral-mask", "format_version": 1, "name": "Mask",
            "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
            "vertices": [{"frequency_hz": 0, "limit_dbm": -80}, {"frequency_hz": 1000, "limit_dbm": -80}]}
    return {
        "compare_sweeps": {"reference_id": "sweep-0", "target_id": "sweep-2"},
        "sweep_average_comparison": {"reference_ids": ["sweep-0", "sweep-1"], "target_id": "sweep-2"},
        "sweep_average_change_detection": {"reference_ids": ["sweep-0", "sweep-1"], "target_id": "sweep-2",
                                            "minimum_increase_db": 0.1, "minimum_bins": 1},
        "sweep_threshold": {"sweep_id": "sweep-1", "window_points": 3, "margin_db": 6},
        "sweep_three_thresholds": {"sweep_id": "sweep-1", "levels_dbm": [-90, -80, -70]},
        "sweep_context": {"sweep_id": "sweep-1", "before": 1, "after": 1},
        "sweep_occupancy": {"sweep_id": "sweep-1", "before": 1, "after": 1, "threshold_dbm": -80},
        "sweep_mask_analysis": {"sweep_id": "sweep-1", "mask_json": encode(mask).decode()},
        "candidate_frequency_rounding": {"candidate_id": "candidate-𐀀", "profile_json": encode(profile).decode()},
        "frequency_rounding": {"candidate_ids": ["candidate-𐀀"], "table_json": encode(raster).decode()},
        "sweep_quality": {"sweep_id": "sweep-1", "edge_margin_db": 10, "high_level_dbm": -20},
        "frequency_counter": {"sweep_id": "sweep-1", "resolution_hz": 1, "start_hz": 100, "stop_hz": 400},
        "dvb_evaluation": {"parameters": {"mer_db": 30, "ber_after_viterbi": 0.0001, "ber_after_rs": 0},
                           "thresholds": {"min_mer_db": 20, "max_ber_after_viterbi": 0.001, "max_ber_after_rs": 0},
                           "guard_interval": "1/32", "code_rate": "3/4"},
        "measurement_map": {"map_json": encode({"format": "openradioinspector-measurement-map",
            "format_version": 1, "name": "Map", "points": [{"latitude_deg": 55.75, "longitude_deg": 37.62,
            "label": "Москва", "sector": {"azimuth_deg": 90, "beamwidth_deg": 60, "radius_m": 1000}}]}).decode()},
    }


def direct_service(document):
    service = SessionService(store=None)
    if document["session"] is not None:
        session = MonitoringSession.from_dict(copy.deepcopy(document["session"]))
        service._sessions[session.id] = session
    service._sweeps = [Sweep.from_dict(copy.deepcopy(item)) for item in document["sweeps"]]
    service._candidates = {item["id"]: Candidate.from_dict(copy.deepcopy(item)) for item in document["candidates"]}
    return service


def direct_call(service, document):
    sid = document["session"]["id"] if document["session"] is not None else document["session_id"]
    method, payload = document["method"], document["payload"]
    if method == "compare_sweeps":
        return service.compare_sweeps(sid, **payload)
    if method == "sweep_threshold":
        return service.sweep_threshold(sid, **payload)
    return getattr(service, method)(sid, payload)


class ResearchRpcParityTests(unittest.TestCase):
    def assert_parity(self, document):
        before = encode(document)
        service = direct_service(document)
        expected = direct_call(service, document)
        result = rpc.process_request(before)
        self.assertEqual(result.keys(), {"version", "request_id", "operation", "ok", "result"})
        self.assertEqual(result["request_id"], document["request_id"])
        self.assertEqual((result["version"], result["operation"], result["ok"]), (1, "analysis", True))
        self.assertEqual(encode(result["result"]), encode(to_primitive(expected)))
        self.assertEqual(before, encode(document))
        return result["result"]

    def assert_error_parity(self, document):
        with self.assertRaises(Exception) as raised:
            direct_call(direct_service(document), document)
        envelope = rpc.process_request(encode(document))
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["type"], type(raised.exception).__name__)
        self.assertEqual(envelope["error"]["message"], str(raised.exception))
        if type(raised.exception) in (ValueError, TypeError):
            self.assertEqual(envelope["error"]["api_code"], "invalid_request")
            self.assertEqual(envelope["error"]["http_status"], 400)
        if envelope["error"]["code"] == "not_found":
            self.assertEqual(envelope["error"]["api_message"], str(raised.exception.args[0]))
            self.assertEqual(envelope["error"]["http_status"], 404)
        return envelope["error"]

    def test_all_fourteen_methods_exact_success_and_error_parity(self):
        self.assertEqual(set(rpc.METHODS), set(METHODS))
        for method, payload in payloads().items():
            with self.subTest(method=method):
                self.assert_parity(request(method, payload))
                self.assert_error_parity(request(method, {}))

    def test_missing_session_preserves_each_methods_validation_error_priority(self):
        # The worker receives the actual absence of a session, without a
        # synthetic DTO. The existing method decides which error comes first.
        for method, payload in payloads().items():
            for body in (payload, {}):
                with self.subTest(method=method, malformed=not body):
                    doc = request(method, body)
                    doc.update(session=None,session_id='missing-session-😀',sweeps=[],candidates=[])
                    self.assert_error_parity(doc)

    def test_stale_exact_ids_units_settings_and_legacy_caps(self):
        for method in ("compare_sweeps", "candidate_frequency_rounding", "frequency_rounding"):
            doc = request(method, payloads()[method])
            doc["sweeps"] = []; doc["candidates"] = []
            self.assertEqual(self.assert_error_parity(doc)["code"], "not_found")
        for method in ("compare_sweeps", "sweep_threshold", "sweep_context", "sweep_occupancy", "frequency_counter"):
            doc = request(method, payloads()[method])
            doc["sweeps"][1]["level_unit"] = "s_meter_count"
            if method == "compare_sweeps": doc["sweeps"][2]["level_unit"] = "s_meter_count"
            self.assertEqual(self.assert_error_parity(doc)["code"], "incompatible")
        doc = request(); doc["sweeps"][0]["settings"]["rbw_hz"] = 999
        self.assert_error_parity(doc)  # Never skip the incompatible nearest source.
        doc = request("sweep_average_comparison", {"reference_ids": [str(i) for i in range(17)], "target_id": "x"})
        self.assert_error_parity(doc)
        doc = request()
        doc["payload"] = {"sweep_id": "sweep-1", "before": 8, "after": 0}
        template = doc["sweeps"][0]
        doc["sweeps"] = [dict(copy.deepcopy(template), id=f"id-{i:02d}",
            frequencies_hz=list(range(16384)), levels=[-90.0] * 16384) for i in range(9)]
        doc["payload"]["sweep_id"] = "id-08"
        self.assert_error_parity(doc)  # Existing 131072-point context bound.

    def test_full_thirty_three_source_context_and_unbounded_counter_legacy_grid(self):
        doc = request()
        template = doc["sweeps"][0]
        doc["sweeps"] = [dict(copy.deepcopy(template), id=f"id-{i:02d}") for i in range(33)]
        doc["payload"] = {"sweep_id": "id-16", "before": 16, "after": 16}
        self.assertTrue(self.assert_parity(doc)["complete"])
        doc = request("frequency_counter", {"sweep_id": "sweep-1", "resolution_hz": 1, "start_hz": 0, "stop_hz": 16384})
        doc["sweeps"] = [doc["sweeps"][1]]
        doc["sweeps"][0].update(frequencies_hz=list(range(16385)), levels=[-90.0] * 16385)
        self.assert_parity(doc)

    def test_full_sixteen_reference_average_keeps_legacy_point_budget(self):
        doc = request("sweep_average_comparison", {
            "reference_ids": [f"source-{i}" for i in range(16)], "target_id": "source-16"})
        template = doc["sweeps"][0]
        doc["sweeps"] = [dict(copy.deepcopy(template), id=f"source-{i}",
            frequencies_hz=list(range(16384)), levels=[-90.0] * 16384) for i in range(17)]
        self.assertEqual(self.assert_parity(doc)["points"], 16384)

    def test_float_bits_negative_zero_time_and_unicode_ids(self):
        doc = request()
        doc["sweeps"][1]["id"] = " exact-𐀀-😀 "
        doc["payload"]["sweep_id"] = " exact-𐀀-😀 "
        doc["sweeps"][1]["levels"] = [-0.0, math.ulp(0.0), 1.0000000000000002, -90.00000000000001]
        result = self.assert_parity(doc)
        source = result["items"][1]
        self.assertEqual(source["id"], " exact-𐀀-😀 ")
        self.assertEqual(source["interval"], doc["sweeps"][1]["interval"])
        self.assertEqual([struct.pack("!d", v) for v in source["levels"]],
                         [struct.pack("!d", v) for v in doc["sweeps"][1]["levels"]])
        self.assertEqual(encode(source["settings"]), encode(doc["sweeps"][1]["settings"]))
        doc = request("candidate_frequency_rounding", payloads()["candidate_frequency_rounding"])
        self.assertEqual(self.assert_parity(doc)["candidate"]["level_unit"], "s_meter_count")

    def test_context_uses_utf8_order_and_never_fills_missing_history(self):
        doc = request()
        template = doc["sweeps"][0]
        doc["sweeps"] = [dict(copy.deepcopy(template), id=identity) for identity in ("𐀀", "\ue000", "😀")]
        doc["payload"] = {"sweep_id": "𐀀", "before": 1, "after": 1}
        self.assertEqual([item["id"] for item in self.assert_parity(doc)["items"]], ["\ue000", "𐀀", "😀"])
        doc["sweeps"] = [doc["sweeps"][0]]
        result = self.assert_parity(doc)
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing"], {"before": 1, "after": 1})

    def test_memory_service_never_writes_database_hardware_or_events(self):
        real_constructor, instances = SessionService, []
        def capture(**kwargs):
            self.assertIsNone(kwargs["store"])
            instance = real_constructor(**kwargs); instances.append(instance)
            return instance
        with patch("radioinspector.session_service.SessionService", side_effect=capture), \
             patch("sqlite3.connect", side_effect=AssertionError("database open")), \
             patch("radioinspector.session_store.SQLiteSessionStore", side_effect=AssertionError("store open")):
            self.assert_parity(request())
        self.assertEqual(len(instances), 1)
        service = instances[0]
        self.assertEqual((service._sequence, len(service._events), len(service._investigations),
                          len(service._measurement_results), len(service._phonograms)), (0, 0, 0, 0, 0))
        self.assertEqual(encode(to_primitive(service._sweeps)), encode(to_primitive(direct_service(request())._sweeps)))


class ResearchRpcProtocolTests(unittest.TestCase):
    def invalid(self, value, code="protocol_error"):
        envelope = rpc.process_request(value if isinstance(value, bytes) else encode(value))
        self.assertEqual(envelope.keys(), {"version", "request_id", "operation", "ok", "error"})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], code)
        return envelope

    def test_strict_json_eof_nonfinite_duplicate_keys_and_utf8(self):
        for raw in (b"", b"{", b"{}{}", b"[1]", b"\xef\xbb\xbf{}", b"\xff", b'{"version":1,"version":1}',
                    b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":-Infinity}', b'{"x":1e400}', b'{"x":"\\ud800"}'):
            with self.subTest(raw=raw): self.invalid(raw)

    def test_strict_envelope_unknown_methods_and_execution_options(self):
        for key, value in (("version", True), ("version", 2), ("request_id", ""), ("request_id", 1),
                           ("operation", "sql"), ("method", "start_session"), ("method", "__dict__"),
                           ("method", []), ("payload", [])):
            doc = request(); doc[key] = value
            self.invalid(doc)
        for key in ("sql", "snapshot_path", "cache_directory", "url", "command", "module"):
            doc = request(); doc[key] = "/tmp/do-not-open"
            self.invalid(doc)
        doc = request(); del doc["session"]["id"]
        self.invalid(doc)

    def test_missing_session_variant_has_no_sources_or_conflicting_full_dto_identity(self):
        valid = request()
        valid.update(session=None,session_id='missing',sweeps=[],candidates=[])
        bad = []
        without_id = dict(valid); del without_id['session_id'];bad.append(without_id)
        for identity in ('',None,True):bad.append(dict(valid,session_id=identity))
        bad.append(dict(valid,sweeps=request()['sweeps']))
        bad.append(dict(valid,candidates=request()['candidates']))
        bad.append(dict(request(),session_id='conflicting'))
        for document in bad:
            with self.subTest(fields=list(document)):
                self.invalid(document)

    def test_duplicate_foreign_missing_source_identity_and_bad_types(self):
        bad = []
        for key in ("sweeps", "candidates"):
            doc = request(); doc[key].append(copy.deepcopy(doc[key][0])); bad.append(doc)
        for scope, field in (("sweeps", "session_id"), ("sweeps", "stream_id"),
                             ("candidates", "session_id"), ("candidates", "source_stream_id")):
            doc = request(); doc[scope][0][field] = "foreign"; bad.append(doc)
        doc = request(); doc["session"]["streams"][0]["session_id"] = "foreign"; bad.append(doc)
        doc = request(); del doc["sweeps"][0]["id"]; bad.append(doc)
        doc = request(); doc["sweeps"][0]["id"] = 42; bad.append(doc)
        doc = request(); doc["sweeps"][0]["levels"][0] = True; bad.append(doc)
        doc = request(); doc["sweeps"][0]["levels"][0] = "-90"; bad.append(doc)
        doc = request(); doc["sweeps"][0]["level_unit"] = 42; bad.append(doc)
        doc = request(); doc["sweeps"][0]["interval"]["started_at"] = "2026-11-11T00:00:00"; bad.append(doc)
        doc = request(); doc["sweeps"][0]["level_unit"] = " dBm "; bad.append(doc)
        doc = request(); doc["session"]["metadata"] = []; bad.append(doc)
        doc = request(); doc["session"]["contract_version"] = True; bad.append(doc)
        doc = request(); doc["session"]["streams"][0]["settings"] = []; bad.append(doc)
        for document in bad:
            with self.subTest(document=document): self.invalid(document)

    def test_source_count_bounds_and_limit_envelope(self):
        doc = request(); doc["sweeps"] *= 12
        self.invalid(doc, "resource_limit")
        doc = request(); doc["candidates"] *= 1001
        self.invalid(doc, "resource_limit")
        doc = request(); doc["limits"] = {"duckdb_threads": 1, "worker_rss_bytes": 1024}
        self.assertTrue(rpc.process_request(encode(doc))["ok"])
        for limits in ({"unknown": 1}, {"duckdb_threads": 3}, {"duckdb_threads": True},
                       {"cache_bytes": 0}, [], {"worker_rss_bytes": 768 * 1024**2 + 1}):
            doc = request(); doc["limits"] = limits
            self.invalid(doc)

    def test_bounded_stream_read_and_atomic_bounded_response(self):
        self.invalid(b" " * (rpc.MAX_REQUEST_BYTES + 1), "resource_limit")
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                self.assertion(size)
                return super().read(min(size, 7))
        source = ShortReads(encode(request())); source.assertion = lambda size: self.assertTrue(0 < size <= 65536)
        output = io.BytesIO()
        self.assertEqual(rpc.run(source, output), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        with patch.object(rpc, "MAX_RESPONSE_BYTES", 1024):
            output = io.BytesIO()
            self.assertEqual(rpc.run(io.BytesIO(encode(request())), output), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["error"]["code"], "resource_limit")
            self.assertLessEqual(len(output.getvalue()), 1024)

    def test_failed_input_or_closed_output_never_leaks_tracebacks(self):
        class FailedInput:
            def read(self, _size):
                raise OSError("private source path")
        destination = io.BytesIO()
        self.assertEqual(rpc.run(FailedInput(), destination), 1)
        self.assertEqual(json.loads(destination.getvalue())["error"]["code"], "protocol_error")
        self.assertNotIn(b"private source path", destination.getvalue())
        class ClosedOutput:
            def write(self, _value):
                raise BrokenPipeError
        self.assertEqual(rpc.run(io.BytesIO(encode(request())), ClosedOutput()), 1)

    def test_subprocess_eof_and_all_methods_without_runtime_version_ban(self):
        for method, payload in payloads().items():
            with self.subTest(method=method):
                doc = request(method, payload)
                child = subprocess.run([sys.executable, "-m", "radioinspector.research_rpc"], input=encode(doc),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, timeout=15)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(child.stderr, b"", child.stderr)
                self.assertEqual(len(child.stdout.splitlines()), 1)
                self.assertEqual(json.loads(child.stdout), rpc.process_request(encode(doc)))
        for raw in (b"", b'{"version":1'):
            child = subprocess.run([sys.executable, "-m", "radioinspector.research_rpc"], input=raw,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, timeout=15)
            self.assertEqual(child.returncode, 1)
            self.assertEqual(json.loads(child.stdout)["error"]["code"], "protocol_error")

    def test_cancelled_partial_request_does_not_dispatch(self):
        child = subprocess.Popen([sys.executable, "-m", "radioinspector.research_rpc"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write(b'{"version":1,"request_id":"cancelled",'); child.stdin.flush()
            child.terminate()
            stdout, _ = child.communicate(timeout=5)
            self.assertEqual(stdout, b"")
            self.assertNotEqual(child.returncode, 0)
        finally:
            if child.poll() is None: child.kill(); child.wait()

    def test_thread_environment_is_set_before_service_import(self):
        # -m initializes this legacy package (and NumPy) before the submodule;
        # the managed parent sets the environment before spawn for that route.
        # Direct installed-file execution can set it without package startup.
        code = ("import os,sys,runpy; os.environ['OPENBLAS_NUM_THREADS']='9'; "
                "runpy.run_path(sys.argv[1],run_name='protocol_guard'); "
                "assert 'numpy' not in sys.modules; "
                "assert os.environ['OPENBLAS_NUM_THREADS']=='1'; "
                "assert os.environ['OMP_NUM_THREADS']=='1'")
        child = subprocess.run([sys.executable, "-c", code, str(ROOT / "radioinspector/research_rpc.py")],
                               cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)


class SharedIoResearchRpcTests(unittest.TestCase):
    def test_optional_private_capability_preserves_all_methods_and_reports_zero_file_io(self):
        with tempfile.TemporaryDirectory() as root:
            capability,ledger=shared_io_capability(root)
            for method,payload in payloads().items():
                ordinary=request(method,payload)
                expected=rpc.process_request(encode(ordinary))
                actual=rpc.process_request(encode(dict(ordinary,shared_io=capability)))
                with self.subTest(method=method):
                    if os.name!='posix':
                        self.assertFalse(actual['ok']);self.assertEqual(actual['error']['code'],'shared_io_unavailable')
                        continue
                    self.assertTrue(actual['ok'],actual)
                    self.assertEqual(actual.pop('shared_io_receipt'),{'version':1,'accounted_bytes':0,'charges':0,'max_charge_bytes':0})
                    self.assertEqual(encode(actual),encode(expected))
                    self.assertNotIn('shared_io_receipt',expected)
            self.assertEqual(shared_io_total(ledger),0)

    def test_private_receipt_cannot_expand_the_final_response_limit(self):
        with tempfile.TemporaryDirectory() as root:
            capability,_=shared_io_capability(root)
            expected=rpc.process_request(encode(request()))
            maximum=len(rpc._encode(expected))
            with patch.object(rpc,'MAX_RESPONSE_BYTES',maximum):
                actual=rpc.process_request(encode(dict(request(),shared_io=capability)))
            self.assertFalse(actual['ok'])
            self.assertNotIn('shared_io_receipt',actual)
            self.assertEqual(actual['error']['code'],'resource_limit' if os.name=='posix' else 'shared_io_unavailable')
            self.assertLessEqual(len(rpc._encode(actual)),maximum)

    def test_invalid_capability_is_refused_before_science_and_never_echoes_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            capability,ledger=shared_io_capability(root)
            for value in (None,{},dict(capability,version=True),dict(capability,clock='unknown'),dict(capability,nonce='ff'*16)):
                with self.subTest(value=value), patch.object(rpc,'_analysis',side_effect=AssertionError('science before capability validation')) as science:
                    response=rpc.process_request(encode(dict(request(),shared_io=value)))
                    self.assertFalse(response['ok'])
                    self.assertEqual(response['error']['code'],'shared_io_unavailable')
                    self.assertNotIn('shared_io_receipt',response)
                    self.assertNotIn(str(ledger),json.dumps(response))
                    self.assertNotIn(capability['nonce'],json.dumps(response))
                    science.assert_not_called()
            self.assertEqual(shared_io_total(ledger),0)


class ControlScienceRpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=json.loads((ROOT/'tests/fixtures/control-science-v1.json').read_text())['cases']

    def document(self,case=None):
        case=case or self.cases[0]
        return {'version':1,'request_id':'control-é-e\u0301-😀','operation':'control_analysis',
                'method':case['method'],'source':copy.deepcopy(case['source']),'rbw_hz':case['rbw_hz']}

    def test_all_five_match_actual_python_controller_and_frozen_oracle(self):
        from radioinspector.tinysa.controller import Controller
        from tests.tinysa_control_fixture import make_adapter
        for case in self.cases:
            with self.subTest(method=case['method']):
                source=case['source']
                scan=base64.b64decode(source['wire_base64'])
                command=f"scanraw {source['frequencies_hz'][0]} {source['requested_stop_hz']} {len(source['levels'])} 0"
                _,_,adapter,wire=make_adapter(self,{command:scan,'rbw':b'rbw {auto|...}\r\n10000Hz\r\n'})
                adapter._clock=lambda:datetime(2026,11,11,12,tzinfo=timezone.utc)
                controller=Controller(adapter,monotonic=lambda:100.)
                controller.execute({'operation':'inspect'})
                oracle=controller.execute({'operation':'measurement','parameters':{'method':case['method'],'points':len(source['levels'])}})['measurement']
                self.assertEqual(encode(oracle),encode(case['result']))
                document=self.document(case);before=encode(document)
                actual=rpc.process_request(before)
                self.assertTrue(actual['ok'],actual)
                self.assertEqual(actual['operation'],'control_analysis')
                self.assertEqual(actual['request_id'],document['request_id'])
                self.assertEqual(encode(actual['result']),encode(oracle))
                self.assertNotIn('source',actual['result'])
                self.assertNotIn('shared_io_receipt',actual)
                self.assertEqual(encode(document),before)

    def test_control_source_negative_zero_and_zero_io_receipt_remain_exact(self):
        document=self.document()
        document['source']['levels']=[-0.0,-1e-300,-1.0000000000000002]
        document['source']['zero_db']=-0.0
        before=encode(document)
        with tempfile.TemporaryDirectory() as root:
            capability,ledger=shared_io_capability(root)
            actual=rpc.process_request(encode(dict(document,shared_io=capability)))
            if os.name!='posix':
                self.assertEqual(actual['error']['code'],'shared_io_unavailable');return
            self.assertTrue(actual['ok'],actual)
            self.assertEqual(actual['shared_io_receipt'],{'version':1,'accounted_bytes':0,'charges':0,'max_charge_bytes':0})
            self.assertEqual(shared_io_total(ledger),0)
        self.assertEqual(struct.pack('>d',actual['result']['details']['level_dbm']),struct.pack('>d',-0.0))
        self.assertEqual(encode(document),before)

    def test_malformed_control_never_calls_science_files_or_tunables(self):
        bad=[]
        for key,value in [('version',True),('method','generator.sweep'),('method',[]),('rbw_hz',True),('rbw_hz',0),('rbw_hz','10000'),('source',None),('path','/do-not-open')]:
            document=self.document();document[key]=value;bad.append(document)
        for field,value in [('frequencies_hz',[1,1,2]),('frequencies_hz',[1,True,3]),('levels',[1,2]),('levels',[1,'bad',3]),
                            ('level_unit','s_meter_count'),('wire_base64','%%%'),('zero_db',True),('started_at','not UTC'),
                            ('ended_at','2025-11-11T12:00:00Z'),('sample_offsets_seconds',[0,1,2]),('timing_source','inferred'),
                            ('grid_source','unknown'),('requested_stop_hz',True),('stop_is_inclusive',True),('path','/do-not-open')]:
            document=self.document();document['source'][field]=value;bad.append(document)
        document=self.document();document['source']['frequencies_hz']=[1];document['source']['levels']=[0];bad.append(document)
        document=self.document();document['source']['frequencies_hz']=list(range(65537));document['source']['levels']=[0]*65537;bad.append(document)
        with patch('radioinspector.tinysa.measurements.analyze',side_effect=AssertionError('science dispatch')) as science, \
             patch('sqlite3.connect',side_effect=AssertionError('database mutation')) as database, \
             patch('tempfile.TemporaryDirectory',side_effect=AssertionError('temporary mutation')) as temporary, \
             patch('radioinspector.serial_instruments.TinySASerialAdapter.__init__',side_effect=AssertionError('tunable device')) as tunable:
            for document in bad:
                with self.subTest(document=document['method']):
                    response=rpc.process_request(encode(document))
                    self.assertFalse(response['ok'])
                    self.assertEqual(response['operation'],'control_analysis')
                    self.assertEqual(response['error']['code'],'protocol_error')
                    self.assertNotIn('shared_io_receipt',response)
            science.assert_not_called();database.assert_not_called();temporary.assert_not_called();tunable.assert_not_called()

    def test_actual_owned_source_child_runs_five_control_methods(self):
        bootstrap=f"import sys,runpy;sys.path.insert(0,{str(ROOT)!r});runpy.run_module('radioinspector.research_rpc',run_name='__main__')"
        for case in self.cases:
            with self.subTest(method=case['method']):
                document=self.document(case)
                child=subprocess.run([sys.executable,'-I','-c',bootstrap],input=encode(document),capture_output=True,cwd=ROOT,timeout=15)
                self.assertEqual(child.returncode,0,child.stdout)
                self.assertEqual(child.stderr,b'')
                self.assertEqual(len(child.stdout.splitlines()),1)
                response=json.loads(child.stdout)
                self.assertEqual(response['operation'],'control_analysis')
                self.assertEqual(encode(response['result']),encode(case['result']))

    def test_control_private_capability_and_output_bounds_fail_closed(self):
        document=self.document()
        expected=rpc.process_request(encode(document))
        with patch.object(rpc,'MAX_RESPONSE_BYTES',len(rpc._encode(expected))-1):
            response=rpc.process_request(encode(document))
        self.assertFalse(response['ok'])
        self.assertEqual(response['operation'],'control_analysis')
        self.assertEqual(response['error']['code'],'resource_limit')
        with tempfile.TemporaryDirectory() as root:
            capability,ledger=shared_io_capability(root)
            capability['nonce']='ff'*16
            with patch('radioinspector.tinysa.measurements.analyze',side_effect=AssertionError('science before capability')) as science:
                response=rpc.process_request(encode(dict(document,shared_io=capability)))
            self.assertFalse(response['ok'])
            self.assertEqual(response['operation'],'control_analysis')
            self.assertEqual(response['error']['code'],'shared_io_unavailable')
            self.assertNotIn('shared_io_receipt',response)
            self.assertNotIn(str(ledger),json.dumps(response));self.assertNotIn(capability['nonce'],json.dumps(response))
            science.assert_not_called()


class AutomaticDetectionScienceRpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=json.loads((ROOT/'tests/fixtures/automatic-detection-science-v1.json').read_text())
        cls.cases=cls.fixture['cases']

    def document(self,case=None):
        case=case or self.cases[0]
        return {'version':1,'request_id':'detection-é-e\u0301-😀','operation':'analysis',
                'method':'automatic_detection_regions','sweep':copy.deepcopy(case['sweep']),
                'threshold_dbm':case['threshold_dbm'],'maximum':case['maximum']}

    def assert_result(self,document,expected):
        before=encode(document)
        actual=rpc.process_request(before)
        self.assertTrue(actual['ok'],actual)
        self.assertEqual(actual.keys(),{'version','request_id','operation','ok','result'})
        self.assertEqual((actual['version'],actual['operation'],actual['request_id']),
                         (1,'analysis',document['request_id']))
        self.assertEqual(encode(actual['result']),encode(expected))
        self.assertEqual(encode(document),before)
        return actual

    def test_original_fixed_detector_and_frozen_oracles_are_exact(self):
        from radioinspector.automatic_detection import detect_regions
        self.assertEqual(hashlib.sha256((ROOT/'radioinspector/automatic_detection.py').read_bytes()).hexdigest(),
                         self.fixture['oracle_source_sha256'])
        for case in self.cases:
            with self.subTest(case=case['name']):
                document=self.document(case)
                original=detect_regions(Sweep.from_dict(copy.deepcopy(case['sweep'])),
                                        threshold_dbm=case['threshold_dbm'],maximum=case['maximum'])
                self.assertEqual(encode(original),encode(case['result']))
                self.assert_result(document,original)

    def test_actual_session_service_detector_oracle_preserves_source_and_has_no_handoff(self):
        from radioinspector.automatic_detection import AutomaticCandidateDetector
        document=self.document()
        source=document['sweep']
        service=SessionService(store=None)
        detector=AutomaticCandidateDetector(service,start_background=False)
        try:
            session=service.create_session({'id':source['session_id'],'name':'Owned synthetic detection',
                'streams':[{'id':source['stream_id'],'device':'synthetic','role':'spectrum_analyzer','level_unit':'dBm'}]})
            service.start_session(session.id)
            detector.configure(session.id,{'enabled':True,'threshold_dbm':document['threshold_dbm'],
                'max_candidates_per_sweep':document['maximum'],'dedup_bandwidth_hz':1})
            service.ingest_sweep(copy.deepcopy(source))
            created=detector.run_once(session.id)
            expected=[{name:getattr(item,name) for name in ('center_frequency_hz','bandwidth_hz','peak_level','score')}
                      for item in created]
            self.assertEqual(encode(expected),encode(self.cases[0]['result']))
            self.assertEqual(service.list_investigations(session.id),[])
            before=encode(to_primitive(service.list_sweeps(session.id)))
            self.assert_result(document,expected)
            self.assertEqual(encode(to_primitive(service.list_sweeps(session.id))),before)
            self.assertEqual(service.list_investigations(session.id),[])
            self.assertEqual(encode(to_primitive(created[0].interval)),encode(source['interval']))
            self.assertEqual(created[0].source_stream_id,source['stream_id'])
        finally:
            detector.close()
            service.close()

    def test_worker_constructs_no_service_scheduler_store_or_files_and_keeps_provenance(self):
        from radioinspector.automatic_detection import detect_regions
        document=self.document(self.cases[2])
        document['sweep']['extension']={'é':-0.0,'e\u0301':1.0000000000000002}
        observed=[]
        def original(sweep,**kwargs):
            observed.append(sweep)
            return detect_regions(sweep,**kwargs)
        with patch('radioinspector.automatic_detection.detect_regions',side_effect=original), \
             patch('radioinspector.session_service.SessionService.__init__',side_effect=AssertionError('service construction')), \
             patch('radioinspector.automatic_detection.AutomaticCandidateDetector.__init__',side_effect=AssertionError('scheduler construction')), \
             patch('radioinspector.session_store.SQLiteSessionStore.__init__',side_effect=AssertionError('store construction')), \
             patch('sqlite3.connect',side_effect=AssertionError('database open')), \
             patch('builtins.open',side_effect=AssertionError('file open')):
            actual=self.assert_result(document,self.cases[2]['result'])
        self.assertEqual(len(observed),1)
        source=observed[0]
        self.assertEqual((source.id,source.session_id,source.stream_id),
                         tuple(document['sweep'][key] for key in ('id','session_id','stream_id')))
        self.assertEqual(encode(source.settings),encode(document['sweep']['settings']))
        self.assertEqual(encode(to_primitive(source.interval)),encode(document['sweep']['interval']))
        self.assertEqual([struct.pack('>d',value) for value in source.levels],
                         [struct.pack('>d',value) for value in document['sweep']['levels']])
        self.assertEqual(struct.pack('>d',actual['result'][1]['peak_level']),struct.pack('>d',-0.0))

    def test_malformed_detection_is_rejected_before_source_construction_science_or_io(self):
        bad=[]
        for key,value in [('version',True),('operation','detection'),('method','detect_regions'),('maximum',True),
                          ('maximum',0),('maximum',101),('maximum',1.0),('maximum','1'),
                          ('threshold_dbm',True),('threshold_dbm','-60'),('threshold_dbm',-301),
                          ('threshold_dbm',101),('threshold_dbm',math.inf),('threshold_dbm',math.nan),
                          ('request_id',''),('payload',{})]:
            doc=self.document();doc[key]=value;bad.append(doc)
        for key in self.document():
            doc=self.document();doc.pop(key);bad.append(doc)
        for key,value in [('id',''),('session_id',' changed '),('stream_id',False),('settings',[]),
                          ('level_unit','s_meter_count'),('frequencies_hz',[100,99]),
                          ('frequencies_hz',[True]*7),('frequencies_hz',[1.0]*7),
                          ('frequencies_hz',[-1]*7),('frequencies_hz',[2**64]*7),
                          ('frequencies_hz',[]),('levels',[-90.]*6),('levels',[True]*7),
                          ('levels',['-90']*7),('levels',[math.inf]*7)]:
            doc=self.document();doc['sweep'][key]=value;bad.append(doc)
        for interval in ({'started_at':TIME}, {'started_at':TIME,'ended_at':'2026-02-30T00:00:00Z'},
                         {'started_at':TIME,'ended_at':'2026-11-10T00:00:00Z'},
                         {'started_at':TIME,'ended_at':'2026-11-12T00:00:00+00:00'},
                         {'started_at':TIME,'ended_at':'2026-11-12T00:00:00.1234567Z'}):
            doc=self.document();doc['sweep']['interval']=interval;bad.append(doc)
        with patch('radioinspector.automatic_detection.detect_regions',side_effect=AssertionError('science')) as science, \
             patch('radioinspector.contracts.Sweep.from_dict',side_effect=AssertionError('source construction')) as constructor, \
             patch('builtins.open',side_effect=AssertionError('file open')), \
             patch('sqlite3.connect',side_effect=AssertionError('database open')):
            for document in bad:
                with self.subTest(document=document):
                    raw=json.dumps(document,ensure_ascii=True,allow_nan=True).encode()
                    response=rpc.process_request(raw)
                    self.assertFalse(response['ok'],response)
                    self.assertEqual(response['error']['code'],'protocol_error')
                    self.assertNotIn('shared_io_receipt',response)
            science.assert_not_called();constructor.assert_not_called()

    def test_private_capability_is_validated_before_science_with_zero_file_payload_receipt(self):
        document=self.document()
        with tempfile.TemporaryDirectory() as root:
            capability,ledger=shared_io_capability(root)
            response=rpc.process_request(encode(dict(document,shared_io=capability)))
            if os.name!='posix':
                self.assertEqual(response['error']['code'],'shared_io_unavailable');return
            self.assertTrue(response['ok'],response)
            self.assertEqual(response.pop('shared_io_receipt'),{'version':1,'accounted_bytes':0,'charges':0,'max_charge_bytes':0})
            self.assertEqual(encode(response),encode(self.assert_result(document,self.cases[0]['result'])))
            self.assertEqual(shared_io_total(ledger),0)
            for bad in (None,{},dict(capability,version=True),dict(capability,nonce='ff'*16)):
                with self.subTest(capability=bad), \
                     patch('radioinspector.automatic_detection.detect_regions',side_effect=AssertionError('science before capability')) as science:
                    response=rpc.process_request(encode(dict(document,shared_io=bad)))
                    self.assertFalse(response['ok'])
                    self.assertEqual(response['error']['code'],'shared_io_unavailable')
                    self.assertNotIn('shared_io_receipt',response)
                    self.assertNotIn(str(ledger),json.dumps(response));self.assertNotIn(capability['nonce'],json.dumps(response))
                    science.assert_not_called()

    def test_detection_bounds_before_science_and_receipt_cannot_expand_final_wire(self):
        from radioinspector.automatic_detection import detect_regions
        document=self.document(self.cases[1]);document['maximum']=100
        with patch.object(rpc,'MAX_TOTAL_POINTS',2), \
             patch('radioinspector.automatic_detection.detect_regions',side_effect=AssertionError('unbounded science')) as science:
            response=rpc.process_request(encode(document))
            self.assertFalse(response['ok']);self.assertEqual(response['error']['code'],'resource_limit')
            science.assert_not_called()
        expected=self.assert_result(document,detect_regions(Sweep.from_dict(document['sweep']),
            threshold_dbm=document['threshold_dbm'],maximum=document['maximum']))
        maximum=len(rpc._encode(expected))
        with tempfile.TemporaryDirectory() as root:
            capability,_=shared_io_capability(root)
            with patch.object(rpc,'MAX_RESPONSE_BYTES',maximum):
                response=rpc.process_request(encode(dict(document,shared_io=capability)))
            self.assertFalse(response['ok'])
            self.assertNotIn('shared_io_receipt',response)
            self.assertEqual(response['error']['code'],'resource_limit' if os.name=='posix' else 'shared_io_unavailable')
            self.assertLessEqual(len(rpc._encode(response)),maximum)

    def test_actual_owned_source_subprocess_all_oracles_and_eof(self):
        for case in self.cases:
            with self.subTest(case=case['name']):
                document=self.document(case)
                child=subprocess.run([sys.executable,'-m','radioinspector.research_rpc'],input=encode(document),
                    capture_output=True,cwd=ROOT,timeout=15)
                self.assertEqual(child.returncode,0,child.stderr or child.stdout)
                self.assertEqual(child.stderr,b'')
                self.assertEqual(len(child.stdout.splitlines()),1)
                response=json.loads(child.stdout)
                self.assertEqual(response['request_id'],document['request_id'])
                self.assertEqual(response['operation'],'analysis')
                self.assertEqual(encode(response['result']),encode(case['result']))


if __name__ == "__main__":
    unittest.main()
