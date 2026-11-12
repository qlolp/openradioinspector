"""Bounded real-HTTP compatibility gate. An unavailable route never passes.

Only disposable loopback nodes and synthetic fixtures are used. Source/spec
inventories describe coverage; actual producer responses are the oracle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import argparse
import copy
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlsplit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_DIFFERENCES = 32


class ProtocolError(ValueError):
    pass


def load_json(data: bytes, maximum: int = MAX_RESPONSE_BYTES) -> Any:
    if len(data) > maximum:
        raise ProtocolError("JSON response byte budget exceeded")

    def fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("Duplicate exact UTF-8 JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ProtocolError("Nonfinite JSON constant")

    def finite(token):
        value=float(token)
        if not math.isfinite(value):
            raise ProtocolError("Nonfinite JSON number")
        return value

    def utf8(value,depth=0):
        if depth>128:raise ProtocolError("JSON nesting budget exceeded")
        if isinstance(value,str):value.encode("utf-8",errors="strict")
        elif isinstance(value,dict):
            for key,item in value.items():
                key.encode("utf-8",errors="strict")
                utf8(item,depth+1)
        elif isinstance(value,list):
            for item in value:utf8(item,depth+1)

    try:
        value=json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=fields,
                          parse_constant=constant,parse_float=finite,
                          parse_int=lambda token:-0.0 if token=="-0" else int(token))
        utf8(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProtocolError("Invalid bounded UTF-8 JSON") from error


_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")


def _utc(value):
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def _pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block:=stream.read(1024*1024):digest.update(block)
    return digest.hexdigest()


def _matches(pattern, path, arrays):
    expected, actual = pattern.split("/"), path.split("/")
    return len(expected) == len(actual) and all(a == b or a == "*" and "/".join(actual[:index]) in arrays
                                               for index,(a,b) in enumerate(zip(expected,actual)))


def compare_value(left, right, *, volatile_paths=()) -> list[str]:
    """Exact IDs/keys/integers/f64 bits. Only declared lifecycle clocks vary."""
    differences = []

    def visit(a, b, path, depth, arrays=frozenset()):
        if len(differences) >= MAX_DIFFERENCES:
            return
        if depth > 128:
            differences.append(path + ": comparison depth exceeded")
            return
        if any(_matches(pattern, path, arrays) for pattern in volatile_paths):
            if a is None and b is None:
                return
            if not _utc(a) or not _utc(b):
                differences.append(path + ": invalid declared lifecycle UTC microseconds")
            return
        if type(a) is not type(b):
            differences.append(path + ": JSON value type differs")
        elif isinstance(a, float):
            if not math.isfinite(a) or not math.isfinite(b) or struct.pack(">d", a) != struct.pack(">d", b):
                differences.append(path + ": float64 bits differ")
        elif isinstance(a, dict):
            if a.keys() != b.keys():
                differences.append(path + ": exact UTF-8 object keys differ")
            for key in sorted(a.keys() & b.keys(), key=lambda s: s.encode("utf-8")):
                visit(a[key], b[key], path + "/" + _pointer(key), depth + 1, arrays)
        elif isinstance(a, list):
            if len(a) != len(b):
                differences.append(path + ": array length differs")
            for index, (first, second) in enumerate(zip(a, b)):
                visit(first, second, path + "/" + str(index), depth + 1, arrays|{path})
        elif a != b:
            differences.append(path + ": value differs")

    visit(left, right, "", 0)
    return differences


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return load_json(self.body)


def load_ndjson(response):
    if not response.body or not response.body.endswith(b"\n"):
        raise ProtocolError("NDJSON export ended without a complete final record")
    lines=response.body.splitlines()
    if len(lines)>1000:raise ProtocolError("NDJSON fixture record budget exceeded")
    records=[load_json(line) for line in lines]
    if not isinstance(records[0],dict):raise ProtocolError("NDJSON header must be an object")
    for item in records[1:]:
        if not isinstance(item,dict) or item.get("kind") not in ("sweep","observation") or not isinstance(item.get("measurement"),dict):
            raise ProtocolError("Invalid NDJSON source measurement record")
    for kind,name in (("sweep","x-archive-sweeps"),("observation","x-archive-observations")):
        raw=response.headers.get(name,"")
        if not raw.isascii() or not raw.isdigit() or int(raw)!=sum(item.get("kind")==kind for item in records[1:]):
            raise ProtocolError("NDJSON committed snapshot counts differ from complete records")
    return records


@dataclass
class Outcome:
    state: str
    details: list[str] = field(default_factory=list)
    validated_extensions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def successful(self):
        return self.state == "pass"


def _capacity_layout(value, *, logical_bytes=None):
    """Validate a node's capacity policy before comparing independent layouts."""
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise ProtocolError("Capacity envelope is missing")
    data = value["data"]
    if "max_database_bytes" not in data:
        raise ProtocolError("SQLite maximum capacity is missing")
    size, maximum = data.get("database_bytes"), data["max_database_bytes"]
    if type(size) is not int or size <= 0 or maximum is not None and (type(maximum) is not int or maximum <= 0):
        raise ProtocolError("Invalid logical SQLite capacity")
    if logical_bytes is not None and size != logical_bytes:
        raise ProtocolError("Capacity differs from owned SQLite page_count times page_size")
    remaining = None if maximum is None else max(0, maximum-size)
    state = ("unlimited" if maximum is None else "full" if size >= maximum
             else "warning" if size*10 >= maximum*9 else "ok")
    expected = {"storage_mode":"sqlite", "remaining_bytes":remaining, "state":state,
                "warning_threshold":0.9, "measurement_writes_blocked":state == "full",
                "automatic_deletion":False}
    for key, wanted in expected.items():
        if key not in data or compare_value(data[key], wanted):
            raise ProtocolError("Invalid SQLite capacity policy: "+key)
    return dict(value, data=dict(data, database_bytes="<verified-independent-layout>",
                                remaining_bytes="<verified-independent-layout>"))


def _background_qos(value):
    flags={'acquisition_pressure','cross_process_logical_io','cleanup_failed'}
    if (not isinstance(value,dict)
            or set(value)!=flags|{'registered_work','logical_bytes','logical_bytes_per_second','physical_io_enforced'}):
        return False
    return (all(type(value[key]) is bool for key in flags)
            and type(value['registered_work']) is int and 0<=value['registered_work']<=8
            and type(value['logical_bytes']) is int and 0<=value['logical_bytes']<=2**64-1
            and type(value['logical_bytes_per_second']) is int
            and value['logical_bytes_per_second']==20*1024*1024
            and value['physical_io_enforced'] is False)


def _acquisition_pressure(value):
    flags={'observer_running','acquisition_pressure'}
    counters={'active_analyzers','calibrating_analyzers','delayed_analyzers'}
    if (not isinstance(value,dict) or set(value)!=flags|counters|{'last_error'}
            or any(type(value[key]) is not bool for key in flags)
            or any(type(value[key]) is not int or not 0<=value[key]<=32 for key in counters)
            or any(value[key]>value['active_analyzers'] for key in counters-{'active_analyzers'})):
        return False
    error=value['last_error']
    return error is None or (isinstance(error,str) and 0<len(error)<=256
                             and all(character.isprintable() for character in error))


def _candidate_metadata(value):
    required={"mode", "compatibility_complete", "release_accepted", "hardware_validation",
              "unsupported_features", "resource_limits"}
    optional_flags={"legacy_aggregate_background_io", "history_worker_configured", "native_connector_enabled"}
    optional={'aggregate_background_io_complete','background_qos','raw_export_limits','acquisition_pressure'}
    if not isinstance(value,dict) or required-set(value) or set(value)-required-optional_flags-optional:
        return False
    if (value["mode"] not in ("shadow","replay")
            or type(value["compatibility_complete"]) is not bool
            or value["release_accepted"] is not False or value["hardware_validation"] != "not_run"):
        return False
    if ('aggregate_background_io_complete' in value
            and value['aggregate_background_io_complete'] is not False):
        return False
    if 'background_qos' in value and not _background_qos(value['background_qos']):
        return False
    if 'acquisition_pressure' in value and not _acquisition_pressure(value['acquisition_pressure']):
        return False
    if 'raw_export_limits' in value and compare_value(value['raw_export_limits'],{
            'live_readers':2,'max_record_bytes':16*1024*1024,'read_timeout_ms':5000,
            'whole_database_copy':False,'heavy_research_permit':False,
            'native_source_io_accounted':False}):
        return False
    features=value["unsupported_features"]
    if (not isinstance(features,list) or len(features)>32
            or any(not isinstance(key,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,79}",key) is None for key in features)
            or len(set(features)) != len(features)):
        return False
    limits=value["resource_limits"]
    boolean_limits={"native_connector_enabled", "measured_rss_gate_passed"}
    if (not isinstance(limits,dict) or not limits or len(limits)>64
            or any(re.fullmatch(r"[a-z][a-z0-9_]{0,79}",key) is None
                   or not (type(item) is bool if key in boolean_limits
                           else type(item) is int and 0<=item<=2**40)
                   for key,item in limits.items())):
        return False
    if ('native_connector_enabled' in value
            and (type(limits.get('native_connector_enabled')) is not bool
                 or limits['native_connector_enabled'] is not value['native_connector_enabled'])):
        return False
    return all(type(value[key]) is bool for key in optional_flags if key in value)


def _diagnostic_extensions(value, operation, *, reference):
    if (not isinstance(value, dict) or not isinstance(value.get("data"), dict)
            or not isinstance(reference, dict) or not isinstance(reference.get("data"), dict)):
        return value
    data = value["data"]
    existing = reference["data"]
    removable = set()
    if (operation == "GET /api/v1/health" and "backend" not in existing
            and data.get("backend") == "rust-candidate"):
        removable.add("backend")
    if (operation == "GET /api/v1/task-scheduler" and "execution_available" not in existing
            and type(data.get("execution_available")) is bool):
        removable.add("execution_available")
    projected={key:item for key,item in data.items() if key not in removable}
    if operation == "GET /api/v1/capabilities":
        # Capability negotiation permits new feature names. Existing feature
        # values, missing features and all other public response data stay exact.
        if isinstance(existing.get("features"),dict) and isinstance(data.get("features"),dict):
            projected["features"]={key:item for key,item in data["features"].items()
                                   if key in existing["features"]}
        if ("storage_mode" not in existing and data.get("storage_mode") in ("sqlite","memory","shadow")
                and data["storage_mode"] == existing.get("session_storage")):
            projected.pop("storage_mode")
        if "candidate" not in existing and _candidate_metadata(data.get("candidate")):
            projected.pop("candidate")
    return dict(value, data=projected)


def _reviewed_openapi_extension(reference, candidate):
    """Prove the entire additive document, without masking common operations."""
    from tools.contracts.runtime_contract import compose_runtime_document
    root = Path(__file__).resolve().parents[2]
    baseline = load_json((root/'contracts/generated/openapi-v1.json').read_bytes())
    runtime = load_json((root/'contracts/generated/runtime-openapi-v1.json').read_bytes())
    extension = load_json((root/'contracts/runtime-extension-v1.json').read_bytes())
    try:
        composed = compose_runtime_document(baseline, extension)
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError('Invalid reviewed runtime OpenAPI composition') from error
    # The composer rejects replacements; exact whole-document comparisons also
    # cover descriptions, response shapes, security and support annotations.
    return (not compare_value(runtime, composed)
            and not compare_value(reference, baseline)
            and not compare_value(candidate, runtime))


_ASYNC_CONTROL_OPERATION='tinySA typed202/poll/idempotency/conflict/cancel'
_ASYNC_CONTROL_AVAILABLE={name:True for name in (
    'inspect','acquire.raw','acquire.zero','trace','marker','measurement','sd.list','sd.read','sd.delete',
    'preset.save','preset.load','preset.export','preset.import','service.guide','selftest.start',
    'generator.sweep','output.off')}
_ASYNC_CONTROL_AVAILABLE.update({'audio.inputs':False,'audio.capture':False})


def _collected_control_extensions(reference,candidate):
    """Only this fixed owned, input-only/no-audio protocol fixture may enrich v1.

    Shared fields never project. Accepted additions keep their exact raw values
    in the response and report; no endpoint or opaque measurement is traversed.
    """
    def at(body,path):
        try:
            for key in path:body=body[key]
        except (KeyError,TypeError,IndexError) as error:
            raise ProtocolError('Malformed collected Control fixture shape') from error
        return body

    expected_keys={'capabilities','terminal_records','conflicting_request','raw_rejected'}
    for body in (reference,candidate):
        if not isinstance(body,dict) or set(body)!=expected_keys:
            raise ProtocolError('Invalid collected Control fixture envelope')
        records=body['terminal_records']
        envelope=body['capabilities']
        if (not isinstance(envelope,dict) or envelope.get('contract')!='openradioinspector-platform'
                or type(envelope.get('contract_version')) is not int or envelope['contract_version']!=1):
            raise ProtocolError('Invalid collected Control v1 capability envelope')
        if (not isinstance(records,list) or len(records)!=3
                or any(not isinstance(row,dict) for row in records)
                or [row.get('operation') for row in records]!=['inspect','setting','setting']
                or [row.get('state') for row in records]!=['succeeded','failed','failed']):
            raise ProtocolError('Invalid collected Control terminal stages')
        operations=at(body,('capabilities','data','capabilities','operations'))
        if not isinstance(operations,dict) or set(operations)!=set(_ASYNC_CONTROL_AVAILABLE):
            raise ProtocolError('Collected Control operation catalogue changed')
        status=at(body,('terminal_records',0,'result','status'))
        if (not isinstance(status,dict) or status.get('device_mode')!='input'
                or status.get('profile_verified') is not True
                or status.get('settings_readback') is not False
                or status.get('device_mode_readback') is not False
                or status.get('generator_output_readback') is not False
                or status.get('hardware_validation')!='not_run'
                or status.get('lease_active') is not False):
            raise ProtocolError('Collected inspect lacks acknowledged input/profile proof')

    positions=[(('capabilities','data','capabilities','operations',name),{'available':available})
               for name,available in _ASYNC_CONTROL_AVAILABLE.items()]
    positions += [
        (('capabilities','data','status'),{'device_mode':'unknown','device_mode_readback':False,
            'generator_output_readback':False,'hardware_validation':'not_run','lease_seconds':0}),
        (('terminal_records',0,'result','status'),{'acquisition_paused':False,'restart_required':False}),
        (('terminal_records',2),{'context_current':True})]
    enriched=any(isinstance(at(candidate,path),dict) and any(key in at(candidate,path) for key in expected)
                 for path,expected in positions)
    projected=copy.deepcopy(candidate);evidence=[]
    for path,expected in positions:
        original,current=at(reference,path),at(candidate,path)
        if not isinstance(original,dict) or not isinstance(current,dict):
            raise ProtocolError('Collected Control typed position must be an object')
        pointer='/'+'/'.join(_pointer(key) for key in path)
        if set(current)-set(original)-set(expected):
            raise ProtocolError('Unknown collected Control addition at '+pointer)
        if enriched and not set(expected)<=set(current):
            raise ProtocolError('Partial collected Control enrichment at '+pointer)
        for key,wanted in expected.items():
            if key not in current:continue
            value=current[key]
            if type(value) is not type(wanted) or compare_value(wanted,value):
                raise ProtocolError('Incoherent collected Control addition at '+pointer+'/'+_pointer(key))
            if key not in original:
                at(projected,path).pop(key)
                evidence.append({'path':pointer+'/'+_pointer(key),'value':value,'expected':wanted,
                                 'contract':'owned_async_control_input_no_audio_v1'})
    return projected,evidence


def compare_http(reference: HTTPResult, candidate: HTTPResult, *, volatile_paths=(),
                 operation=None, storage_layout_verified=False,typed_control_collected=False) -> Outcome:
    try:
        first_mime = reference.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        second_mime = candidate.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if second_mime == "application/json" and candidate.status in (501,503):
            value=candidate.json()
            code=value.get("error",{}).get("code") if isinstance(value,dict) and isinstance(value.get("error"),dict) else None
            if candidate.status==503 and isinstance(code,str) or code=="candidate_route_unavailable":
                return Outcome("unavailable", ["Core explicitly reports unavailable: "+code])
        differences = [];extensions=[]
        if reference.status != candidate.status:
            differences.append("HTTP status differs")
        if first_mime != second_mime:
            differences.append("Content-Type differs")
        if first_mime == second_mime == "application/json":
            first, second = reference.json(), candidate.json()
            if (reference.status == candidate.status == 200
                    and operation == 'GET /api/v1/openapi.json'
                    and _reviewed_openapi_extension(first, second)):
                return Outcome('pass', ['Exact reviewed additive OpenAPI document and unchanged baseline verified'])
            if reference.status == candidate.status == 200:
                if typed_control_collected and operation==_ASYNC_CONTROL_OPERATION:
                    second,extensions=_collected_control_extensions(first,second)
                second = _diagnostic_extensions(second, operation, reference=first)
                if operation == "GET /api/v1/archive/capacity" and storage_layout_verified:
                    first, second = (_capacity_layout(value) for value in (first, second))
            differences.extend(compare_value(first, second, volatile_paths=volatile_paths))
        elif first_mime == second_mime == "application/x-ndjson":
            differences.extend(compare_value(load_ndjson(reference),load_ndjson(candidate),volatile_paths=volatile_paths))
        elif reference.body != candidate.body:
            differences.append("Original response bytes differ")
        return Outcome("mismatch" if differences else "pass", differences[:MAX_DIFFERENCES],extensions)
    except ProtocolError as error:
        return Outcome("error", [str(error)])


def probe_cli(argv, *, timeout=5.0, environment=None, cwd=None):
    """Run caller-selected safe CLI commands under retained tree authority."""
    from tools.modernization.owned_process import ManagedError, ManagedProcess
    if not argv or not (Path(argv[0]).is_file() or shutil.which(str(argv[0]),path=None if environment is None else environment.get("PATH"))):
        return {"state":"unavailable","successful":False,"error":"FileNotFoundError"}
    process=ManagedProcess(argv,environment=environment,cwd=cwd,limit=MAX_OUTPUT_BYTES)
    failure=cleanup_error=None
    try:
        process.start()
        process.wait(timeout)
        process.finish(2)
    except subprocess.TimeoutExpired:
        failure="command_deadline"
    except (ManagedError,OSError) as error:
        failure=str(error)
    except Exception as error:  # noqa: BLE001 - tool failures remain explicit in the parity report
        failure=type(error).__name__+": "+str(error)
    except BaseException:
        process.stop(2)
        process.dispose()
        raise
    if not process.closed:
        try:process.stop(2)
        except BaseException as error:cleanup_error=type(error).__name__+": "+str(error)  # noqa: BLE001 - retain failure and original authority
    outputs=process.outputs()
    if process.output_exceeded:failure="output_budget"
    code=process.returncode
    recovery=None if process.closed else process.recovery
    if process.closed:process.dispose()
    try:
        stdout,stderr=(value.decode("utf-8",errors="strict") for value in outputs)
    except UnicodeError:
        stdout=stderr=""
        failure="invalid_utf8_output"
    passed=code==0 and failure is None and cleanup_error is None
    result={"state":"pass" if passed else "mismatch","successful":passed,
            "exit_code":code,"stdout":stdout,"stderr":stderr,"error":failure,
            "stdout_sha256":hashlib.sha256(outputs[0]).hexdigest()}
    if cleanup_error is not None:result.update(cleanup_error=cleanup_error,recovery=recovery)
    return result


@dataclass(frozen=True)
class NodeSpec:
    kind: str
    executable: Path
    source_root: Path
    analysis_python: Path | None = None
    fixture_executable: Path | None = None


class OwnedNode:
    """Own one fresh node; do not remove its data until graceful process JOIN."""
    def __init__(self, spec: NodeSpec, directory: Path):
        self.spec=spec
        self.directory=directory
        self.ready=directory/"ready"
        self.process=None
        self.reader=None
        self.url=None
        self.joined=False
        self.log_overflow=False

    def _command(self,data):
        if self.spec.kind=="python":
            argv=[str(self.spec.executable),str(Path(__file__).resolve()),"--fixture-node",
                  "--source-root",str(self.spec.source_root),"--data-dir",str(data),"--ready-file",str(self.ready)]
        elif self.spec.fixture_executable is not None:
            argv=[str(self.spec.fixture_executable),"--data-dir",str(data),"--ready-file",str(self.ready)]
            if self.spec.analysis_python is not None:
                argv += ["--analysis-python",str(self.spec.analysis_python)]
        else:
            argv=[str(self.spec.executable),"--mode","replay","--data-dir",str(data),"--port","0",
                  "--host","127.0.0.1","--stdin-lifeline","--ready-file",str(self.ready)]
            if self.spec.analysis_python is not None:
                argv += ["--analysis-python",str(self.spec.analysis_python),"--research-best-effort"]
        return argv

    def __enter__(self):
        if self.spec.kind not in ("python","core"):
            raise ProtocolError("Unknown node backend")
        if self.spec.fixture_executable is not None:
            if self.spec.kind!="core":
                raise ProtocolError("Only the candidate may select an explicit synthetic embedding")
            if not self.spec.fixture_executable.is_file():
                raise FileNotFoundError("Requested synthetic candidate embedding is unavailable")
        if not self.spec.executable.is_file():
            raise FileNotFoundError("Requested node executable is unavailable")
        self.directory.mkdir(mode=0o700)
        data=self.directory/"data"
        data.mkdir(mode=0o700)
        environment={key:value for key,value in os.environ.items()
                     if not key.startswith(("ORI_RUNTIME_","ORI_CORE_"))}
        environment.update(PYTHONPATH=str(self.spec.source_root),PYTHONDONTWRITEBYTECODE="1",
                           OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1",MKL_NUM_THREADS="1")
        self.environment=environment
        argv=self._command(data)
        from tools.modernization.owned_process import ManagedProcess
        self.process=ManagedProcess(argv,environment=environment,cwd=self.spec.source_root,
                                    limit=MAX_OUTPUT_BYTES,merge=True,stdin_pipe=True)
        deadline=time.monotonic()+15
        try:
            self.process.start()
            while time.monotonic()<deadline:
                self.log_overflow=self.process.output_exceeded
                if self.process.poll() is not None:
                    raise ProtocolError("Node exited before owned readiness")
                if self.log_overflow:
                    raise ProtocolError("Node output budget exceeded")
                if self.ready.is_file():
                    if self.ready.stat().st_size>4096:
                        raise ProtocolError("Readiness byte budget exceeded")
                    value=self.ready.read_text(encoding="utf-8").strip()
                    parsed=urlsplit(value)
                    if parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username:
                        raise ProtocolError("Readiness must identify the owned ephemeral loopback listener")
                    self.url=value
                    return self
                time.sleep(.01)
            raise ProtocolError("Owned node readiness deadline exceeded")
        except BaseException:
            self.close()
            raise

    def request(self,method,path,payload=None,*,headers=None,maximum=MAX_RESPONSE_BYTES):
        if self.url is None or self.process.poll() is not None:
            raise ProtocolError("Owned node is not running")
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ProtocolError("Invalid relative request path")
        parsed=urlsplit(self.url)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=5)
        body=None if payload is None else (payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode("utf-8"))
        request_headers={"Connection":"close",**(headers or {})}
        if body is not None:request_headers["Content-Type"]="application/json"
        response=None
        try:
            connection.request(method,path,body=body,headers=request_headers)
            response=connection.getresponse()
            values={name.lower():value for name,value in response.getheaders()}
            length=values.get("content-length")
            if length is not None and (not length.isascii() or not length.isdigit() or int(length)>maximum):
                raise ProtocolError("HTTP response byte budget exceeded")
            body=response.read(maximum+1)
            if len(body)>maximum:raise ProtocolError("HTTP response byte budget exceeded")
            return HTTPResult(response.status,values,body)
        finally:
            if response is not None:response.close()
            connection.close()

    def events(self,path,*,frames=5):
        if not 1<=frames<=16:raise ProtocolError("SSE frame budget")
        parsed=urlsplit(self.url)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=5)
        response=None
        try:
            connection.request("GET",path,headers={"Accept":"text/event-stream","Connection":"close"})
            response=connection.getresponse()
            headers={key.lower():value for key,value in response.getheaders()}
            if response.status!=200 or headers.get("content-type","").split(";")[0]!="text/event-stream":
                body=response.read(MAX_RESPONSE_BYTES+1)
                if len(body)>MAX_RESPONSE_BYTES:raise ProtocolError("SSE error body budget")
                return HTTPResult(response.status,headers,body)
            output=[];current={};lines=[];total=0;deadline=time.monotonic()+5
            while len(output)<frames:
                if time.monotonic()>=deadline:raise ProtocolError("SSE committed frame deadline exceeded")
                line=response.fp.readline(65537)
                total+=len(line)
                if len(line)>65536 or total>MAX_RESPONSE_BYTES:raise ProtocolError("SSE byte budget exceeded")
                if not line:raise ProtocolError("SSE ended before its committed fixture frames")
                text=line.decode("utf-8",errors="strict").rstrip("\r\n")
                if not text:
                    if lines:
                        data=load_json("\n".join(lines).encode("utf-8"))
                        if path.startswith("/api/v1/"):
                            if data.get("contract")!="openradioinspector-platform" or data.get("contract_version")!=1:
                                raise ProtocolError("SSE version1 envelope mismatch")
                            sequence=data["data"]["sequence"]
                            if not isinstance(sequence,int) or current.get("id")!=str(sequence):
                                raise ProtocolError("SSE ID is not the original committed sequence")
                        current["data"]=data;output.append(current)
                    current={};lines=[]
                elif not text.startswith(":"):
                    key,separator,value=text.partition(":")
                    if separator:
                        value=value[1:] if value.startswith(" ") else value
                        if key=="data":lines.append(value)
                        else:current[key]=value
            body=json.dumps({"source_mime":"text/event-stream","frames":output},ensure_ascii=False,allow_nan=False).encode()
            return HTTPResult(200,{"content-type":"application/json"},body)
        finally:
            if response is not None:response.close()
            connection.close()

    def close(self):
        from tools.modernization.owned_process import ManagedError
        if self.process is None or self.joined:return
        failure=None
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
                self.process.finish(2)
            except BaseException as error:  # noqa: BLE001 - interruption also requires owned cleanup
                failure=error
                try:self.process.stop(5)
                except BaseException as cleanup:
                    raise ProtocolError("Node cleanup unconfirmed; retain archive/logs; recovery: "+self.process.recovery) from cleanup
            self.joined=self.process.closed
            self.log_overflow=self.process.output_exceeded
            if failure is not None or not self.joined or self.process.returncode!=0 or self.log_overflow or self.ready.exists():
                raise ProtocolError("Node did not prove graceful owned shutdown; retain its archive/logs") from failure
        finally:
            try:
                stdout,stderr=self.process.outputs()
                (self.directory/"process.log").write_bytes(stdout+stderr)
            except (ManagedError,OSError) as error:
                raise ProtocolError("Node logs unconfirmed; retain recovery: "+self.process.recovery) from error
            if self.process.closed:self.process.dispose()

    def readonly_cli(self):
        """Help/version and live readiness only; never provisioning or capture."""
        before=_snapshot(self)
        public=[str(self.spec.executable)]
        readiness=public.copy()
        if self.spec.kind=="python":
            public += ["-m","radioinspector"]
            readiness += ["-m","radioinspector.launcher"]
        readiness += ["--data-dir",str(self.directory/"data"),"--host","127.0.0.1","--port",str(urlsplit(self.url).port),"--check-ready"]
        results={name:probe_cli(argv,environment=self.environment,cwd=self.spec.source_root) for name,argv in
                 [("help",public+["--help"]),("version",public+["--version"]),("readiness",readiness)]}
        successful=all(item["successful"] for item in results.values())
        if compare_value(before,_snapshot(self)):
            successful=False
            results["state_change"]={"state":"mismatch","successful":False,"error":"Read-only CLI changed committed state"}
        return {"successful":successful,"commands":results,"scope":"read_only_fixture_commands; retained legacy CLI inventory is separate"}

    def __exit__(self,*_error):
        self.close()


def _fixture_ready_identity(path):
    entry=path.lstat()
    if not stat.S_ISREG(entry.st_mode):
        raise ProtocolError("Fixture readiness ownership changed; path retained")
    return entry.st_dev,entry.st_ino


def _remove_fixture_ready(path,identity):
    if _fixture_ready_identity(path)!=identity:
        raise ProtocolError("Fixture readiness ownership changed; path retained")
    path.unlink()


def _publish_fixture_ready(path,url):
    """Publish a complete private sentinel exclusively, retaining its identity."""
    descriptor,name=tempfile.mkstemp(prefix=".ready-",dir=path.parent)
    temporary=Path(name)
    identity=None
    try:
        entry=os.fstat(descriptor)
        identity=entry.st_dev,entry.st_ino
        output=os.fdopen(descriptor,"w",encoding="utf-8",newline="\n")
        descriptor=None
        with output:
            output.write(url+"\n")
            output.flush()
            os.fsync(output.fileno())
        if _fixture_ready_identity(temporary)!=identity:
            raise ProtocolError("Fixture readiness ownership changed; path retained")
        os.link(temporary,path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            _remove_fixture_ready(temporary,identity)
    return identity


def fixture_node(arguments):
    """Actual LocalCore + transport-only synthetic tinySA fixture; never serial."""
    sys.path.insert(0,str(arguments.source_root))
    from radioinspector.launcher import LocalCore
    from radioinspector.serial_instruments import TinySASerialAdapter
    from tests.tinysa_control_fixture import UltraWire
    from unittest.mock import patch

    def factory(stream,**_unused):
        if not isinstance(stream.port,str) or not stream.port.startswith("synthetic://api-parity/"):
            raise ProtocolError("Parity fixture refuses every physical device path")
        if stream.settings.get("adapter")!="tinysa-serial":
            raise ProtocolError("Parity fixture requires its explicit synthetic adapter")
        wire=UltraWire({"capture":b"\xf8\x00"*76800})
        adapter=TinySASerialAdapter(stream,transport=wire)
        # Keep ingestion fixture-controlled. The real DeviceWorker still owns
        # its command queue, and real instrument controller/protocol run above
        # the synthetic transport; there is no invented RF readback.
        adapter.instrument_idle=lambda:True
        return adapter

    with patch("radioinspector.serial_instruments.create_device_adapter",factory):
        with LocalCore(arguments.data_dir,host="127.0.0.1",port=0) as core:
            ready_identity=_publish_fixture_ready(arguments.ready_file,core.url)
            try:
                while sys.stdin.buffer.read(1):pass
            finally:
                # LocalCore joins HTTP, drains SSE/callbacks and closes SQLite
                # before its archive/data writer owners are released.
                pass
        _remove_fixture_ready(arguments.ready_file,ready_identity)
    return 0


def source_cases():
    """Original values are literal synthetic sources, never recomputed by Core."""
    sid="session-é";spectrum="spectrum/e\u0301";receiver="receiver/é"
    base="/api/v1/sessions/"+quote(sid,safe="")
    interval={"started_at":"2026-11-11T12:00:00.123456Z","ended_at":"2026-11-11T12:00:00.123457Z"}
    opaque={"é":-0.0,"e\u0301":-1.0000000000000002,"future":{"n":9007199254740993}}
    sweep={"id":"é","stream_id":spectrum,"interval":interval,"frequencies_hz":[100000001,100000003,100000005],
           "levels":[-0.0,-1.0000000000000002,-100.125],"level_unit":"dBm","settings":{"opaque":opaque}}
    observation={"id":"observation-é","stream_id":receiver,"interval":interval,"frequency_hz":9007199254740991,
                 "mode":"FM","level":-0.0,"level_unit":"s_meter_count","metadata":{"opaque":opaque}}
    lifecycle=["/data/created_at","/data/started_at","/data/ended_at"]
    cases=[]
    def add(name,method,path,payload=None,*,status=200,volatile=(),unchanged=False,operation=None):
        cases.append({"id":name,"method":method,"path":path,"payload":payload,"status":status,
                      "volatile_paths":list(volatile),"unchanged":unchanged,"operation":operation or method+" "+path})
    add("create-session","POST","/api/v1/sessions",{"id":sid,"name":"Synthetic API parity","metadata":{"opaque":opaque},"streams":[
        {"id":spectrum,"device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm"},
        {"id":receiver,"device":"synthetic","role":"receiver","level_unit":"s_meter_count"}]},status=201,volatile=lifecycle)
    add("get-session","GET",base,volatile=lifecycle)
    add("start-session","POST",base+"/start",{},volatile=lifecycle)
    add("source-sweep","POST",base+"/sweeps",sweep,status=201)
    add("source-sweep-normalization-distinct","POST",base+"/sweeps",dict(sweep,id="e\u0301"),status=201)
    add("source-observation","POST",base+"/observations",observation,status=201)
    add("list-source-sweeps","GET",base+"/sweeps?limit=100")
    add("list-source-observations","GET",base+"/observations?limit=100")
    add("sweep-summaries","GET",base+"/sweep-summaries?limit=100")
    add("create-candidate","POST",base+"/candidates",{"id":"candidate-é","source_stream_id":spectrum,
        "interval":interval,"center_frequency_hz":100000003,"bandwidth_hz":1,"peak_level":-0.0,"score":-1.0000000000000002,
        "level_unit":"dBm","observation_ids":["é"]},status=201)
    add("get-candidate","GET",base+"/candidates/"+quote("candidate-é",safe=""))
    add("enqueue","POST",base+"/candidates/"+quote("candidate-é",safe="")+"/investigations",
        {"id":"job-é","target_stream_id":receiver,"mode":" fm ","priority":9},status=201,volatile=("/data/created_at",))
    add("unconfirmed-handoff-no-write","POST",base+"/investigations/start-next",{"confirm":False,"investigation_id":"job-é"},status=400,unchanged=True)
    add("queued-priority","GET",base+"/investigations/next",volatile=("/data/created_at",))
    add("cancel-queued","POST",base+"/investigations/"+quote("job-é",safe="")+"/cancel",{},volatile=("/data/created_at","/data/ended_at"))
    add("candidate-after-cancel","GET",base+"/candidates/"+quote("candidate-é",safe=""))
    add("invalid-batch-priority","POST","/api/v1/sessions/missing-%C3%A9/sweeps/batch",{"items":[False]},status=400,unchanged=True)
    add("invalid-source-priority","POST","/api/v1/sessions/missing-%C3%A9/sweeps",{},status=400,unchanged=True)
    add("invalid-candidate-priority","POST","/api/v1/sessions/missing-%C3%A9/candidates",{"state":"resolved"},status=400,unchanged=True)
    add("exact-session-id-isolation","GET","/api/v1/sessions/session-e%CC%81",status=404,unchanged=True)
    large=dict(sweep,id="large-grid-20000",interval={"started_at":"2026-11-11T12:00:01.123456Z","ended_at":"2026-11-11T12:00:01.123457Z"},
               frequencies_hz=list(range(100000000,100020000)),levels=[-100.0]*19999+[-0.0])
    add("large-grid-20000-commit","POST",base+"/sweeps",large,status=201)
    cases[-1]["original_fields"]={"/data/"+key:value for key,value in large.items()}
    add("large-grid-20000-history","GET",base+"/sweeps?limit=1")
    cases[-1]["original_fields"]={"/data/0/"+key:value for key,value in large.items()}
    above_js=9007199254740993
    add("receiver-hz-above-js-safe","POST",base+"/observations",dict(observation,id="observation-hz-2p53plus1",frequency_hz=above_js),status=[201,400])
    cases[-1]["original_fields"]={"/data/frequency_hz":above_js,"/data/level":-0.0}
    cases[-1]["unchanged_on_error"]=True
    add("candidate-hz-above-js-safe","POST",base+"/candidates",{"id":"candidate-hz-2p53plus1","source_stream_id":spectrum,"interval":interval,
        "center_frequency_hz":above_js,"bandwidth_hz":1,"peak_level":-0.0,"score":-1.0000000000000002,"level_unit":"dBm"},status=[201,400])
    cases[-1]["original_fields"]={"/data/center_frequency_hz":above_js,"/data/peak_level":-0.0,"/data/score":-1.0000000000000002}
    cases[-1]["unchanged_on_error"]=True
    return cases


def public_cases():
    root=Path(__file__).resolve().parents[2]
    inventory=load_json((root/"contracts/fixtures/api-parity-inventory-v1.json").read_bytes())
    cases=source_cases()
    for suffix in ("export","export.ndjson"):
        cases.append({"id":"archive-source-"+suffix,"method":"GET","path":"/api/v1/sessions/session-%C3%A9/archive/"+suffix,
                      "payload":None,"status":200,"volatile_paths":[],"unchanged":False,"operation":"GET /api/v1/sessions/{id}/archive/"+suffix,
                      "envelope":not suffix.endswith("ndjson")})
    clocks=["/data/created_at","/data/started_at","/data/ended_at","/data/*/created_at","/data/*/started_at","/data/*/ended_at",
            "/data/*/occurred_at","/data/runtime/last_run_at"]
    for name in ("session","investigation"):
        for field in ("created_at","started_at","ended_at","listening_started_at"):
            clocks.append(f"/data/*/payload/{name}/{field}")
    for operation in inventory["operations"]+inventory["legacy_operations"]:
        method,template=operation["method"],operation["path"]
        if template=="/api/v1/monitoring-projects/audio-inputs":continue
        if template in ("/api/v1/events","/api/stream"):
            case={"id":"sse-v1" if "/v1/" in template else "sse-legacy","method":"GET","path":template+"?after=0",
                  "payload":None,"status":200,"volatile_paths":[value.replace("/data/*/","/frames/*/data/data/") for value in clocks if value.startswith("/data/*/")],
                  "unchanged":False,"operation":method+" "+template,"envelope":False,"transport":"sse","frames":5 if "/v1/" in template else 1}
            cases.append(case);continue
        path=re.sub(r"\{[^}]+\}",lambda _:quote("missing-é",safe=""),template)
        payload=None if method=="GET" else {}
        if template=="/api/v1/sessions" and method=="POST":payload={"state":"running"}
        if template=="/api/v1/monitoring-projects" and method=="POST":payload={"name":""}
        if template=="/api/measure":path+="?start=100000000&stop=100000005"
        cases.append({"id":"published-"+hashlib.sha256((method+template).encode()).hexdigest()[:12],"method":method,"path":path,"payload":payload,
                      "status":[200,201,400,404,409,503],"volatile_paths":clocks,"unchanged":method=="POST" and "missing-" in path,
                      "operation":method+" "+template,"envelope":template.startswith("/api/v1/") and not template.endswith("openapi.json")})
    return cases,inventory


def asynchronous_protocol(node):
    """Real typed202/poll/dedup/conflict/cancel over a transport-only fixture."""
    sid="lab-é";stream="lab/e\u0301";base="/api/v1/sessions/"+quote(sid,safe="")
    instrument=base+"/streams/"+quote(stream,safe="")+"/instrument"
    case={"id":"instrument-async-protocol","method":"PROTOCOL","path":instrument,"operation":"tinySA typed202/poll/idempotency/conflict/cancel",
          "payload":None,"status":200,"volatile_paths":[],"unchanged":False,"envelope":False}
    created=node.request("POST","/api/v1/sessions",{"id":sid,"name":"Synthetic async wire fixture","streams":[
        {"id":stream,"device":"synthetic","role":"spectrum_analyzer","level_unit":"dBm","port":"synthetic://api-parity/lab",
         "settings":{"adapter":"tinysa-serial","start_hz":100000000,"stop_hz":101000000,"points":3,"rbw_hz":10000,"period_s":0.05}}]})
    if created.status!=201:return {"case":case,"response":created,"checks":["Async synthetic session creation failed"]}
    capabilities=node.request("GET",instrument+"/capabilities")
    if capabilities.status!=200:return {"case":case,"response":capabilities,"checks":["Typed instrument capability unavailable"]}
    if node.request("POST",base+"/start",{}).status!=200:raise ProtocolError("Async fixture session did not start")
    worker=node.request("POST",base+"/streams/"+quote(stream,safe="")+"/start",{})
    if worker.status!=200:return {"case":case,"response":worker,"checks":["Synthetic command worker unavailable; physical serial is forbidden"]}
    headers={"X-Instrument-Client":"parity-client-0000000000000000"}
    observed=[];connection_id=None
    def poll(identifier):
        deadline=time.monotonic()+5
        for _ in range(100):
            response=node.request("GET",instrument+"/operations/"+quote(identifier,safe=""),headers=headers)
            if response.status!=200:raise ProtocolError("Accepted async operation cannot be polled")
            value=response.json()["data"]
            if value["operation_id"]!=identifier or value["session_id"]!=sid or value["stream_id"]!=stream:
                raise ProtocolError("Async original operation/session/stream identity changed")
            if value["state"] in ("succeeded","failed","cancelled","unknown"):return response,value
            if time.monotonic()>=deadline:break
            time.sleep(.01)
        raise ProtocolError("Async terminal observation deadline exceeded")
    def canonical(value,label):
        nonlocal connection_id
        value=json.loads(json.dumps(value,allow_nan=False))
        if not isinstance(value.get("created_at"),(float,int)) or not math.isfinite(value["created_at"]):
            raise ProtocolError("Invalid async monotonic admission timestamp")
        value["created_at"]="<validated-monotonic-admission>";value["operation_id"]="<operation:"+label+">"
        status=(value.get("result") or {}).get("status")
        if isinstance(status,dict) and "connection_id" in status:
            if connection_id is None:connection_id=status["connection_id"]
            if not isinstance(connection_id,str) or not connection_id or status["connection_id"]!=connection_id:
                raise ProtocolError("Original command connection identity changed")
            status["connection_id"]="<original-fixture-connection>"
        return value
    inspect=node.request("POST",instrument+"/operations",{"operation":"inspect"},headers=headers)
    if inspect.status!=202:raise ProtocolError("Typed inspect did not return202")
    identifier=inspect.json()["data"]["operation_id"]
    _,finished=poll(identifier)
    if finished["state"]!="succeeded" or finished.get("context_current") is not True:raise ProtocolError("Inspect lost its original worker context")
    observed.append(canonical(finished,"inspect"))
    denied={"operation":"setting","parameters":{"name":"output_enabled","value":True},"request_id":"parity-denied-setting"}
    accepted=node.request("POST",instrument+"/operations",denied,headers=headers)
    if accepted.status!=202:raise ProtocolError("Valid typed lease-denied request did not use202 protocol")
    identifier=accepted.json()["data"]["operation_id"]
    _,finished=poll(identifier)
    if finished["state"]!="failed" or not finished["error"]:raise ProtocolError("Mutation without a lab lease was not refused")
    observed.append(canonical(finished,"denied"))
    duplicate=node.request("POST",instrument+"/operations",denied,headers=headers)
    if duplicate.status!=202 or duplicate.json()["data"]["operation_id"]!=identifier:raise ProtocolError("Idempotent request minted another operation")
    conflict=node.request("POST",instrument+"/operations",dict(denied,parameters={"name":"rbw_hz","value":10000}),headers=headers)
    if conflict.status!=409:raise ProtocolError("Conflicting idempotency request was accepted")
    cancelled=node.request("POST",instrument+"/operations/"+quote(identifier,safe="")+"/cancel",{},headers=headers)
    if cancelled.status!=200 or cancelled.json()["data"]["state"]!="failed":raise ProtocolError("Cancel replay rewrote a terminal operation")
    observed.append(canonical(cancelled.json()["data"],"denied"))
    raw=node.request("POST",instrument+"/operations",{"operation":"raw","parameters":{"command":"output on"}},headers=headers)
    if raw.status!=400:raise ProtocolError("Arbitrary shell operation accepted")
    if node.request("POST",base+"/streams/"+quote(stream,safe="")+"/stop",{}).status!=200:raise ProtocolError("Owned fake command worker did not stop")
    body={"capabilities":capabilities.json(),"terminal_records":observed,"conflicting_request":conflict.json(),"raw_rejected":raw.json()}
    return {"case":case,"response":HTTPResult(200,{"content-type":"application/json"},json.dumps(body,ensure_ascii=False,allow_nan=False).encode()),"checks":[]}


def _snapshot(node):
    health=node.request("GET","/api/v1/health")
    sessions=node.request("GET","/api/v1/sessions")
    if health.status!=200 or sessions.status!=200:
        raise ProtocolError("Cannot prove rejected-request archive invariance")
    data=health.json()["data"]
    if not isinstance(data.get("last_event_id"),int):
        raise ProtocolError("No committed event watermark for no-state-change proof")
    return {"watermark":data["last_event_id"],"sessions":sessions.json()}


def execute_cases(node,cases):
    if len(cases)>256:raise ProtocolError("Case count budget exceeded")
    trace=[];total=0
    for case in cases:
        before=_snapshot(node) if case["method"]=="POST" or case["unchanged"] or case.get("unchanged_on_error") else None
        response=node.events(case["path"],frames=case["frames"]) if case.get("transport")=="sse" else node.request(case["method"],case["path"],case["payload"])
        total+=len(response.body)
        if total>32*1024*1024:raise ProtocolError("Aggregate trace byte budget exceeded")
        checks=[]
        capacity_verified=False
        statuses=case["status"] if isinstance(case["status"],list) else [case["status"]]
        if response.status not in statuses:checks.append("Expected fixture HTTP status was not returned")
        try:
            value=load_ndjson(response) if response.headers.get("content-type","").split(";")[0]=="application/x-ndjson" else response.json()
            if case["operation"] == "GET /api/v1/archive/capacity" and response.status == 200:
                import sqlite3
                database = node.directory/"data"/"sessions.sqlite3"
                connection = sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=2)
                try:
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN")
                    logical = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
                    _capacity_layout(value, logical_bytes=logical)
                    capacity_verified=True
                finally:
                    connection.close()
            if case.get("envelope",True) and (value.get("contract")!="openradioinspector-platform" or value.get("contract_version")!=1):
                checks.append("Version1 envelope missing or changed")
            if case.get("envelope",True) and response.status>=400 and not isinstance(value.get("error",{}).get("code"),str):
                checks.append("Expected public error code missing")
            if case.get("envelope",True) and response.status<400 and "data" not in value:checks.append("Successful response data missing")
            if response.status<400:
                for pointer,expected in case.get("original_fields",{}).items():
                    actual=value
                    try:
                        for part in pointer.lstrip("/").split("/"):
                            part=part.replace("~1","/").replace("~0","~")
                            actual=actual[int(part)] if isinstance(actual,list) else actual[part]
                        if compare_value(expected,actual):checks.append("Original source field changed: "+pointer)
                    except (KeyError,IndexError,TypeError,ValueError):checks.append("Original source field missing: "+pointer)
        except ProtocolError as error:
            checks.append(str(error)[:240])
        except AttributeError:
            checks.append("Strict envelope JSON unavailable")
        if before is not None and (case["unchanged"] or response.status>=400):
            after=_snapshot(node)
            if compare_value(before,after):checks.append("Rejected request changed committed archive/session state")
        trace.append({"case":case,"response":response,"checks":checks,"capacity_verified":capacity_verified})
    return trace


def _trace_document(trace):
    import base64
    def request_body(case):
        value=case.get("payload")
        return None if value is None else base64.b64encode(value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()).decode("ascii")
    return [{"case_id":step["case"]["id"],"operation":step["case"]["operation"],"method":step["case"]["method"],"path":step["case"]["path"],
             "request_body_base64":request_body(step["case"]),"status":step["response"].status,
             "content_type":step["response"].headers.get("content-type"),"body_base64":base64.b64encode(step["response"].body).decode("ascii"),
             "headers":{key:value for key,value in step["response"].headers.items() if key in ("content-type","content-length","x-archive-sweeps","x-archive-observations","cache-control","x-content-type-options")},
             "body_sha256":hashlib.sha256(step["response"].body).hexdigest(),"checks":step["checks"],
             "capacity_verified":step.get("capacity_verified",False)} for step in trace]


def _compare_traces(reference,candidate):
    if len(reference)!=len(candidate):return [{"state":"error","details":["Trace length differs"]}]
    results=[]
    for left,right in zip(reference,candidate):
        if left["case"]["id"]!=right["case"]["id"]:
            raise ProtocolError("Trace case identity changed")
        outcome=compare_http(left["response"],right["response"],volatile_paths=left["case"]["volatile_paths"],
                             operation=left["case"]["operation"],
                             typed_control_collected=(left["case"]["id"]=="instrument-async-protocol"
                                 and left["case"]["operation"]==right["case"]["operation"]==_ASYNC_CONTROL_OPERATION
                                 and left["case"]["method"]==right["case"]["method"]=="PROTOCOL"
                                 and left["case"]["path"]==right["case"]["path"]),
                             storage_layout_verified=left.get("capacity_verified",False) and right.get("capacity_verified",False))
        if outcome.state=="pass" and (left["checks"] or right["checks"]):
            outcome=Outcome("error",left["checks"]+right["checks"],outcome.validated_extensions)
        elif left["checks"] or right["checks"]:
            outcome.details.extend(left["checks"]+right["checks"])
        results.append({"case_id":left["case"]["id"],"operation":left["case"]["operation"],"state":outcome.state,
                        "successful":outcome.successful,"reference_status":left["response"].status,
                        "candidate_status":right["response"].status,"details":outcome.details,
                        "validated_extensions":outcome.validated_extensions})
    return results


def approved_source_snapshot(source_root, inventory):
    """Match one complete route/support map, never combine per-file revisions."""
    baseline=inventory["source_sha256"]
    if not isinstance(baseline,dict) or not baseline or len(baseline)>16:
        raise ProtocolError("Invalid reviewed source authority")
    support=inventory.get("source_support_sha256",{})
    if not isinstance(support,dict) or len(support)>16 or set(support)&set(baseline):
        raise ProtocolError("Invalid reviewed source support hashes")
    original={**baseline,**support}
    if any(not isinstance(name,str) or not name or len(name)>256 or "\\" in name or ":" in name
           or any(part in ("", ".", "..") for part in name.split("/")) for name in original):
        raise ProtocolError("Invalid reviewed source path")
    snapshots=[{"revision":inventory["python_source_revision"],"source_sha256":original}]
    additional=inventory.get("approved_source_snapshots",[])
    if not isinstance(additional,list) or len(additional)>4:
        raise ProtocolError("Invalid reviewed source snapshots")
    snapshots += additional
    for snapshot in snapshots:
        if not isinstance(snapshot,dict) or set(snapshot)!={"revision","source_sha256"}:
            raise ProtocolError("Invalid reviewed source snapshot")
        hashes=snapshot["source_sha256"]
        if (not isinstance(snapshot["revision"],str) or not re.fullmatch(r"[0-9a-f]{40}",snapshot["revision"])
                or not isinstance(hashes,dict) or set(hashes)!=set(original)
                or any(not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest)
                       for digest in hashes.values())):
            raise ProtocolError("Invalid reviewed source snapshot hashes")
    actual={name:file_sha256(Path(source_root)/name) if (Path(source_root)/name).is_file() else None
            for name in original}
    for snapshot in snapshots:
        if actual==snapshot["source_sha256"]:
            return snapshot["revision"],actual
    stale=[name for name,digest in original.items() if actual[name]!=digest]
    return None,stale


def run_parity(producer:NodeSpec,candidate:NodeSpec|None,artifacts:Path,*,profile="sources"):
    """A failing producer sanity stops candidate evaluation. Never manufacture green."""
    if profile not in ("sources","public"):raise ProtocolError("Unknown parity profile")
    artifacts=artifacts.resolve()
    artifacts.mkdir(mode=0o700,exist_ok=True)
    if list(artifacts.iterdir()):raise ProtocolError("Artifacts directory must be fresh and empty")
    cases=source_cases();inventory=None
    if profile=="public":cases,inventory=public_cases()
    report={"format":"ori-public-api-parity","format_version":1,"successful":False,"profile":profile,
            "producer_sanity":{"successful":False},"candidate":{"state":"unavailable","successful":False},"nodes":[],"cli_probes":[],
            "gate_source_sha256":file_sha256(Path(__file__)),"producer_executable":str(producer.executable),
            "limits":{"cases":256,"response_bytes":MAX_RESPONSE_BYTES,"trace_bytes_per_node":32*1024*1024,"node_output_bytes":MAX_OUTPUT_BYTES,"sse_frames":16},
            "validation":"synthetic software; no physical hardware, throughput or latency acceptance"}
    traces=[]
    if inventory is not None:
        report["inventory"]={"published_v1_operations":len(inventory["operations"]),"legacy_operations":len(inventory["legacy_operations"]),
                             "radioinspector_subcommands":inventory["radioinspector_subcommands"],"console_entrypoints":inventory["console_entrypoints"],
                             "physical_not_run":["GET /api/v1/monitoring-projects/audio-inputs"],"source_sha256":inventory["source_sha256"],
                             "source_support_sha256":inventory.get("source_support_sha256",{})}
    try:
        if candidate is not None:
            http_executable=candidate.fixture_executable or candidate.executable
            report["candidate"].update(
                executable_sha256=file_sha256(candidate.executable) if candidate.executable.is_file() else None,
                cli_executable=str(candidate.executable),
                http_node={"kind":"production_cli" if candidate.fixture_executable is None else "trusted_synthetic_embedding",
                           "executable":str(http_executable),
                           "executable_sha256":file_sha256(http_executable) if http_executable.is_file() else None})
        if inventory is not None:
            revision,source_hashes=approved_source_snapshot(producer.source_root,inventory)
            if revision is None:
                report["inventory"]["stale_files"]=source_hashes
                raise ProtocolError("Authority inventory changed; regenerate and review before claiming complete route coverage")
            report["inventory"]["matched_source_revision"]=revision
            report["inventory"]["matched_source_sha256"]=source_hashes
        for label in ("producer-a","producer-b"):
            node=OwnedNode(producer,artifacts/label)
            try:
                with node:
                    trace=execute_cases(node,cases)
                    if profile=="public":trace.append(asynchronous_protocol(node))
                    report["cli_probes"].append({"label":label,**node.readonly_cli()})
                traces.append(trace)
                (artifacts/(label+".json")).write_text(json.dumps(_trace_document(trace),ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
            finally:
                report["nodes"].append({"label":label,"joined":node.joined,"exit_code":None if node.process is None else node.process.returncode})
        results=_compare_traces(*traces)
        sanity=bool(results) and all(item["successful"] for item in results) and all(item["successful"] for item in report["cli_probes"])
        report["producer_sanity"]={"successful":sanity,"compared_cases":len(results),"results":results,
                                   "async_protocol_verified":profile=="public" and any(item["case_id"]=="instrument-async-protocol" and item["successful"] for item in results)}
        if sanity and candidate is not None and candidate.executable.is_file():
            node=OwnedNode(candidate,artifacts/"candidate")
            try:
                with node:
                    trace=execute_cases(node,cases)
                    if profile=="public":trace.append(asynchronous_protocol(node))
                    report["cli_probes"].append({"label":"candidate",**node.readonly_cli()})
                (artifacts/"candidate.json").write_text(json.dumps(_trace_document(trace),ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
                results=_compare_traces(traces[0],trace)
                successful=bool(results) and all(item["successful"] for item in results) and report["cli_probes"][-1]["successful"]
                if (file_sha256(candidate.executable)!=report["candidate"]["executable_sha256"] or
                    file_sha256(candidate.fixture_executable or candidate.executable)!=report["candidate"]["http_node"]["executable_sha256"]):
                    raise ProtocolError("Candidate executable bytes changed during the owned run")
                report["candidate"].update(state="pass" if successful else "failed",successful=successful,results=results)
                report["successful"]=successful
            finally:
                report["nodes"].append({"label":"candidate","joined":node.joined,"exit_code":None if node.process is None else node.process.returncode})
    except (OSError,ProtocolError,KeyError,ValueError,http.client.HTTPException) as error:
        report["error"]=type(error).__name__+": "+str(error)
    (artifacts/"report.json").write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+"\n",encoding="utf-8")
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-node",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--source-root",type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-dir",type=Path)
    parser.add_argument("--ready-file",type=Path)
    parser.add_argument("--producer-python",type=Path,default=Path(sys.executable))
    parser.add_argument("--candidate",type=Path)
    parser.add_argument("--candidate-fixture",type=Path,help="Explicit trusted synthetic Core embedding for HTTP only; CLI probes still use --candidate")
    parser.add_argument("--candidate-source-root",type=Path)
    parser.add_argument("--analysis-python",type=Path)
    parser.add_argument("--artifacts",type=Path)
    parser.add_argument("--profile",choices=("sources","public"),default="public")
    args=parser.parse_args(argv)
    if args.fixture_node:
        if args.data_dir is None or args.ready_file is None:parser.error("Fixture paths required")
        return fixture_node(args)
    if args.artifacts is None:parser.error("--artifacts is required")
    if args.candidate_fixture is not None and args.candidate is None:
        parser.error("--candidate-fixture requires the original --candidate executable")
    producer=NodeSpec("python",args.producer_python.absolute(),args.source_root.resolve())
    candidate=None if args.candidate is None else NodeSpec("core",args.candidate.resolve(),
        (args.candidate_source_root or args.source_root).resolve(),args.analysis_python,
        None if args.candidate_fixture is None else args.candidate_fixture.resolve())
    result=run_parity(producer,candidate,args.artifacts,profile=args.profile)
    print(json.dumps({"successful":result["successful"],"producer_sanity":result["producer_sanity"]["successful"],
                      "candidate":result["candidate"]["state"],"report":str(args.artifacts/"report.json")},ensure_ascii=False))
    return 0 if result["successful"] else 1


if __name__=="__main__":
    if __package__ in (None, ""):
        # Direct and isolated script execution must resolve our private helpers
        # from this checkout, independently of cwd or an inherited PYTHONPATH.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
