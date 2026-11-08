"""DVB-T parameter evaluation: verdicts apart from values, honest scope."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.dvb_parameters import (
    CODE_RATES, DVB_METHODOLOGY, GUARD_INTERVALS, PARAMETER_FIELDS, THRESHOLD_FIELDS,
    DvbEvaluationError, evaluate_dvb_parameters, validate_dvb_request,
)
from radioinspector.measurement_results import METHODOLOGIES
from radioinspector.session_service import SessionNotFoundError, SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session


def body(**changes):
    request = {
        "parameters": {"mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0},
        "thresholds": {"min_mer_db": 15.0, "max_ber_after_viterbi": 1e-3, "max_ber_after_rs": 1e-9},
        "guard_interval": "1/4", "code_rate": "2/3",
    }
    request.update(changes)
    return request


class DvbContractTests(unittest.TestCase):
    def test_exact_shape_fields_rates_and_ranges(self):
        invalid = [None, [], True, "x", {}, dict(body(), extra=1)]
        for field in body():
            request = body()
            del request[field]
            invalid.append(request)
        for value in (None, 1, "1/3", "", "3/8"):
            invalid.append(body(guard_interval=value))
        for value in (None, 0, "4/5", "", "7/9"):
            invalid.append(body(code_rate=value))
        invalid += [body(parameters=value) for value in (None, {}, {"mer_db": 1.0},
                    dict({f: 1.0 for f in PARAMETER_FIELDS}, extra=1.0))]
        invalid += [body(thresholds=value) for value in (None, {}, {"min_mer_db": 1.0},
                    dict({f: 1.0 for f in THRESHOLD_FIELDS}, extra=1.0))]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises((ValueError, TypeError, DvbEvaluationError)):
                validate_dvb_request(request)
        parsed = validate_dvb_request(body())
        self.assertEqual(set(parsed), {"parameters", "thresholds", "guard_interval", "code_rate"})
        self.assertEqual(parsed["guard_interval"], "1/4")

    def test_oversized_numeric_values_are_validation_errors(self):
        for key in PARAMETER_FIELDS:
            invalid = body(); invalid['parameters'][key] = 10**309
            with self.subTest(key=key), self.assertRaises(DvbEvaluationError):
                validate_dvb_request(invalid)

    def test_ber_ranges_and_nonfinite_rejected(self):
        for field, value in (("ber_after_viterbi", 1.5), ("ber_after_viterbi", -0.1),
                             ("ber_after_rs", 2.0)):
            with self.subTest(field=field), self.assertRaises(DvbEvaluationError):
                validate_dvb_request(body(parameters=dict(body()["parameters"], **{field: value})))
        for field in PARAMETER_FIELDS:
            for value in (float("nan"), float("inf"), True, "1"):
                with self.subTest(field=field, value=value), self.assertRaises(DvbEvaluationError):
                    validate_dvb_request(body(parameters=dict(body()["parameters"], **{field: value})))
        for field in THRESHOLD_FIELDS:
            with self.subTest(field=field), self.assertRaises(DvbEvaluationError):
                validate_dvb_request(body(thresholds=dict(body()["thresholds"], **{field: float("nan")})))
        with self.assertRaises(DvbEvaluationError):
            validate_dvb_request(body(thresholds=dict(body()["thresholds"], max_ber_after_rs=1.5)))

    def test_verdicts_and_result_code(self):
        result = evaluate_dvb_parameters(body())
        self.assertEqual(result["result_code"], "norm")
        self.assertEqual(result["verdicts"], {"mer_db": True, "ber_after_viterbi": True, "ber_after_rs": True})
        self.assertEqual(result["methodology"], DVB_METHODOLOGY)
        values = result["values"]
        self.assertEqual(set(values), set(METHODOLOGIES[DVB_METHODOLOGY]["fields"]))
        self.assertEqual(values["guard_interval_ratio"], 0.25)
        self.assertAlmostEqual(values["code_rate_ratio"], 2 / 3)
        # Ties pass: >= for MER, <= for BER.
        tied = evaluate_dvb_parameters(body(
            parameters={"mer_db": 15.0, "ber_after_viterbi": 1e-3, "ber_after_rs": 1e-9}))
        self.assertEqual(tied["result_code"], "norm")
        # Each parameter breach is caught.
        for parameters in ({"mer_db": 14.9, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0},
                           {"mer_db": 20.0, "ber_after_viterbi": 2e-3, "ber_after_rs": 0.0},
                           {"mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 1e-8}):
            with self.subTest(parameters=parameters):
                result = evaluate_dvb_parameters(body(parameters=parameters))
                self.assertEqual(result["result_code"], "not_norm")
                self.assertIn(False, result["verdicts"].values())
        for guard, code in (("1/32", "1/2"), ("1/16", "7/8"), ("1/8", "3/4")):
            result = evaluate_dvb_parameters(body(guard_interval=guard, code_rate=code))
            self.assertEqual(result["values"]["guard_interval_ratio"],
                             int(guard.split("/")[0]) / int(guard.split("/")[1]))
        self.assertEqual(len(GUARD_INTERVALS), 4)
        self.assertEqual(len(CODE_RATES), 5)


class DvbServiceMixin:
    def test_evaluates_without_writes_and_binds_session(self):
        result = self.service.dvb_evaluation(self.session.id, body())
        self.assertEqual(result["session_id"], self.session.id)
        self.assertEqual(result["result_code"], "norm")
        before = (self.service.health(), self.service.events())
        self.service.dvb_evaluation(self.session.id, body())
        self.assertEqual((self.service.health(), self.service.events()), before)

    def test_missing_session(self):
        with self.assertRaises(SessionNotFoundError):
            self.service.dvb_evaluation("no-such-session", body())

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["dvb_t_evaluation"])


class MemoryDvbTests(DvbServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteDvbTests(DvbServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class DvbHTTPTests(unittest.TestCase):
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
        self.root = f"/api/v1/sessions/{self.session.id}/dvb-evaluation"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload, *, path=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path or self.root, body=json.dumps(payload).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_evaluates_over_http(self):
        status, envelope = self.request(body())
        self.assertEqual(status, 200)
        data = envelope["data"]
        self.assertEqual(data["result_code"], "norm")
        self.assertEqual(data["session_id"], self.session.id)
        self.assertEqual(data["methodology"], DVB_METHODOLOGY)

    def test_error_statuses(self):
        cases = [
            (body(guard_interval="1/3"), 400, "invalid_request"),
            (body(parameters={"mer_db": 1.0, "ber_after_viterbi": 2.0, "ber_after_rs": 0.0}),
             409, "dvb_evaluation_error"),
            ({}, 400, "invalid_request"),
        ]
        for payload, status, error in cases:
            with self.subTest(payload=payload):
                code, envelope = self.request(payload)
                self.assertEqual(code, status)
                self.assertEqual(envelope["error"]["code"], error)
        code, envelope = self.request(body(),
                                      path="/api/v1/sessions/no-such-session/dvb-evaluation")
        self.assertEqual(code, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_query_parameters_rejected(self):
        code, envelope = self.request(body(), path=self.root + "?x=1")
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
        self.assertTrue(envelope["data"]["features"]["dvb_t_evaluation"])


if __name__ == "__main__":
    unittest.main()
