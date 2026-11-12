"""Functional real-Core Web/Swift binary interop over disposable committed data."""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from contextlib import redirect_stdout
from itertools import pairwise
from pathlib import Path

from radioinspector.contracts import Sweep, to_primitive
from tests.run_release_checks import child_environment, run_command
from tools.modernization.api_parity import NodeSpec, OwnedNode, file_sha256

ROOT = Path(__file__).resolve().parents[2]
FUNCTIONAL_DEADLINE_SECONDS = 30
BINARY_SUBPROTOCOL = "ori.measurements.v1.protobuf"
RENDERERS = {
    "canvas": {"spectrum": "Canvas fallback", "waterfall": "Canvas fallback"},
    "webgl2": {"spectrum": "WebGL2", "waterfall": "WebGL2 · incremental rows"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sequence(value):
    require(
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and str(int(value)) == value
        and int(value) <= 2**64 - 1,
        "Client proof has a noncanonical UInt64 cursor",
    )
    return int(value)


def verify_web_proof(case, proof):
    require(isinstance(proof, dict), "Web proof must be an object")
    count = len(case["sweeps"])
    renderer = case["renderer_profile"]
    require(
        renderer in RENDERERS
        and proof.get("renderer_profile") == renderer
        and proof.get("renderers") == RENDERERS[renderer],
        "Web actual spectrum/waterfall renderers differ from the explicit profile",
    )
    require(
        type(proof.get("binary_frames")) is int and proof["binary_frames"] == count,
        "Web did not receive every source as a binary frame",
    )
    expected = [str(sequence(case["after"]) + index + 1) for index in range(count)]
    require(
        proof.get("applied_sequences") == expected,
        "Web applied frame sequence is incomplete",
    )
    rows = proof.get("sources")
    require(
        isinstance(rows, list) and len(rows) == count, "Web source frame count differs"
    )
    for index, (actual, original) in enumerate(zip(rows, case["expected_sweeps"])):
        require(isinstance(actual, dict), f"Web source {index} is not an object")
        wanted = {
            "id": original["id"],
            "frequencies": original["frequencies_hz"],
            "bits": case["level_bits"],
            "interval": original["interval"],
            "unit": original["level_unit"],
        }
        require(
            actual == wanted,
            f"Web original source metadata/bits differ at frame {index}",
        )
    opened, connections = proof.get("opened"), proof.get("connections")
    require(
        type(opened) is int
        and opened > 0
        and isinstance(connections, list)
        and len(connections) == opened,
        "Web binary connection proof is incomplete",
    )
    cursors = []
    for connection in connections:
        require(isinstance(connection, dict), "Web connection cursor proof is missing")
        require(
            connection.get("protocol") == BINARY_SUBPROTOCOL
            and connection.get("binary_type") == "arraybuffer",
            "Web did not negotiate the required binary transport",
        )
        cursor = connection.get("after")
        require(
            cursor == connection.get("applied_at_connect"),
            "Web reconnect skipped its applied cursor",
        )
        require(
            sequence(case["after"]) <= sequence(cursor) <= int(expected[-1]),
            "Web reconnect cursor is outside the replay",
        )
        cursors.append(cursor)
    require(cursors[0] == case["after"], "Web initial cursor differs")
    require(
        all(sequence(a) < sequence(b) for a, b in pairwise(cursors)),
        "Web reconnect did not advance from applied data",
    )
    acks, applied = proof.get("acks"), proof.get("ack_after_apply")
    require(
        isinstance(acks, list)
        and isinstance(applied, list)
        and len(acks) == len(applied),
        "Web applied ACK proof is incomplete",
    )
    require(
        all(type(item) is str for item in acks) and len(acks) == len(set(acks)),
        "Web ACKs are duplicated or malformed",
    )
    require(
        all(sequence(a) < sequence(b) for a, b in pairwise(acks)),
        "Web ACKs are not ordered",
    )
    require(
        bool(acks)
        and acks[-1] == expected[-1]
        and set(acks) <= set(expected)
        and set(expected) - set(acks) <= set(cursors[1:]),
        "Web ACKs do not cover every frame except an applied disconnected cursor",
    )
    require(
        all(
            item == {"sequence": ack, "applied": ack}
            for item, ack in zip(applied, acks)
        ),
        "Web ACK preceded consumer application",
    )
    reconnect = proof.get("disconnected") is True and opened >= 2
    if count > 17:
        require(reconnect, "Web forced reconnect was not exercised")
    return {
        "state": "passed",
        "source_count": count,
        "exact_sources": True,
        "applied_acks": True,
        "forced_reconnect": reconnect,
        "last_applied_sequence": expected[-1],
        "connections": opened,
        "binary_subprotocol": BINARY_SUBPROTOCOL,
        "binary_frames": count,
        "renderer_profile": renderer,
        "renderers": proof["renderers"],
    }


def verify_swift_proof(case, proof):
    require(isinstance(proof, dict), "Swift proof must be an object")
    expected = [
        str(sequence(case["after"]) + index + 1) for index in range(len(case["sweeps"]))
    ]
    require(proof.get("sequences") == expected, "Swift replay sequence is incomplete")
    require(
        proof.get("exact_utf8_ids")
        == [list(item["id"].encode("utf-8")) for item in case["expected_sweeps"]],
        "Swift original UTF-8 source identifiers differ",
    )
    require(
        proof.get("preceding_acks_proven_by_sentinel") is True,
        "Swift sentinel did not prove preceding ACKs",
    )
    return {
        "state": "passed",
        "source_count": len(expected),
        "exact_sources": True,
        "sentinel_ack": True,
        "last_applied_sequence": expected[-1],
    }


def verify_summary(value, *, swift):
    require(
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("successful") is True,
        "Real-Core client gate did not report validated success",
    )
    require(
        type(value.get("source_count")) is int and value["source_count"] == 1205,
        "Real-Core client gate must apply all 1205 source frames",
    )
    require(
        type(value.get("functional_deadline_seconds")) is int
        and value["functional_deadline_seconds"] == 30,
        "Real-Core client gate functional deadline differs",
    )
    require(
        value.get("renderer_profile") == "canvas",
        "Modern functional client gate must explicitly select Canvas",
    )
    require(
        value.get("core_joined") is True
        and value.get("immutable_binary") is True
        and isinstance(value.get("candidate_sha256"), str)
        and re.fullmatch("[a-f0-9]{64}", value["candidate_sha256"]) is not None,
        "Real-Core client gate did not retain immutable/joined ownership proof",
    )
    require(
        type(value.get("committed_sweeps")) is int
        and value["committed_sweeps"] == 1205 + (2 if swift else 1),
        "Real-Core client gate sentinel/source count differs",
    )
    require(
        value.get("hardware_validation")
        == value.get("performance_acceptance")
        == "not_run",
        "Functional client gate cannot claim hardware or performance acceptance",
    )
    clients = value.get("clients")
    require(
        isinstance(clients, dict) and set(clients) == {"web", "swift", "swift_resumed"},
        "Required client states are missing",
    )
    web = clients["web"]
    require(
        isinstance(web, dict)
        and web.get("state") == "passed"
        and type(web.get("source_count")) is int
        and web["source_count"] == 1205
        and all(
            web.get(name) is True
            for name in ("exact_sources", "applied_acks", "forced_reconnect")
        ),
        "Web source/application/reconnect proof is incomplete",
    )
    require(
        web.get("renderer_profile") == "canvas"
        and web.get("renderers") == RENDERERS["canvas"]
        and web.get("binary_subprotocol") == BINARY_SUBPROTOCOL
        and type(web.get("binary_frames")) is int
        and web["binary_frames"] == 1205,
        "Web binary negotiation/actual renderer proof is incomplete",
    )
    for name, count in (("swift", 1205), ("swift_resumed", 1)):
        proof = clients[name]
        require(isinstance(proof, dict), "Swift client state is missing")
        if swift:
            require(
                proof.get("state") == "passed"
                and type(proof.get("source_count")) is int
                and proof["source_count"] == count
                and proof.get("exact_sources") is True
                and proof.get("sentinel_ack") is True,
                "Required Swift replay/resume proof is incomplete",
            )
        else:
            require(
                proof == {"state": "not_requested"},
                "Unexpected partial Swift proof in Web-only gate",
            )


def run_logged(command, *, name, environment, directory, timeout):
    # Keep 1205-element SDK proofs in owned files, not parent tool/CI stdout.
    try:
        with (
            (directory / (name + "-transcript.log")).open(
                "w", encoding="utf-8"
            ) as output,
            redirect_stdout(output),
        ):
            return run_command(
                command,
                name=name,
                environment=environment,
                directory=directory,
                timeout=timeout,
            )
    except BaseException:
        # The release report retains the outer stage log, not private build
        # trees. Keep the useful failure tail there without dumping SDK vectors.
        try:
            with (directory / (name + ".log")).open("rb") as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 16384))
                tail = log.read(16384).decode("utf-8", errors="replace")
            print(
                f"{name} failed; final owned log bytes:\n{tail}",
                file=sys.stderr,
                flush=True,
            )
        except OSError as error:
            print(
                f"{name} failure log unavailable: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
        raise


def build_swift_client(builder, directory, environment):
    # Swift dispatches from its executable name; resolving the final symlink
    # would turn a trusted `swift` entry point into `swift-driver` on Linux.
    require(
        builder.resolve(strict=True).is_file(), "Requested Swift builder is not a file"
    )
    builder = builder.absolute()
    version = run_logged(
        [str(builder), "--version"],
        name="swift-builder-version",
        environment=environment,
        directory=directory,
        timeout=30,
    )
    require(
        re.search(r"^(?:Apple )?Swift version 6\.4\.2(?:\s|$)", version, re.MULTILINE)
        is not None,
        "Swift helper requires the pinned 6.4.2 builder",
    )
    scratch = directory / "swift-build"
    command = [
        str(builder),
        "build",
        "--package-path",
        str(ROOT / "tools/modernization/swift-client"),
        "--scratch-path",
        str(scratch),
        "-j",
        "1",
    ]
    run_logged(
        command + ["--product", "ORIClientInterop"],
        name="swift-build",
        environment=environment,
        directory=directory,
        timeout=600,
    )
    location = run_logged(
        command + ["--show-bin-path"],
        name="swift-bin-path",
        environment=environment,
        directory=directory,
        timeout=30,
    )
    lines = [line.strip() for line in location.splitlines() if line.strip()]
    require(
        len(lines) == 1 and Path(lines[0]).is_absolute(),
        "Swift builder did not report one absolute binary directory",
    )
    binary_directory = Path(lines[0]).resolve(strict=True)
    require(
        binary_directory.is_relative_to(scratch.resolve(strict=True)),
        "Swift builder output escaped its owned scratch directory",
    )
    binary = binary_directory / (
        "ORIClientInterop.exe" if os.name == "nt" else "ORIClientInterop"
    )
    require(
        binary.is_file(),
        "Swift builder did not produce the requested interop executable",
    )
    return binary.resolve(strict=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    native = parser.add_mutually_exclusive_group()
    native.add_argument("--swift-client", type=Path)
    native.add_argument("--swift-builder", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        help="Compact validated report retained by the modern profile",
    )
    parser.add_argument("--count", type=int, default=1205)
    parser.add_argument(
        "--renderer",
        choices=RENDERERS,
        default="webgl2",
        help="Explicit browser rendering profile; the modern functional gate uses canvas",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 1205:
        parser.error("--count must be between 1 and 1205")
    directory = args.artifacts.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "successful": False,
        "source_count": args.count,
        "functional_deadline_seconds": FUNCTIONAL_DEADLINE_SECONDS,
        "renderer_profile": args.renderer,
        "clients": {
            "web": {"state": "not_run"},
            "swift": {
                "state": "not_run"
                if args.swift_client or args.swift_builder
                else "not_requested"
            },
            "swift_resumed": {
                "state": "not_run"
                if args.swift_client or args.swift_builder
                else "not_requested"
            },
        },
        "hardware_validation": "not_run",
        "performance_acceptance": "not_run",
    }
    summary_path = args.report.resolve() if args.report else None
    details = {}
    core = None
    active_client = None
    try:
        binary = args.candidate.resolve(strict=True)
        require(binary.is_file(), "Requested Core candidate is not a file")
        digest = file_sha256(binary)
        report["candidate_sha256"] = digest
        node = args.node.resolve(strict=True)
        require(node.is_file(), "Requested Node executable is not a file")
        environment = child_environment()
        swift_client = (
            build_swift_client(args.swift_builder, directory, environment)
            if args.swift_builder
            else (args.swift_client.resolve(strict=True) if args.swift_client else None)
        )
        with OwnedNode(NodeSpec("core", binary, ROOT), directory / "node") as core:
            capabilities = core.request("GET", "/api/v1/capabilities").json()["data"]
            require(
                isinstance(
                    capabilities["features"].get("measurement_binary_stream"), dict
                ),
                "Requested immutable Core does not advertise binary transport",
            )
            fixture = json.loads(
                (ROOT / "contracts/fixtures/measurement-v1.json").read_text()
            )
            source = fixture["session"]
            created = core.request(
                "POST",
                "/api/v1/sessions",
                {
                    "name": "Owned real-Core client interoperability",
                    "streams": [
                        {
                            key: value
                            for key, value in stream.items()
                            if key not in ("id", "session_id", "state")
                        }
                        for stream in source["streams"]
                    ],
                },
            )
            if created.status != 201:
                raise AssertionError((created.status, created.body))
            session = created.json()["data"]
            project = core.request(
                "POST",
                "/api/v1/monitoring-projects",
                {
                    "name": "Owned unarmed binary interop",
                    "session_id": session["id"],
                    "site": "Synthetic fixture; no physical device",
                },
            )
            if project.status != 201:
                raise AssertionError((project.status, project.body))
            after = core.request("GET", "/api/v1/health").json()["data"][
                "last_event_id"
            ]
            stream = next(
                item
                for item in session["streams"]
                if item["role"] == "spectrum_analyzer"
            )
            sweeps = []
            levels = [-0.0, 1.0000000000000002, -100.125]
            for index in range(args.count):
                sweep = dict(
                    fixture["sweep"],
                    id="interop-" + ["é", "e\u0301"][index % 2] + str(index),
                    stream_id=stream["id"],
                    frequencies_hz=[100000000, 100000001, 100000002],
                    levels=levels,
                    settings={"synthetic": True},
                )
                sweep.pop("session_id", None)
                sweeps.append(sweep)
            case = {
                "url": core.url,
                "session_id": session["id"],
                "project_id": project.json()["data"]["id"],
                "after": str(after),
                "renderer_profile": args.renderer,
                "sweeps": sweeps,
                # The public input contract accepts offsets and stores UTC.
                # Compare clients to the existing Python producer contract,
                # retaining exactly its UTC spelling and microseconds.
                "expected_sweeps": [
                    to_primitive(Sweep.from_dict(dict(sweep, session_id=session["id"])))
                    for sweep in sweeps
                ],
                "request_bodies": [
                    json.dumps(
                        {"items": sweeps[offset : offset + 100]},
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    for offset in range(0, len(sweeps), 100)
                ],
                "level_bits": [struct.pack(">d", value).hex() for value in levels],
            }
            vectors = directory / "vectors.json"
            vectors.write_text(
                json.dumps(case, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
            active_client = "web"
            report["clients"][active_client] = {"state": "running"}
            run_logged(
                [
                    str(node),
                    str(ROOT / "tools/modernization/client_interop_web.mjs"),
                    str(vectors),
                    str(directory),
                ],
                name="web",
                environment=environment,
                directory=directory,
                timeout=60,
            )
            web_proof = json.loads((directory / "web.json").read_text(encoding="utf-8"))
            details["web"] = web_proof
            report["clients"]["web"] = verify_web_proof(case, web_proof)
            active_client = None
            expected_last = after + len(sweeps)
            require(
                core.request("GET", "/api/v1/health").json()["data"]["last_event_id"]
                == expected_last,
                "Client traffic changed the expected committed event count",
            )
            # Receiving this sentinel proves the SDK ACKed the preceding frame:
            # the production Core allows only one outstanding binary frame.
            sentinel = dict(sweeps[-1], id="interop-sentinel")
            appended = core.request(
                "POST", f"/api/v1/sessions/{session['id']}/sweeps", sentinel
            )
            require(appended.status == 201, "Core rejected the first ACK sentinel")
            if swift_client:
                active_client = "swift"
                report["clients"][active_client] = {"state": "running"}
                swift = run_logged(
                    [str(swift_client), str(vectors)],
                    name="swift",
                    environment=environment,
                    directory=directory,
                    timeout=FUNCTIONAL_DEADLINE_SECONDS,
                )
                proof = json.loads(swift)
                details["swift"] = proof
                report["clients"]["swift"] = verify_swift_proof(case, proof)
                active_client = None
                (directory / "swift.json").write_text(swift, encoding="utf-8")
                resumed = dict(
                    case,
                    after=str(expected_last),
                    sweeps=[sentinel],
                    expected_sweeps=[
                        to_primitive(
                            Sweep.from_dict(dict(sentinel, session_id=session["id"]))
                        )
                    ],
                )
                resume_vectors = directory / "resume-vectors.json"
                resume_vectors.write_text(
                    json.dumps(resumed, ensure_ascii=False, allow_nan=False),
                    encoding="utf-8",
                )
                second_sentinel = dict(sentinel, id="interop-final-sentinel")
                appended = core.request(
                    "POST", f"/api/v1/sessions/{session['id']}/sweeps", second_sentinel
                )
                require(
                    appended.status == 201, "Core rejected the resumed ACK sentinel"
                )
                active_client = "swift_resumed"
                report["clients"][active_client] = {"state": "running"}
                resumed_swift = run_logged(
                    [str(swift_client), str(resume_vectors)],
                    name="swift-resumed",
                    environment=environment,
                    directory=directory,
                    timeout=FUNCTIONAL_DEADLINE_SECONDS,
                )
                proof = json.loads(resumed_swift)
                details["swift_resumed"] = proof
                report["clients"]["swift_resumed"] = verify_swift_proof(resumed, proof)
                active_client = None
                (directory / "swift-resumed.json").write_text(
                    resumed_swift, encoding="utf-8"
                )
            else:
                details["swift"] = {"state": "not_requested"}
            report["committed_sweeps"] = len(sweeps) + (2 if swift_client else 1)
        require(core.joined, "Owned Core did not join")
        require(
            file_sha256(binary) == digest, "Core candidate changed during client proof"
        )
        report.update(successful=True, core_joined=True, immutable_binary=True)
    except BaseException as error:
        if active_client:
            report["clients"][active_client]["state"] = "failed"
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if core is not None:
            report["core_joined"] = core.joined
        (directory / "report.json").write_text(
            json.dumps({**details, **report}, indent=2) + "\n", encoding="utf-8"
        )
        if summary_path:
            summary_path.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
    print(
        json.dumps(
            {
                "successful": report["successful"],
                "committed_sweeps": report["committed_sweeps"],
                "report": str(directory / "report.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
