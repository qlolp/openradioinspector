"""One-shot, memory-only scientific RPC over bounded strict JSON stdin/stdout.

The allowlist delegates to SessionService; it does not reimplement algorithms.
The caller owns archive snapshot selection, process cancellation and hard limits.
"""
from __future__ import annotations

import os

# This module imports no scientific/service code until after request validation.
# Set these before any path can import NumPy or another native numerical runtime.
for _thread_variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "ARROW_NUM_THREADS",
):
    os.environ[_thread_variable] = "1"

import json
import base64
from datetime import datetime
import math
import re
import sys
from typing import Any, BinaryIO

MAX_REQUEST_BYTES = 16 * 1024**2
MAX_RESPONSE_BYTES = 16 * 1024**2
MAX_SWEEPS = 33  # Anchor plus both legacy sixteen-neighbour context windows.
MAX_CANDIDATES = 1000
# Each pair requires at least four JSON bytes: [0,...] in each of two arrays.
# This conservative wire-derived cap does not narrow the legacy counter grid.
MAX_TOTAL_POINTS = MAX_REQUEST_BYTES // 4
METHODS = frozenset({
    "compare_sweeps", "sweep_average_comparison", "sweep_average_change_detection",
    "sweep_threshold", "sweep_three_thresholds", "sweep_context", "sweep_occupancy",
    "sweep_mask_analysis", "candidate_frequency_rounding", "frequency_rounding",
    "sweep_quality", "frequency_counter", "dvb_evaluation", "measurement_map",
})
CONTROL_METHODS = frozenset({'peak', 'occupied_bandwidth', 'bandwidth_3db', 'centroid', 'channel_power'})
DETECTION_METHOD = 'automatic_detection_regions'

# Same lowering-only execution-envelope fields as ori_research.protocol v1.
# Point analysis uses neither DuckDB nor caches; these are transport-compatible
# ceilings, not a claim of in-process enforcement of OS memory/I/O quotas.
DEFAULT_LIMITS = {
    "duckdb_threads": 2,
    "duckdb_memory_bytes": 512 * 1024**2,
    "worker_rss_bytes": 768 * 1024**2,
    "cache_bytes": 10 * 1024**3,
    "temp_bytes": 10 * 1024**3,
    "io_bytes_per_second": 20 * 1024**2,
    "batch_rows": 1024,
    "batch_bytes": 4 * 1024**2,
    "max_record_bytes": 8 * 1024**2,
    "max_points_per_sweep": 65_536,
}

_API_ERRORS = {
    "SessionNotFoundError": (404, "session_not_found"),
    "SweepNotFoundError": (404, "sweep_not_found"),
    "CandidateNotFoundError": (404, "candidate_not_found"),
    "ComparisonIncompatibleError": (409, "comparison_incompatible"),
    "AverageReferenceIncompatibleError": (409, "average_reference_incompatible"),
    "ThresholdIncompatibleError": (409, "threshold_incompatible"),
    "ThreeThresholdsIncompatibleError": (409, "three_thresholds_incompatible"),
    "ContextIncompatibleError": (409, "context_incompatible"),
    "FrequencyRoundingIncompatibleError": (409, "frequency_rounding_incompatible"),
    "QualityIncompatibleError": (409, "quality_incompatible"),
    "CounterIncompatibleError": (409, "counter_incompatible"),
    "DvbEvaluationError": (409, "dvb_evaluation_error"),
    "MeasurementMapError": (409, "measurement_map_error"),
    "MaskIncompatibleError": (409, "mask_incompatible"),
}


class ProtocolError(ValueError):
    def __init__(self, message: str, *, code: str = "protocol_error") -> None:
        super().__init__(message)
        self.code = code


class _SharedFileIo:
    """Supervisor-only file-payload reservations; transport/RAM are excluded."""
    CHUNK = 65536

    def __init__(self, capability):
        from radioinspector.worker_io import SharedIoBudget, SharedIoError
        self._error = SharedIoError
        self.bytes = self.charges = self.maximum = 0
        try:
            self._budget = SharedIoBudget(capability)
        except SharedIoError:
            raise ProtocolError('Shared cooperative I/O capability is unavailable', code='shared_io_unavailable') from None

    def account(self, size):
        if type(size) is not int or size < 0:
            raise ProtocolError('Invalid worker I/O reservation', code='shared_io_unavailable')
        while size:
            count = min(size, self.CHUNK)
            try:
                self._budget.account(count)
            except self._error:
                raise ProtocolError('Shared cooperative I/O capability is unavailable', code='shared_io_unavailable') from None
            self.bytes += count
            self.charges += 1
            self.maximum = max(self.maximum, count)
            size -= count

    def receipt(self):
        return {'version': 1, 'accounted_bytes': self.bytes,
                'charges': self.charges, 'max_charge_bytes': self.maximum}

    def close(self):
        self._budget.close()


def _shared_file_io(request):
    return _SharedFileIo(request['shared_io']) if 'shared_io' in request else None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON object key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ProtocolError("Non-finite JSON number")


def _json_values(value: Any) -> None:
    """Check UTF-8 strings and overflowed exponents without numeric coercion."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ProtocolError("Non-finite JSON number")
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field} must be a non-empty string")
    return value  # IDs, units and source metadata are never stripped or rounded.


def _unit(value: Any) -> None:
    if _text(value, "level_unit") != value.strip():
        raise ProtocolError("Raw level_unit must not require normalization")


def _record(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not fields <= value.keys():
        raise ProtocolError(f"Invalid {label} DTO")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{field} must be an array")
    return value


def _number(value: Any, field: str) -> None:
    if type(value) not in (int, float):
        raise ProtocolError(f"{field} must contain finite numbers")
    try:
        if not math.isfinite(value):
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ProtocolError(f"{field} must contain finite numbers") from exc


def _interval(record: dict[str, Any]) -> None:
    value = _record(record["interval"], {"started_at", "ended_at"}, "interval")
    _text(value["started_at"], "started_at")
    _text(value["ended_at"], "ended_at")


def _validate_limits(request):
    limits = request.get("limits", {})
    if not isinstance(limits, dict) or limits.keys() - DEFAULT_LIMITS.keys():
        raise ProtocolError("Invalid limit fields")
    for name, value in limits.items():
        if type(value) is not int or not 1 <= value <= DEFAULT_LIMITS[name]:
            raise ProtocolError("Limits must be positive integers no greater than their defaults")


def _validate_control(request):
    required = {'version', 'request_id', 'operation', 'method', 'source', 'rbw_hz'}
    if not isinstance(request, dict) or not required <= request.keys() or request.keys()-required-{'limits', 'shared_io'}:
        raise ProtocolError('Invalid control analysis envelope fields')
    if type(request['version']) is not int or request['version'] != 1 or request['operation'] != 'control_analysis':
        raise ProtocolError('Unsupported control analysis protocol')
    if not _valid_request_id(request['request_id']):
        raise ProtocolError('request_id must contain 1-128 UTF-8 characters')
    if not isinstance(request['method'], str) or request['method'] not in CONTROL_METHODS:
        raise ProtocolError('Control analysis method is not allowlisted')
    _validate_limits(request)
    _number(request['rbw_hz'], 'rbw_hz')
    if request['rbw_hz'] <= 0: raise ProtocolError('rbw_hz must be positive')
    source = request['source']
    fields = {'frequencies_hz', 'levels', 'level_unit', 'wire_base64', 'zero_db', 'started_at', 'ended_at',
              'sample_offsets_seconds', 'timing_source', 'grid_source', 'requested_stop_hz', 'stop_is_inclusive'}
    if not isinstance(source, dict) or set(source) != fields:
        raise ProtocolError('Invalid owned raw trace fields')
    hz, levels = source['frequencies_hz'], source['levels']
    if not isinstance(hz, list) or not isinstance(levels, list) or not 2 <= len(hz) <= 65536 or len(hz) != len(levels):
        raise ProtocolError('Control source dimensions must match with 2-65536 points')
    if any(type(value) is not int or not 0 <= value < 2**64 for value in hz) or any(a >= b for a, b in zip(hz, hz[1:])):
        raise ProtocolError('Control source frequencies must be increasing integer Hz')
    for value in levels: _number(value, 'levels')
    _number(source['zero_db'], 'zero_db')
    if source['level_unit'] != 'dBm' or source['sample_offsets_seconds'] is not None or source['timing_source'] != 'interval_only' or source['grid_source'] != 'firmware_ea71dfd_scanraw':
        raise ProtocolError('Unsupported raw trace unit, timing or grid context')
    stop = source['requested_stop_hz']
    if stop is not None and (type(stop) is not int or not 0 <= stop < 2**64):
        raise ProtocolError('Invalid requested stop context')
    if source['stop_is_inclusive'] is not False and source['stop_is_inclusive'] is not None:
        raise ProtocolError('Unsupported stop-grid context')
    dates = []
    for key in ('started_at', 'ended_at'):
        value = source[key]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
            raise ProtocolError('Raw trace interval must contain UTC microsecond timestamps')
        try: dates.append(datetime.fromisoformat(value))
        except ValueError: raise ProtocolError('Invalid raw trace UTC interval') from None
    if dates[1] < dates[0]: raise ProtocolError('Raw trace interval is reversed')
    wire = source['wire_base64']
    if not isinstance(wire, str) or not wire:
        raise ProtocolError('Raw trace wire evidence must be base64')
    try: base64.b64decode(wire, validate=True)
    except (ValueError, UnicodeError): raise ProtocolError('Raw trace wire evidence must be base64') from None


def _control_analysis(request):
    # The controller's original host-only function owns all scientific math.
    # This path constructs no controller, adapter, store, worker or artifact.
    from radioinspector.tinysa.measurements import analyze
    return analyze(request['source'], request['method'], rbw_hz=request['rbw_hz'])


def _validate_detection(request):
    required = {'version', 'request_id', 'operation', 'method', 'sweep', 'threshold_dbm', 'maximum'}
    if not isinstance(request, dict) or not required <= request.keys() or request.keys()-required-{'limits', 'shared_io'}:
        raise ProtocolError('Invalid automatic detection envelope fields')
    if type(request['version']) is not int or request['version'] != 1 or request['operation'] != 'analysis' or request['method'] != DETECTION_METHOD:
        raise ProtocolError('Unsupported automatic detection protocol')
    if not _valid_request_id(request['request_id']):
        raise ProtocolError('request_id must contain 1-128 UTF-8 characters')
    _validate_limits(request)
    _number(request['threshold_dbm'], 'threshold_dbm')
    if not -300 <= request['threshold_dbm'] <= 100:
        raise ProtocolError('threshold_dbm must be in -300..100')
    if type(request['maximum']) is not int or not 1 <= request['maximum'] <= 100:
        raise ProtocolError('maximum must be an integer in 1..100')
    sweep = _record(request['sweep'], {'id', 'session_id', 'stream_id', 'interval', 'frequencies_hz', 'levels', 'level_unit'}, 'detection sweep')
    _text(sweep['id'], 'sweep.id')
    for key in ('session_id', 'stream_id'):
        value = _text(sweep[key], 'sweep.'+key)
        if value != value.strip():
            raise ProtocolError('Source ownership must not require normalization')
    if sweep['level_unit'] != 'dBm':
        raise ProtocolError('Automatic detection requires original dBm')
    if not isinstance(sweep.get('settings', {}), dict):
        raise ProtocolError('sweep.settings must be an object')
    hz, levels = _array(sweep['frequencies_hz'], 'frequencies_hz'), _array(sweep['levels'], 'levels')
    if not hz or len(hz) != len(levels):
        raise ProtocolError('Source frequency/level dimensions must match and be non-empty')
    if len(hz) > MAX_TOTAL_POINTS:
        raise ProtocolError('Source point budget exceeded', code='resource_limit')
    if any(type(value) is not int or not 0 <= value < 2**64 for value in hz) or any(a > b for a, b in zip(hz, hz[1:])):
        raise ProtocolError('Detection frequencies must be nondecreasing integer Hz')
    for value in levels:
        _number(value, 'levels')
    interval = _record(sweep['interval'], {'started_at', 'ended_at'}, 'detection interval')
    dates = []
    for key in ('started_at', 'ended_at'):
        value = interval[key]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
            raise ProtocolError('Detection interval must contain UTC microsecond timestamps')
        try:
            dates.append(datetime.fromisoformat(value))
        except ValueError:
            raise ProtocolError('Invalid detection UTC interval') from None
    if dates[1] < dates[0]:
        raise ProtocolError('Detection interval is reversed')


def _detection_analysis(request):
    from radioinspector.automatic_detection import detect_regions
    from radioinspector.contracts import Sweep
    return detect_regions(Sweep.from_dict(request['sweep']),
                          threshold_dbm=request['threshold_dbm'], maximum=request['maximum'])


def _validate(request: Any) -> None:
    required = {"version", "request_id", "operation", "method", "session", "sweeps", "candidates", "payload"}
    if not isinstance(request, dict) or not required <= request.keys() or request.keys() - required - {"limits", "session_id", "shared_io"}:
        raise ProtocolError("Invalid analysis envelope fields")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ProtocolError("Unsupported RPC version")
    if not _valid_request_id(request["request_id"]):
        raise ProtocolError("request_id must contain 1-128 UTF-8 characters")
    if request["operation"] != "analysis":
        raise ProtocolError("Only the analysis operation is supported")
    if not isinstance(request["method"], str) or request["method"] not in METHODS:
        raise ProtocolError("Analysis method is not allowlisted")
    if not isinstance(request["payload"], dict):
        raise ProtocolError("payload must be an object")
    _validate_limits(request)

    if request["session"] is None:
        # Preserve the actual method's validation-before-lookup semantics when
        # the parent's consistent archive snapshot contains no such session.
        # A missing session can never carry data from a different scope.
        _text(request.get("session_id"), "session_id")
        if request["sweeps"] != [] or request["candidates"] != []:
            raise ProtocolError("A missing session must not contain source DTOs")
        return
    if "session_id" in request:
        raise ProtocolError("A full session DTO cannot contain a second scope identity")

    session = _record(request["session"], {"id", "name", "created_at", "streams"}, "session")
    sid = _text(session["id"], "session.id")
    _text(session["name"], "session.name")
    _text(session["created_at"], "session.created_at")
    if "contract_version" in session and (
        type(session["contract_version"]) is not int or session["contract_version"] != 1
    ):
        raise ProtocolError("Invalid session contract_version")
    if not isinstance(session.get("metadata", {}), dict):
        raise ProtocolError("session.metadata must be an object")
    streams: set[str] = set()
    for stream in _array(session["streams"], "streams"):
        _record(stream, {"id", "session_id", "device", "role", "level_unit"}, "stream")
        identity = _text(stream["id"], "stream.id")
        if identity in streams or stream["session_id"] != sid:
            raise ProtocolError("Duplicate or foreign stream identity")
        streams.add(identity)
        _text(stream["device"], "stream.device")
        _unit(stream["level_unit"])
        if not isinstance(stream.get("settings", {}), dict):
            raise ProtocolError("stream.settings must be an object")

    sweeps = _array(request["sweeps"], "sweeps")
    candidates = _array(request["candidates"], "candidates")
    if len(sweeps) > MAX_SWEEPS or len(candidates) > MAX_CANDIDATES:
        raise ProtocolError("Source count limit exceeded", code="resource_limit")
    total_points = 0
    for items, kind, stream_key, fields in (
        (sweeps, "sweep", "stream_id", {"frequencies_hz", "levels", "settings"}),
        (candidates, "candidate", "source_stream_id", {"center_frequency_hz"}),
    ):
        identities: set[str] = set()
        for item in items:
            _record(item, {"id", "session_id", stream_key, "interval", "level_unit"} | fields, kind)
            identity = _text(item["id"], f"{kind}.id")
            source_stream = _text(item[stream_key], f"{kind}.{stream_key}")
            if identity in identities or item["session_id"] != sid or source_stream not in streams:
                raise ProtocolError(f"Duplicate or foreign {kind} identity")
            identities.add(identity)
            _unit(item["level_unit"])
            _interval(item)
            if kind == "sweep":
                frequencies = _array(item["frequencies_hz"], "frequencies_hz")
                levels = _array(item["levels"], "levels")
                if not frequencies or len(frequencies) != len(levels):
                    raise ProtocolError("Source frequency/level dimensions must match and be non-empty")
                total_points += len(frequencies)
                if total_points > MAX_TOTAL_POINTS:
                    raise ProtocolError("Source point budget exceeded", code="resource_limit")
                if any(type(hz) is not int for hz in frequencies):
                    raise ProtocolError("Source frequencies must be integer Hz")
                for level in levels:
                    _number(level, "levels")
                if not isinstance(item["settings"], dict):
                    raise ProtocolError("sweep.settings must be an object")
            else:
                for field in ("center_frequency_hz", "bandwidth_hz"):
                    if item.get(field) is not None and type(item[field]) is not int:
                        raise ProtocolError(f"candidate.{field} must be integer Hz")
                for field in ("peak_level", "score"):
                    if item.get(field) is not None:
                        _number(item[field], f"candidate.{field}")
                references = _array(item.get("observation_ids", []), "observation_ids")
                if len(references) > 1000:
                    raise ProtocolError("Candidate source reference limit exceeded", code="resource_limit")
                for reference in references:
                    _text(reference, "observation_id")


def _analysis(request: dict[str, Any]) -> Any:
    # Imports are deliberately lazy at this boundary. The legacy package's
    # __init__ can load NumPy before -m starts; see the pre-spawn env contract.
    # No archive constructor, recovery, workers or ports run.
    from radioinspector.contracts import Candidate, MonitoringSession, Sweep, to_primitive
    from radioinspector.session_service import SessionService

    try:
        session = MonitoringSession.from_dict(request["session"]) if request["session"] is not None else None
        sweeps = [Sweep.from_dict(item) for item in request["sweeps"]]
        candidates = [Candidate.from_dict(item) for item in request["candidates"]]
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ProtocolError("Invalid source DTO") from exc
    # The legacy constructors strip scope fields. Reject a DTO if that would
    # silently change ownership; exact source IDs themselves remain untouched.
    for prepared, raw in zip(sweeps, request["sweeps"]):
        if prepared.session_id != raw["session_id"] or prepared.stream_id != raw["stream_id"]:
            raise ProtocolError("Source ownership must not require normalization")
    for prepared, raw in zip(candidates, request["candidates"]):
        if prepared.session_id != raw["session_id"] or prepared.source_stream_id != raw["source_stream_id"]:
            raise ProtocolError("Source ownership must not require normalization")
    service = SessionService(store=None)
    # This one-shot service is private and has no subscribers. Seed existing
    # immutable source objects directly: ingest/create would emit new events.
    if session is not None:
        service._sessions[session.id] = session
    service._sweeps = sweeps
    service._candidates = {item.id: item for item in candidates}
    sid, payload = session.id if session is not None else request["session_id"], request["payload"]
    # Static bound-method table: user text is never passed to getattr/import/eval.
    dispatch = {
        "compare_sweeps": lambda: service.compare_sweeps(sid, **payload),
        "sweep_average_comparison": lambda: service.sweep_average_comparison(sid, payload),
        "sweep_average_change_detection": lambda: service.sweep_average_change_detection(sid, payload),
        "sweep_threshold": lambda: service.sweep_threshold(sid, **payload),
        "sweep_three_thresholds": lambda: service.sweep_three_thresholds(sid, payload),
        "sweep_context": lambda: service.sweep_context(sid, payload),
        "sweep_occupancy": lambda: service.sweep_occupancy(sid, payload),
        "sweep_mask_analysis": lambda: service.sweep_mask_analysis(sid, payload),
        "candidate_frequency_rounding": lambda: service.candidate_frequency_rounding(sid, payload),
        "frequency_rounding": lambda: service.frequency_rounding(sid, payload),
        "sweep_quality": lambda: service.sweep_quality(sid, payload),
        "frequency_counter": lambda: service.frequency_counter(sid, payload),
        "dvb_evaluation": lambda: service.dvb_evaluation(sid, payload),
        "measurement_map": lambda: service.measurement_map(sid, payload),
    }
    return to_primitive(dispatch[request["method"]]())


def _valid_request_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return False
    try:
        value.encode("utf-8", errors="strict")
        return True
    except UnicodeError:
        return False


def _failure(request_id: str | None, error: Exception, *, operation='analysis') -> dict[str, Any]:
    name, message = type(error).__name__, str(error)
    status, api_code = _API_ERRORS.get(name, (400, "invalid_request"))
    code = "not_found" if status == 404 else "incompatible" if status == 409 else "validation_error"
    if isinstance(error, ProtocolError):
        code = error.code
        status, api_code = (413 if code == "resource_limit" else 503 if code == "shared_io_unavailable" else 400), code
    elif not isinstance(error, (ValueError, TypeError, KeyError, ArithmeticError)):
        code, name, message, status, api_code = "internal_error", "InternalError", "Analysis worker failed", 500, "internal_error"
    api_message = str(error.args[0]) if status == 404 else message
    if isinstance(error, KeyError) and status == 400:
        api_code, api_message = "missing_field", f"Отсутствует поле: {error.args[0]}"
    return {"version": 1, "request_id": request_id, "operation": operation, "ok": False,
            "error": {"code": code, "type": name, "message": message,
                      "api_code": api_code, "http_status": status,
                      "api_message": api_message}}


def _encode(response: dict[str, Any]) -> bytes:
    return json.dumps(response, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def _bounded(response: dict[str, Any]) -> dict[str, Any]:
    try:
        if len(_encode(response)) <= MAX_RESPONSE_BYTES:
            return response
    except (ValueError, OverflowError, UnicodeError, RecursionError):
        return _failure(response["request_id"], ProtocolError("Result is not finite UTF-8 JSON"), operation=response['operation'])
    return _failure(response["request_id"], ProtocolError("Response byte limit exceeded", code="resource_limit"), operation=response['operation'])


def process_request(raw: bytes) -> dict[str, Any]:
    """Return one bounded result/error envelope, including for malformed EOF."""
    request_id = None
    operation = 'analysis'
    detection = False
    shared_io = None
    try:
        if not isinstance(raw, bytes):
            raise ProtocolError("Request must be UTF-8 bytes")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ProtocolError("Request byte limit exceeded", code="resource_limit")
        try:
            request = json.loads(raw.decode("utf-8", errors="strict"),
                                 object_pairs_hook=_pairs, parse_constant=_constant)
            _json_values(request)
        except (ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("Request must be one strict UTF-8 JSON object") from exc
        if isinstance(request, dict) and _valid_request_id(request.get("request_id")):
            request_id = request["request_id"]
        if isinstance(request, dict) and request.get('operation') == 'control_analysis':
            operation = 'control_analysis'
            _validate_control(request)
        elif isinstance(request, dict) and request.get('method') == DETECTION_METHOD:
            _validate_detection(request)
            detection = True
        else:
            _validate(request)
        shared_io = _shared_file_io(request)
        result = (_control_analysis(request) if operation == 'control_analysis'
                  else _detection_analysis(request) if detection else _analysis(request))
        response = {"version": 1, "request_id": request_id, "operation": operation, "ok": True, "result": result}
        if shared_io is not None:
            response['shared_io_receipt'] = shared_io.receipt()
    except Exception as exc:
        response = _failure(request_id, exc, operation=operation)
    finally:
        if shared_io is not None:
            shared_io.close()
    return _bounded(response)


def run(source: BinaryIO, destination: BinaryIO) -> int:
    """Read until EOF or the first excess byte; publish no partial response."""
    pieces, length = [], 0
    try:
        while length <= MAX_REQUEST_BYTES:
            piece = source.read(min(65536, MAX_REQUEST_BYTES + 1 - length))
            if not piece:
                break
            pieces.append(piece)
            length += len(piece)
        response = process_request(b"".join(pieces))
    except (OSError, ValueError):
        response = _failure(None, ProtocolError("Unable to read request stream"))
    encoded = _encode(response)
    try:
        destination.write(encoded)
        destination.flush()
    except (BrokenPipeError, OSError):
        return 1  # Caller cancelled; do not attempt another stdout envelope.
    return 0 if response["ok"] else 1


def main() -> int:
    return run(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
