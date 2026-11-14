"""Actual Python adapter/controller oracle on owned synthetic transports only.

No serial connector, archive, database, server or physical RF is used. Cold
Python inspect includes prepare side effects; Rust deliberately separates those
into an explicit Prepare operation. Warm/cached queries are the parity oracle.
"""
from __future__ import annotations

import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[_name] = "1"

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from radioinspector.contracts import DeviceRole, DeviceStream, Sweep, parse_time, to_primitive
from radioinspector.monitoring.detector import fingerprint, stable
from radioinspector.serial_instruments import TinySASerialAdapter
from radioinspector.tinysa.controller import Controller
from tests.tinysa_control_fixture import UltraWire

CONNECTION = "0123456789abcdef0123456789abcdef"
CLOCK = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)


def adapter(extra=None, rbw=10_000):
    stream = DeviceStream(session_id="inspection-session", id="spectrum/one", device="tinySA Ultra ZS405",
        role=DeviceRole.SPECTRUM_ANALYZER, level_unit="dBm", port="synthetic://owned-only",
        settings={"adapter":"tinysa-serial","start_hz":100_000_000,"stop_hz":101_000_000,"points":3,"rbw_hz":rbw})
    wire = UltraWire(extra)
    if rbw != 10_000: wire.responses[f"rbw {rbw//1000}"] = b""
    return TinySASerialAdapter(stream,transport=wire,clock=lambda:CLOCK), wire


def controller(value):
    control = Controller(value,monotonic=lambda:100.)
    control.connection_id = CONNECTION
    control._publish()
    value._instrument = control
    return control


def status(value):
    return {key:value[key] for key in ("mode","profile","revision","connection_id","settings_complete","profile_verified","settings","settings_source","settings_readback","device_mode","device_mode_readback","generator_output_readback","hardware_validation")}


def inspection_cases():
    result=[]
    for name,help_body in (("normal",None),("regex-token-boundaries",b"commands: 2scan _marker UPper 99foo_bar MODE\r\n"),("empty-help",b"")):
        value,wire=adapter({} if help_body is None else {"help":help_body})
        try:
            value._prepare();prepared=list(wire.sent)
            control=controller(value)
            inspected=control.execute({"operation":"inspect"})
            first=list(wire.sent[len(prepared):])
            before=len(wire.sent);again=control.execute({"operation":"inspect"})
            result.append({"name":name,"prepare_commands":prepared,"warm_commands":first,
                "cached_commands":wire.sent[before:],"commands":inspected["commands"],
                "status":status(inspected["status"]),"cached_status":status(again["status"]),
                "help_body":wire.responses["help"].decode("ascii")})
        finally:value.close()
    value,wire=adapter()
    try:
        inspected=controller(value).execute({"operation":"inspect"})
        cold={"commands":list(wire.sent),"status":status(inspected["status"]),
              "rust_deliberate_query_only_commands":["info","version","deviceid","help"]}
    finally:value.close()
    return result,cold


def sweep_cases():
    cases=[]
    for name,levels,inspect,rbw in (
        ("receive-initial",[-90.,-42.50000000000001,-80.],False,10_000),
        ("receive-30k",[-0.,1.0000000000000002,-1e-300],False,30_000),
        ("control-inspected",[-90.,-42.50000000000001,-80.],True,10_000),
        ("first-tied-peak",[-0.,0.,-1e-300],True,10_000),
        ("middle-global-peak",[-90.,-10.,-80.],True,10_000),
        ("last-global-peak",[-90.,-80.,-10.],True,10_000),
        ("explicit-trace-dbm",[-90.,-42.50000000000001,-80.],True,10_000),
    ):
        scan="".join(f"{hz} {level!r} 0\r\n" for hz,level in zip([100_000_000,100_500_000,101_000_000],levels)).encode()
        value,wire=adapter({"scan 100M 101M 3 3":scan},rbw=rbw)
        try:
            if inspect:
                control=controller(value)
                control.execute({"operation":"inspect"})
                if name=="explicit-trace-dbm":control.execute({"operation":"setting","parameters":{"name":"trace_unit","value":"dBm"}})
            sweep=replace(value.poll(),id="fixture-"+name)
            document=to_primitive(sweep)
            peak=max(range(len(sweep.levels)),key=sweep.levels.__getitem__)
            cases.append({"name":name,"inspected":inspect,"rbw_hz":rbw,"commands":list(wire.sent),
                "sweep":document,"stable_settings":stable(sweep.settings),"fingerprint":fingerprint(sweep),
                "level_bits":[struct.pack(">d",n).hex() for n in sweep.levels],
                "first_peak_index":peak,"peak_frequency_hz":sweep.frequencies_hz[peak],
                "peak_level_bits":struct.pack(">d",sweep.levels[peak]).hex(),"scan_body":scan.decode()})
        finally:value.close()
    return cases


def emit():
    inspected,cold=inspection_cases()
    sources=["radioinspector/serial_instruments.py","radioinspector/tinysa/controller.py","radioinspector/tinysa_profile.py","radioinspector/monitoring/detector.py"]
    return {"fixture_version":1,"synthetic":True,"physical_validation":"not_run",
        "source_sha256":{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
        "inspection":inspected,"python_cold_inspect":cold,"sweeps":sweep_cases()}


def check_rust(path):
    """Compare actual Rust live samples with the original Python algorithms.

    Canonical IDs and the deterministic comparison interval belong to the test
    fixture. Rust's separate real operation interval is validated, not replaced
    or described as if it were obtained from the fixed Python fixture clock.
    """
    import re
    expected={case["name"]:case for case in emit()["sweeps"]}
    actual=json.loads(Path(path).read_text())
    assert len(actual)==len(expected)
    seen=set()
    for row in actual:
        assert row["name"] not in seen
        seen.add(row["name"])
        case=expected[row["name"]]
        comparison={**case["sweep"],**{key:row[key] for key in ("frequencies_hz","levels","level_unit","settings")}}
        sweep=Sweep.from_dict(comparison)
        assert row["settings"]==case["sweep"]["settings"],row["name"]
        assert stable(row["settings"])==case["stable_settings"],row["name"]
        assert fingerprint(sweep)==case["fingerprint"],row["name"]
        assert [struct.pack(">d",value).hex() for value in sweep.levels]==case["level_bits"],row["name"]
        peak=max(range(len(sweep.levels)),key=sweep.levels.__getitem__)
        assert peak==case["first_peak_index"]
        assert sweep.frequencies_hz[peak]==case["peak_frequency_hz"]
        assert struct.pack(">d",sweep.levels[peak]).hex()==case["peak_level_bits"]
        interval=row["operation_interval"]
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",interval[key]) for key in ("started_at","ended_at"))
        assert parse_time(interval["started_at"])<=parse_time(interval["ended_at"])
    assert seen==set(expected)
    print(f"{len(actual)} actual Rust samples match Python metadata/fingerprints/f64 bits/first peaks; UTC operation intervals valid")


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--check-rust":check_rust(sys.argv[2])
    elif len(sys.argv)==1:(ROOT/"contracts/fixtures/instruments-inspection-v1.json").write_text(json.dumps(emit(),ensure_ascii=False,allow_nan=False,indent=2)+"\n")
    else:raise SystemExit("Usage: generate_inspection_fixtures.py [--check-rust EVIDENCE_JSON]")
