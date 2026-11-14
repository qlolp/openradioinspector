"""Compare an isolated installed scientific library with the owned legacy oracle.

The RPC entry point is an absolute owned script until the release wheel includes
it. The child uses -I and imports only its installed radioinspector algorithms.
"""
from __future__ import annotations

import os

for _key in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "ARROW_NUM_THREADS",
):
    os.environ[_key] = "1"

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_fixtures import create, freeze
from tests.test_legacy_rpc import sealed_snapshot
from tests.test_research_rpc import shared_io_capability, shared_io_total


def assert_exact(actual, expected, path="$"):
    if type(actual) is not type(expected):
        raise AssertionError(f"{path}: {type(actual)} != {type(expected)}")
    if isinstance(actual, float):
        if struct.pack(">d", actual) != struct.pack(">d", expected):
            raise AssertionError(f"{path}: f64 bits differ")
    elif isinstance(actual, dict):
        if actual.keys() != expected.keys():
            raise AssertionError(f"{path}: object keys differ")
        for key in actual:
            assert_exact(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(actual, list):
        if len(actual) != len(expected):
            raise AssertionError(f"{path}: lengths differ")
        for index, (left, right) in enumerate(zip(actual, expected)):
            assert_exact(left, right, f"{path}[{index}]")
    elif actual != expected:
        raise AssertionError(f"{path}: {actual!r} != {expected!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Trusted installed worker interpreter")
    parser.add_argument("--installed-module", action="store_true", help="Verify the installed -I -m entry point after wheel refresh")
    parser.add_argument("--shared-io", action="store_true", help="Require the private shared file-I/O capability and bounded success receipts")
    args = parser.parse_args()
    worker = ROOT / "radioinspector/legacy_rpc.py"
    worker_command = ([args.python, "-I", "-m", "radioinspector.legacy_rpc"]
                      if args.installed_module else [args.python, "-I", str(worker)])
    cases = []
    with tempfile.TemporaryDirectory(prefix="ori-legacy-process-proof-") as directory:
        directory = Path(directory)
        source, oracle = create(directory / "source")
        # Match the actual HTTP serialization boundary: NumPy scalar subclasses
        # in direct helper results become ordinary JSON float tokens.
        oracle = json.loads(json.dumps(oracle, ensure_ascii=False, allow_nan=False))
        source_before = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = directory / "snapshot"
        nonce = sealed_snapshot(source, snapshot)
        shared_io, ledger = shared_io_capability(directory) if args.shared_io else (None, None)
        receipts = []

        def invoke(action, state, version, parameters=None):
            request = {
                "version": 1, "request_id": "installed-legacy-proof",
                "operation": "analysis", "method": "legacy_workspace",
                "action": action, "source_version": version,
                "database": str(source), "snapshot_directory": str(snapshot),
                "snapshot_nonce": nonce,
                "state": {"threshold": state["threshold"], "signals": state["signals"]},
                "parameters": parameters or {}, "legacy_limits": {},
            }
            before_io = shared_io_total(ledger) if ledger else 0
            if shared_io is not None:
                request['shared_io'] = shared_io
            completed = subprocess.run(
                worker_command,
                input=json.dumps(request, ensure_ascii=False, allow_nan=False).encode(),
                capture_output=True, timeout=60,
            )
            if not completed.stdout or len(completed.stdout) > 16 * 1024**2:
                raise AssertionError(f"Invalid worker output: {completed.stderr.decode(errors='replace')}")
            response = json.loads(completed.stdout)
            assert response["request_id"] == request["request_id"]
            assert response["operation"] == "analysis"
            if shared_io is not None and response['ok']:
                receipt=response.pop('shared_io_receipt')
                assert set(receipt)=={'version','accounted_bytes','charges','max_charge_bytes'}
                assert receipt['version']==1 and 0<receipt['max_charge_bytes']<=65536
                assert receipt['accounted_bytes']==shared_io_total(ledger)-before_io
                assert receipt['charges']>0
                receipts.append(receipt)
            else:
                assert 'shared_io_receipt' not in response
            assert set(response)=={'version','request_id','operation','ok','result' if response['ok'] else 'error'}
            cases.append(action)
            return response

        loaded = invoke("load", {"threshold": None, "signals": []}, 0)
        assert loaded["ok"], loaded
        state = loaded["result"]["state"]
        assert_exact(freeze(state), oracle["loaded"])
        for action, parameters, expected in (
            ("state", {"include_waterfall": True}, oracle["loaded"]),
            ("state", {"include_waterfall": False}, {k: v for k, v in oracle["loaded"].items() if k != "waterfall"}),
            ("signals", {}, {"signals": oracle["loaded"]["signals"]}),
            *((case["action"], case["parameters"], case["body"]) for case in oracle["queries"]),
        ):
            response = invoke(action, state, state["version"], parameters)
            assert response["ok"], response
            assert_exact(freeze(response["result"]["reply"]), expected)
        for case in oracle["thresholds"]:
            response = invoke("threshold", state, state["version"], case["payload"])
            if case["status"] == 200:
                assert response["ok"], response
                assert_exact(freeze(response["result"]["reply"]), case["body"])
                state = response["result"]["state"]
                assert_exact(freeze(state), case["state"])
            else:
                assert not response["ok"], response
                assert response["error"]["http_status"] == case["status"]
                assert response["error"]["api_message"] == case["body"]["error"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_before

    modules = ["web.server", "panorama", "threshold", "signals", "jsonio", "research_rpc"]
    if args.installed_module:
        modules.append("legacy_rpc")
    if args.shared_io:
        modules.append("worker_io")
    probe = f"""
import importlib, hashlib, json, sys
from pathlib import Path
names = {modules!r}
files = {{name: importlib.import_module('radioinspector.' + name).__file__ for name in names}}
assert all(Path(path).resolve().is_relative_to(Path(sys.prefix).resolve()) for path in files.values()), 'Module escaped installed interpreter prefix'
evidence = {{'python': sys.version, 'files': {{}}}}
for name, path in files.items():
    with open(path, 'rb') as source:
        evidence['files'][name] = {{'path': path, 'sha256': hashlib.sha256(source.read()).hexdigest()}}
print(json.dumps(evidence))
"""
    installed = subprocess.run([args.python, "-I", "-c", probe], check=True, capture_output=True, timeout=60)
    evidence = json.loads(installed.stdout)
    evidence.update(
        worker_script_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
        fixture_sha256=hashlib.sha256((ROOT / "contracts/fixtures/legacy-workspace-v1.json").read_bytes()).hexdigest(),
        source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        cases=cases, exact_f64_bits=True, source_file_bytes_unchanged=True,
        entry_point=("installed -I -m radioinspector.legacy_rpc" if args.installed_module
                     else "owned absolute script with -I; algorithms from installed package"),
        shared_io_receipts=receipts,
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
