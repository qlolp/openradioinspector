"""Behavioral generation/freshness tests; no source-text assertions."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools/contracts/generate.py"
SCHEMA = ROOT / "contracts/generated/openapi-v1.json"


class GeneratorTests(unittest.TestCase):
    def run_generator(self, output, source=SCHEMA, check=False):
        command = [sys.executable, str(GENERATOR), "--schema-source", str(source), "--output-root", str(output)]
        if check:
            command.append("--check")
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def test_regeneration_is_order_independent_and_check_never_rewrites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = self.run_generator(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            baseline = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(self.run_generator(root, check=True).returncode, 0)
            # Removing an extension from the input changes the artifact fingerprint
            # and outputs. Checking must detect that without mutating any file.
            source = root / "source.json"
            doc = json.loads(SCHEMA.read_text())
            doc["info"]["description"] = "controlled schema revision"
            source.write_text(json.dumps(doc), encoding="utf-8")
            check = self.run_generator(root, source, check=True)
            self.assertEqual(check.returncode, 1, check.stderr)
            self.assertEqual({p: (root / p).read_bytes() for p in baseline}, baseline)
            target = root / "contracts/generated/typescript/models.ts"
            target.write_text("stale output", encoding="utf-8")
            stamp = target.stat().st_mtime_ns
            self.assertEqual(self.run_generator(root, check=True).returncode, 1)
            self.assertEqual(target.read_text(), "stale output")
            self.assertEqual(target.stat().st_mtime_ns, stamp)
            self.assertEqual(self.run_generator(root).returncode, 0)
            self.assertEqual({p: (root / p).read_bytes() for p in baseline}, baseline)
            # Canonical object ordering must not depend on Python-vs-JSON loading.
            def reverse_keys(value):
                if isinstance(value, dict):
                    return {key: reverse_keys(item) for key, item in reversed(list(value.items()))}
                if isinstance(value, list):
                    return [reverse_keys(item) for item in value]
                return value
            reversed_doc = reverse_keys(json.loads(SCHEMA.read_text()))
            source.write_text(json.dumps(reversed_doc), encoding="utf-8")
            self.assertEqual(self.run_generator(root, source, check=True).returncode, 0)

    def test_shared_measurements_satisfy_published_schemas(self):
        import jsonschema
        doc = json.loads(SCHEMA.read_text())
        jsonschema.Draft202012Validator.check_schema(doc)
        fixture = json.loads((ROOT / "contracts/fixtures/measurement-v1.json").read_text())
        for key, name in [("sweep", "Sweep"), ("observation", "ReceiverObservation")]:
            validator = jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": doc["components"]})
            validator.validate(fixture[key])


if __name__ == "__main__":
    unittest.main()
