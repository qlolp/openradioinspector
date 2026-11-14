"""The published real-Core harness is mandatory and its proof cannot be partial."""

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from tests import run_release_checks as release
from tests.swift_version_fixtures import SUPPORTED_SWIFT_OUTPUTS, UNSUPPORTED_SWIFT_OUTPUTS
from tools.modernization import checks, client_interop


def summary(*, swift=False):
    web = {
        "state": "passed",
        "source_count": 1205,
        "exact_sources": True,
        "applied_acks": True,
        "forced_reconnect": True,
        "renderer_profile": "canvas",
        "renderers": {"spectrum": "Canvas fallback", "waterfall": "Canvas fallback"},
        "binary_subprotocol": "ori.measurements.v1.protobuf",
        "binary_frames": 1205,
    }
    native = (
        {
            "state": "passed",
            "source_count": 1205,
            "exact_sources": True,
            "sentinel_ack": True,
        }
        if swift
        else {"state": "not_requested"}
    )
    resumed = (
        {
            "state": "passed",
            "source_count": 1,
            "exact_sources": True,
            "sentinel_ack": True,
        }
        if swift
        else {"state": "not_requested"}
    )
    return {
        "schema_version": 1,
        "successful": True,
        "source_count": 1205,
        "functional_deadline_seconds": 30,
        "renderer_profile": "canvas",
        "candidate_sha256": "a" * 64,
        "core_joined": True,
        "immutable_binary": True,
        "committed_sweeps": 1207 if swift else 1206,
        "hardware_validation": "not_run",
        "performance_acceptance": "not_run",
        "clients": {"web": web, "swift": native, "swift_resumed": resumed},
    }


class ModernClientPlanTests(unittest.TestCase):
    def test_ci_prepares_the_locked_browser_on_every_existing_platform(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/modern-core.yml").read_text()
        self.assertIn(
            "os: [ubuntu-latest, windows-latest, macos-latest, ubuntu-24.04-arm]",
            workflow,
        )
        self.assertIn(
            "npx --no-install playwright install --with-deps chromium",
            workflow,
            "each modern CI host must prepare the locked Playwright browser",
        )
        self.assertIn("chromium.executablePath()", workflow)
        self.assertIn("CHROMIUM_PATH=", workflow)
        self.assertIn("statSync", workflow)
        self.assertIn(
            "command -v pkg-config >/dev/null 2>&1 && pkg-config --exists alsa",
            workflow,
        )
        self.assertIn("sudo apt-get install -y pkg-config libasound2-dev", workflow)
        self.assertLess(
            workflow.index("Build offline UI before compiling embedded Rust resources"),
            workflow.index("Run owned modernization release checks"),
        )
        for pinned in (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        ):
            self.assertIn(pinned, workflow)

    def test_every_profile_runs_the_actual_built_core_and_requires_the_selected_clients(
        self,
    ):
        root = Path("/prepared-checkout")
        directory = Path("/private-run")
        environment = {
            "PATH": "/prepared-tools",
            "CARGO_TARGET_DIR": str(directory / "rust-target"),
        }
        with (
            patch.object(
                checks,
                "tool",
                side_effect=lambda name, _environment: "/prepared-tools/" + name,
            ),
            patch.object(
                checks,
                "npm_command",
                return_value=["/prepared-tools/node", "/prepared-tools/npm-cli.js"],
            ),
        ):
            for swift in (False, True):
                with self.subTest(swift=swift):
                    plan = checks.commands(
                        root,
                        directory,
                        environment,
                        research_python="research-python",
                        swift=swift,
                    )
                    names = [check.name for check in plan]
                    self.assertIn(
                        "modern-core-client-interop",
                        names,
                        "modern release must execute the real Core-to-clients replay",
                    )
                    gate = plan[names.index("modern-core-client-interop")]
                    self.assertGreater(
                        names.index(gate.name), names.index("modern-rust-tests")
                    )
                    self.assertIn(
                        str(root / "tools/modernization/client_interop.py"),
                        gate.command,
                    )
                    self.assertIn(
                        str(
                            directory
                            / "rust-target"
                            / "debug"
                            / (
                                "ori-core.exe"
                                if sys.platform == "win32"
                                else "ori-core"
                            )
                        ),
                        gate.command,
                    )
                    self.assertEqual(
                        gate.command[gate.command.index("--count") + 1], "1205"
                    )
                    self.assertEqual(
                        gate.command[gate.command.index("--renderer") + 1], "canvas"
                    )
                    self.assertEqual(
                        gate.report, directory / "modern-core-client-interop.json"
                    )
                    self.assertEqual("--swift-builder" in gate.command, swift)
                    self.assertEqual(
                        gate.kind,
                        "client-interop-swift" if swift else "client-interop-web",
                    )

    def test_modern_result_validation_rejects_missing_frames_or_unrun_swift(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proof.json"
            for kind, swift in (
                ("client-interop-web", False),
                ("client-interop-swift", True),
            ):
                good = summary(swift=swift)
                path.write_text(json.dumps(good))
                self.assertIsNone(checks.count_tests(kind, "", path))
                changes = [
                    lambda value: value.update(source_count=1204),
                    lambda value: value.update(source_count=True),
                    lambda value: value.update(core_joined=False),
                    lambda value: value.update(immutable_binary=False),
                    lambda value: value["clients"]["web"].update(
                        forced_reconnect=False
                    ),
                    lambda value: value["clients"]["web"].update(source_count=1204),
                    lambda value: value["clients"]["web"].pop("exact_sources"),
                    lambda value: value.update(committed_sweeps=1205),
                    lambda value: value.update(renderer_profile="webgl2"),
                    lambda value: value["clients"]["web"].update(binary_frames=1204),
                    lambda value: value["clients"]["web"].update(binary_subprotocol=""),
                    lambda value: value["clients"]["web"]["renderers"].update(
                        waterfall="WebGL2 · incremental rows"
                    ),
                ]
                if swift:
                    changes += [
                        lambda value: value["clients"].update(
                            swift={"state": "not_requested"}
                        ),
                        lambda value: value["clients"]["swift_resumed"].update(
                            sentinel_ack=False
                        ),
                    ]
                for change in changes:
                    bad = deepcopy(good)
                    change(bad)
                    with self.subTest(kind=kind, bad=bad):
                        path.write_text(json.dumps(bad))
                        with self.assertRaises(ValueError):
                            checks.count_tests(kind, "", path)

    def test_owned_profile_retains_the_proof_and_marks_partial_replay_failed(self):
        for complete in (False, True):
            with (
                self.subTest(complete=complete),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)

                def plan(directory, _environment, complete=complete, **_kwargs):
                    proof = summary()
                    if not complete:
                        proof["clients"]["web"]["source_count"] = 1204
                    report = directory / "modern-core-client-interop.json"
                    write = (
                        "from pathlib import Path; Path("
                        + repr(str(report))
                        + ").write_text("
                        + repr(json.dumps(proof))
                        + ")"
                    )
                    tap = "# tests 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0"
                    return [
                        checks.Check(
                            "modern-unit-seed",
                            [sys.executable, "-c", "print(" + repr(tap) + ")"],
                            "node",
                        ),
                        checks.Check(
                            "modern-core-client-interop",
                            [sys.executable, "-c", write],
                            "client-interop-web",
                            report,
                        ),
                    ]

                with (
                    patch.object(release, "modern_commands", side_effect=plan),
                    redirect_stdout(io.StringIO()),
                ):
                    if complete:
                        release.release_checks(
                            modern=True, research_python=sys.executable, report_dir=root
                        )
                    else:
                        with self.assertRaises(release.ReleaseCheckError):
                            release.release_checks(
                                modern=True,
                                research_python=sys.executable,
                                report_dir=root,
                            )
                retained = next(root.glob("run-*"))
                result = json.loads((retained / "modern-results.json").read_text())
                self.assertEqual(result["successful"], complete)
                self.assertEqual(
                    result["checks"][-1]["status"], "passed" if complete else "failed"
                )
                self.assertIsNone(result["checks"][-1]["tests_run"])
                self.assertTrue(
                    (retained / "modern-core-client-interop.json").is_file()
                )


class ScientificClientProofTests(unittest.TestCase):
    def case(self):
        sweeps = [
            {"id": "interop-é", "frequencies_hz": [100000000, 100000001, 100000002]},
            {
                "id": "interop-e\u0301",
                "frequencies_hz": [100000000, 100000001, 100000002],
            },
        ]
        expected = [
            dict(
                sweep,
                interval={
                    "started_at": "2026-11-11T12:00:00Z",
                    "ended_at": "2026-11-11T12:00:00Z",
                },
                level_unit="dBm",
            )
            for sweep in sweeps
        ]
        bits = [
            struct.pack(">d", value).hex()
            for value in (-0.0, 1.0000000000000002, -100.125)
        ]
        return {
            "after": "9",
            "renderer_profile": "canvas",
            "sweeps": sweeps,
            "expected_sweeps": expected,
            "level_bits": bits,
        }

    def web(self, case):
        return {
            "opened": 1,
            "disconnected": False,
            "connections": [
                {
                    "after": "9",
                    "applied_at_connect": "9",
                    "protocol": "ori.measurements.v1.protobuf",
                    "binary_type": "arraybuffer",
                }
            ],
            "binary_frames": 2,
            "renderer_profile": "canvas",
            "renderers": {
                "spectrum": "Canvas fallback",
                "waterfall": "Canvas fallback",
            },
            "acks": ["10", "11"],
            "applied_sequences": ["10", "11"],
            "ack_after_apply": [
                {"sequence": "10", "applied": "10"},
                {"sequence": "11", "applied": "11"},
            ],
            "sources": [
                {
                    "id": sweep["id"],
                    "frequencies": sweep["frequencies_hz"],
                    "bits": case["level_bits"],
                    "interval": sweep["interval"],
                    "unit": sweep["level_unit"],
                }
                for sweep in case["expected_sweeps"]
            ],
        }

    def validator(self, name):
        value = getattr(client_interop, name, None)
        self.assertTrue(
            callable(value),
            "interop must validate actual client proof before reporting success",
        )
        return value

    def test_web_source_validation_rejects_missing_middle_data_and_changed_original_metadata(
        self,
    ):
        validate = self.validator("verify_web_proof")
        case = self.case()
        good = self.web(case)
        self.assertEqual(validate(case, good)["source_count"], 2)
        for change in [
            lambda proof: proof["sources"].pop(0),
            lambda proof: proof["sources"][0].update(bits=["0000000000000000"] * 3),
            lambda proof: proof["sources"][1].update(id="interop-é"),
            lambda proof: proof["sources"][0]["interval"].update(
                started_at="2026-11-11T12:00:00.000000Z"
            ),
            lambda proof: proof["sources"][0].update(unit="dB"),
            lambda proof: proof["ack_after_apply"][0].update(applied="9"),
            lambda proof: proof["connections"][0].update(protocol=""),
            lambda proof: proof["connections"][0].update(binary_type="blob"),
            lambda proof: proof.update(binary_frames=1),
            lambda proof: proof["renderers"].update(spectrum="WebGL2"),
            lambda proof: proof["renderers"].update(
                waterfall="Canvas fallback · graphics context lost"
            ),
        ]:
            bad = deepcopy(good)
            change(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate(case, bad)

    def test_standalone_webgl2_requires_actual_webgl_renderers(self):
        case = dict(self.case(), renderer_profile="webgl2")
        proof = self.web(case)
        proof.update(
            renderer_profile="webgl2",
            renderers={
                "spectrum": "WebGL2",
                "waterfall": "WebGL2 · incremental rows",
            },
        )
        self.assertEqual(
            client_interop.verify_web_proof(case, proof)["source_count"], 2
        )
        proof["renderers"]["waterfall"] = "Canvas fallback"
        with self.assertRaises(ValueError):
            client_interop.verify_web_proof(case, proof)

    def test_swift_proof_requires_every_exact_utf8_id_sequence_and_sentinel_ack(self):
        validate = self.validator("verify_swift_proof")
        case = self.case()
        good = {
            "sequences": ["10", "11"],
            "exact_utf8_ids": [
                list(item["id"].encode("utf-8")) for item in case["sweeps"]
            ],
            "preceding_acks_proven_by_sentinel": True,
        }
        self.assertEqual(validate(case, good)["source_count"], 2)
        for change in [
            lambda proof: proof.update(sequences=["10"]),
            lambda proof: proof["exact_utf8_ids"].__setitem__(
                1, proof["exact_utf8_ids"][0]
            ),
            lambda proof: proof.update(preceding_acks_proven_by_sentinel=False),
        ]:
            bad = deepcopy(good)
            change(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate(case, bad)


class ClientHarnessPreparationTests(unittest.TestCase):
    def test_swift_builder_accepts_supported_compiler_output_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "swift-build" / "out" / "Products" / "Debug-special-target"
            actual.mkdir(parents=True)
            binary = actual / (
                "ORIClientInterop.exe" if os.name == "nt" else "ORIClientInterop"
            )
            binary.write_bytes(b"owned test artifact")
            for name, output in SUPPORTED_SWIFT_OUTPUTS:
                with (
                    self.subTest(format=name),
                    patch.object(
                        client_interop,
                        "run_logged",
                        side_effect=[output, "", str(actual) + "\n"],
                    ),
                ):
                    try:
                        result = client_interop.build_swift_client(Path(sys.executable), root, {})
                    except ValueError as error:
                        self.fail(f"Supported compiler output was rejected: {error}")
                    self.assertEqual(
                        result,
                        binary.resolve(),
                    )

    def test_swift_builder_rejects_wrong_ambiguous_and_unrelated_versions_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, output in UNSUPPORTED_SWIFT_OUTPUTS:
                with self.subTest(format=name):
                    with patch.object(client_interop, "run_logged", return_value=output) as run:
                        with self.assertRaisesRegex(ValueError, "pinned 6.4.2 builder"):
                            client_interop.build_swift_client(Path(sys.executable), root, {})
                        self.assertEqual(run.call_count, 1, "invalid compiler output must prevent compilation")

    def test_failed_owned_child_forwards_a_bounded_tail_into_the_retained_stage_log(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            diagnostic = io.StringIO()

            def supervisor_failure(*_args, **_kwargs):
                (directory / "bounded-error.log").write_text(
                    "x" * 20000 + "\nowned diagnostic final line\n", encoding="utf-8"
                )
                raise release.ReleaseCheckError("controlled supervisor failure")

            with (
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(diagnostic),
                patch.object(
                    client_interop, "run_command", side_effect=supervisor_failure
                ),
                self.assertRaises(release.ReleaseCheckError),
            ):
                client_interop.run_logged(
                    ["owned-child"],
                    name="bounded-error",
                    environment=release.child_environment(),
                    directory=directory,
                    timeout=5,
                )
            self.assertEqual(output.getvalue(), "")
            self.assertIn("owned diagnostic final line", diagnostic.getvalue())
            self.assertLess(len(diagnostic.getvalue()), 17000)
            self.assertGreater((directory / "bounded-error.log").stat().st_size, 20000)

    def test_missing_requested_tool_retains_failed_report_and_never_starts_a_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(client_interop, "OwnedNode") as node,
                redirect_stdout(io.StringIO()),
                self.assertRaises(FileNotFoundError),
            ):
                client_interop.main(
                    [
                        "--candidate",
                        sys.executable,
                        "--node",
                        str(root / "missing-node"),
                        "--artifacts",
                        str(root / "artifacts"),
                        "--report",
                        str(root / "modern-client.json"),
                    ]
                )
            node.assert_not_called()
            report = json.loads((root / "modern-client.json").read_text())
            self.assertFalse(report["successful"])
            self.assertEqual(report["clients"]["web"]["state"], "not_run")
            self.assertIn("FileNotFoundError", report["error"])

    def test_swift_builder_uses_reported_owned_xcode_layout_and_rejects_foreign_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "swift-build"
            actual = scratch / "out" / "Products" / "Debug-special-target"
            actual.mkdir(parents=True)
            binary = actual / (
                "ORIClientInterop.exe" if os.name == "nt" else "ORIClientInterop"
            )
            binary.write_bytes(b"owned test artifact")
            with patch.object(
                client_interop,
                "run_logged",
                side_effect=["Swift version 6.4.2\n", "", str(actual) + "\n"],
            ) as run:
                builder = Mock(spec=Path)
                builder.resolve.return_value = Path(sys.executable)
                builder.absolute.return_value = root / "swift-entry-point"
                self.assertEqual(
                    client_interop.build_swift_client(builder, root, {}),
                    binary.resolve(),
                )
            self.assertTrue(
                all(
                    call.args[0][0] == str(root / "swift-entry-point")
                    for call in run.call_args_list
                ),
                "multicall Swift must keep its trusted entry-point name",
            )
            self.assertIn("--show-bin-path", run.call_args_list[-1].args[0])
            outside = root / "foreign-build"
            outside.mkdir()
            for location in (
                str(outside),
                "relative/output",
                str(actual) + "\n" + str(outside),
            ):
                with (
                    self.subTest(location=location),
                    patch.object(
                        client_interop,
                        "run_logged",
                        side_effect=["Swift version 6.4.2\n", "", location],
                    ),
                    self.assertRaises(ValueError),
                ):
                    client_interop.build_swift_client(Path(sys.executable), root, {})


if __name__ == "__main__":
    unittest.main()
