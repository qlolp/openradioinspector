"""Independent proto wire oracle: stdlib JSON/struct/varints, no protoc/runtime.

Only owned synthetic v1 contract inputs are used. Tags are read from the IDL so
Rust Prost derives and both portable client decoders are checked against it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[2]
IDL = Path(__file__).with_name("measurement_stream.proto")


def fields(message):
    body = re.search(r"message " + message + r"\s*\{([^}]+)\}", IDL.read_text()).group(1)
    return {name: int(tag) for name, tag in re.findall(r"\b(\w+)\s*=\s*(\d+)", body)}


FRAME, ARRAYS = fields("StreamFrame"), fields("SpectrumArrays")


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(tag, value): return varint(tag << 3) + varint(value)
def blob(tag, value): return varint((tag << 3) | 2) + varint(len(value)) + value
def fixed(tag, value): return varint((tag << 3) | 1) + struct.pack("<d", value)
def json_bytes(value): return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def frame(sequence, metadata, spectrum=None, version=1):
    result = integer(FRAME["protocol_version"], version) + integer(FRAME["sequence"], sequence) + blob(FRAME["envelope_json"], metadata)
    return result if spectrum is None else result + blob(FRAME["spectrum"], spectrum)


def spectrum(frequencies, levels):
    return blob(ARRAYS["frequencies_hz"], b"".join(varint(value) for value in frequencies)) + blob(ARRAYS["levels"], b"".join(struct.pack("<d", value) for value in levels))


def envelope(sequence, kind="sweep.received", payload=None):
    original = json.loads((ROOT / "contracts/fixtures/measurement-v1.json").read_text())
    if payload is None: payload = {"sweep": copy.deepcopy(original["sweep"])}
    return {"contract": "openradioinspector-platform", "contract_version": 1,
        "data": {"sequence": sequence, "event_type": kind, "occurred_at": "2026-11-11T12:00:00.223456Z",
                 "session_id": "fixture-сессия", "payload": payload}}


def case(name, value, typed, original=None):
    raw = original.encode() if original is not None else json_bytes(value)
    metadata = copy.deepcopy(value)
    frequencies, levels = [], []
    packed = None
    if typed:
        sweep = metadata["data"]["payload"]["sweep"]
        frequencies, levels = sweep.pop("frequencies_hz"), sweep.pop("levels")
        packed = spectrum(frequencies, levels)
    encoded = frame(value["data"]["sequence"], json_bytes(metadata) if typed else raw, packed)
    return {"name": name, "input_json": raw.decode(), "wire_hex": encoded.hex(), "canonical_hex": encoded.hex(),
            "sequence": str(value["data"]["sequence"]), "typed": typed,
            "metadata_json": (json_bytes(metadata) if typed else raw).decode(),
            "frequencies_hz": [str(value) for value in frequencies],
            "level_bits": [struct.pack(">d", value).hex() for value in levels]}


def emit():
    valid = [case("measurement-v1", envelope(1), True)]
    value = envelope(9007199254740993)
    value["unknown-envelope"] = {"é": "NFC", "é": "NFD"}
    value["data"]["payload"]["unknown"] = {"$serde_json::private::Number": "literal", "$serde_json::private::RawValue": "raw",
        "$ori::escaped::prefix": "literal prefix", "__proto__": {"polluted": True},
        "huge": 2**100, "float": float(2**100), "negative_zero": -0.0, "int_zero": 0,
        "text": "escaped \\ \" / 😀\n", "time": "2026-11-11 15:00:00.123456+03:00"}
    valid.append(case("large-sequence-and-opaque-metadata", value, True, json_bytes(value).decode().replace('"int_zero":0','"int_zero":-0')))
    value = envelope(2**64-1)
    value["data"]["payload"]["sweep"].update(frequencies_hz=[0, 2**53+1, 2**64-1], levels=[-0.0, float.fromhex("0x0.0000000000001p-1022"), 1.0000000000000002])
    valid.append(case("uint64-limits-and-f64-bits", value, True))
    value = envelope(4, "future.extension", {"unknown": {"value": 2**100, "unit": "original"}, "levels": [0, -0.0]})
    valid.append(case("unknown-event-generic", value, False, json.dumps(value, ensure_ascii=False, indent=1)))
    value = envelope(5, "observation.received", {"observation": json.loads((ROOT/"contracts/fixtures/measurement-v1.json").read_text())["observation"]})
    valid.append(case("receiver-original-unit-generic", value, False))
    value = envelope(6, "audio.segment", {"segment": {"id": "audio-😀", "sample_rate_hz": 48000, "channels": 1, "sample_count": 4800, "codec": "pcm_s16le"}})
    valid.append(case("audio-metadata-generic", value, False))
    value = envelope(7)
    value["data"]["payload"]["sweep"].update(frequencies_hz=[2**80, 2**80+1], levels=[-90.0, -89.0])
    valid.append(case("larger-than-uint64-hz-generic", value, False))
    value = envelope(8)
    value["data"]["payload"]["sweep"].update(frequencies_hz=[1, 2, 3], levels=[-90, 0, 1])
    valid.append(case("integer-level-kind-generic", value, False))
    value = envelope(9)
    value["data"]["payload"]["sweep"].update(frequencies_hz=[0]*512, levels=[0.0]*512)
    valid.append(case("packing-expansion-generic", value, False))
    value = envelope(10)
    value["data"]["payload"]["sweep"].update(levels=[-32.75,-0.0,-0.0,-32.75], first_peak_frequency_hz=100000002)
    valid.append(case("first-tied-peak-order", value, True))
    value = envelope(11)
    value["data"]["payload"]["sweep"].update(frequencies_hz=[1,2,3],levels=[1.0,float(2**60),-0.0])
    valid.append(case("integral-float-level-bits", value, True))

    first = valid[0]
    metadata = first["metadata_json"].encode()
    frequencies = [int(value) for value in first["frequencies_hz"]]
    levels = [struct.unpack(">d", bytes.fromhex(value))[0] for value in first["level_bits"]]
    split = spectrum(frequencies[:2], levels[:2]) + spectrum(frequencies[2:], levels[2:])
    unpacked = b"".join(integer(ARRAYS["frequencies_hz"], value) for value in frequencies) + b"".join(fixed(ARRAYS["levels"], value) for value in levels)
    for name, payload in (("split-packed-fields", split), ("unpacked-repeated-fields", unpacked)):
        changed = copy.deepcopy(first); changed.update(name=name, wire_hex=frame(1, metadata, payload).hex()); valid.append(changed)
    unknown = integer(100, 2**64-1) + blob(101, b"opaque") + varint((102<<3)|1) + b"12345678" + varint((103<<3)|5) + b"1234"
    changed = copy.deepcopy(first); changed.update(name="unknown-safe-protobuf-fields", wire_hex=(bytes.fromhex(first["wire_hex"])+unknown).hex()); valid.append(changed)
    changed = copy.deepcopy(first); changed.update(name="unknown-spectrum-fields", wire_hex=frame(1,metadata,spectrum(frequencies,levels)+unknown).hex()); valid.append(changed)

    base = bytes.fromhex(first["wire_hex"])
    packed = spectrum(frequencies, levels)
    malformed = {
        "duplicate-version": base+integer(1,1), "duplicate-sequence": base+integer(2,1),
        "duplicate-json": base+blob(3,metadata), "duplicate-spectrum": base+blob(4,packed),
        "version-unsupported": frame(1,metadata,packed,2), "sequence-zero": frame(0,metadata,packed),
        "version-overflow-u32": frame(1,metadata,packed,2**32),
        "sequence-mismatch": frame(2,metadata,packed), "invalid-utf8": frame(1,b"\xff"),
        "utf8-bom": frame(1,b"\xef\xbb\xbf"+first["input_json"].encode()),
        "bad-wire-type": varint((1<<3)|1)+b"12345678", "zero-field-tag": base+b"\x00",
        "unknown-group": base+varint((100<<3)|3), "unknown-wire-seven": base+varint((100<<3)|7),
        "unterminated-varint": b"\x80"*10, "overflow-varint": b"\xff"*9+b"\x02",
        "truncated-length": integer(1,1)+integer(2,1)+varint(26)+varint(2**64-1),
        "packed-double-tail": frame(1,metadata,blob(1,b"\x01")+blob(2,b"123")),
        "packed-varint-tail": frame(1,metadata,blob(1,b"\x80")+blob(2,struct.pack("<d",0.0))),
        "mismatched-array-length": frame(1,metadata,spectrum([1,2],[0.0])),
        "empty-arrays": frame(1,metadata,b""), "unordered-hz": frame(1,metadata,spectrum([2,1],[0.0,1.0])),
        "nan-level": frame(1,metadata,spectrum([1],[float("nan")])),
        "infinite-level": frame(1,metadata,spectrum([1],[float("inf")])),
        "ambiguous-metadata-arrays": frame(1,first["input_json"].encode(),packed),
        "typed-wrong-kind": frame(1,json_bytes(envelope(1,"future.extension",{})),packed),
    }
    bad = envelope(1); bad["contract_version"] = 2
    malformed["contract-version"] = frame(1,json_bytes(bad))
    bad = envelope(1); bad["contract_version"] = 1.0
    malformed["float-contract-version"] = frame(1,json_bytes(bad))
    bad = envelope(1); bad["contract"] = "other"
    malformed["wrong-contract"] = frame(1,json_bytes(bad))
    bad = envelope(1); bad["data"]["sequence"] = 1.0
    malformed["float-sequence"] = frame(1,json_bytes(bad))
    malformed["exponent-sequence"] = frame(1,first["input_json"].replace('"sequence":1','"sequence":1e0').encode())
    malformed["exponent-contract-version"] = frame(1,first["input_json"].replace('"contract_version":1','"contract_version":1e0').encode())
    malformed["escaped-header-integer-float"] = frame(1,first["input_json"].replace('"sequence":1','"\\u0073equence":1.0').encode())
    malformed["duplicate-json-key"] = frame(1,first["input_json"].replace('"sequence":1','"sequence":1,"\\u0073equence":1').encode())
    malformed["nonfinite-json-number"] = frame(1,first["input_json"].replace('"sequence":1','"sequence":1,"bad":1e400').encode())
    for field in ("sequence", "event_type", "occurred_at", "payload"):
        bad = envelope(1); del bad["data"][field]
        malformed["missing-event-"+field] = frame(1,json_bytes(bad))
    bad = envelope(1); bad["data"]["payload"] = []
    malformed["nonobject-payload"] = frame(1,json_bytes(bad))
    bad = envelope(1); del bad["data"]["payload"]["sweep"]["levels"]
    malformed["partial-generic-sweep-arrays"] = frame(1,json_bytes(bad))
    malformed["deep-json"] = frame(1,first["input_json"].replace('"sequence":1','"sequence":1,"deep":'+'['*129+'0'+']'*129).encode())
    return {"fixture_version":1,"synthetic":True,"protocol_version":1,"proto_sha256":hashlib.sha256(IDL.read_bytes()).hexdigest(),
        "valid":valid,"invalid":[{"name":name,"wire_hex":wire.hex()} for name,wire in malformed.items()]}


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify fixture freshness without rewriting it')
    args = parser.parse_args()
    path = ROOT / "contracts/fixtures/stream-v1.json"
    expected = json.dumps(emit(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.check:
        if not path.is_file() or path.read_text(encoding='utf-8') != expected:
            print('Measurement stream fixture is stale; regenerate it explicitly', file=sys.stderr)
            raise SystemExit(1)
    else:
        path.write_text(expected, encoding='utf-8')
