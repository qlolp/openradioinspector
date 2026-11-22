"""Build native reader test cases from the shared synthetic archive."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import zipfile


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def build(source, directory):
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payloads = {name: archive.read(name) for name in manifest["files"]}

    def write(name, info, data):
        with zipfile.ZipFile(directory / (name + ".orifield"), "x") as archive:
            for member, raw in data.items():
                receipt = info["files"][member]
                receipt.update(bytes=len(raw), sha256=sha256(raw).hexdigest())
                archive.writestr(member, raw)
            archive.writestr("manifest.json", encode(info))

    info = deepcopy(manifest)
    info["streams"][1]["settings"]["frequencies_hz"] = list(range(100000000, 100010000))
    write("long-plan", info, payloads)
    info = deepcopy(manifest)
    info["streams"][1]["id"] = info["streams"][0]["id"]
    write("foreign-inventory", info, payloads)
    write("missing-lf", deepcopy(manifest), payloads | {"track.geojson": payloads["track.geojson"].rstrip(b"\n")})
    original = payloads["measurements.ndjson"]
    data = original.replace(b"12:00:00.123456Z", b"15:00:00.123456789+03:00")
    data = data.replace(b"12:00:00.223456Z", b"12:00:00.123456790Z")
    write("nanos", deepcopy(manifest), payloads | {"measurements.ndjson": data})
    write("reversed", deepcopy(manifest), payloads | {"measurements.ndjson": data.replace(b"790Z", b"788Z")})

    header, sweep, observation = original.splitlines(keepends=True)
    info = deepcopy(manifest)
    count = 10001
    # The first sweep retains the ID referenced by the marker in the field log.
    rows = [header, sweep]
    for index in range(1, count):
        rows.append(sweep.replace("fixture-sweep-α".encode(), f"sweep-{index}".encode()))
    info["sweep_count"] = count
    info["files"]["measurements.ndjson"]["records"] = count + 1
    write("long-session", info, payloads | {"measurements.ndjson": b"".join(rows) + observation})


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
