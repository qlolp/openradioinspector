"""Small independent vectors from the unchanged Python audio implementation."""

import wave
from datetime import datetime, timezone
from io import BytesIO

from radioinspector.audio_stream import pcm16_bytes
from radioinspector.phonogram import PhonogramRecorder

SAMPLE_CASES = {
    "clipping": [-2, -1, -0.5, -0.0, 0, 0.5, 1, 2],
    "adjacent": [
        -1.0000000000000002,
        -0.9999999999999999,
        -1 / 32767,
        1 / 32767,
        0.9999999999999999,
        1.0000000000000002,
    ],
    "booleans": [False, True],
    "numeric_strings": ["-1", " 0.5 ", "+1e-1"],
    "unicode_and_underscore_numbers": ["٠.٢٥", "-０.５", "0.1_25"],
    "rectangular": [[-1, 0], [0.5, 1]],
    "empty_nested": [[]],
}


def quantization_vectors():
    result = {}
    for name, samples in SAMPLE_CASES.items():
        recorder = PhonogramRecorder(
            clock=lambda: datetime(2026, 11, 11, 12, 0, 0, 123456, timezone.utc)
        )
        recorder.push(samples)
        result[name] = {
            "samples": samples,
            "pcm_hex": pcm16_bytes(samples).hex(),
            "buffered_seconds": recorder.buffered_seconds,
        }
    return result


def completed_recording(prefix, recorded):
    instants = iter(
        (
            datetime(2026, 11, 11, 12, 0, 0, 123456, timezone.utc),
            datetime(2026, 11, 11, 12, 0, 1, 654321, timezone.utc),
        )
    )
    recorder = PhonogramRecorder(clock=lambda: next(instants))
    recorder.push(prefix)
    before = recorder.status()
    started = recorder.start()
    recorder.push(recorded)
    stopped = recorder.stop()
    return {
        "before": before,
        "started": started,
        "status": recorder.status(),
        "summary": stopped.as_dict(),
        "wav": stopped.wav_bytes(),
    }


def wav_frames(value):
    with wave.open(BytesIO(value), "rb") as source:
        return (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
            source.readframes(source.getnframes()),
        )


def capture():
    """Regenerate the fixture from original source, including its provenance."""
    import hashlib
    import platform
    from pathlib import Path

    import numpy as np

    from radioinspector import audio_stream, phonogram

    root = Path(__file__).resolve().parents[3]
    recording = completed_recording(
        SAMPLE_CASES["clipping"] + SAMPLE_CASES["adjacent"],
        [0.0, 1.0000000000000002, -0.0, -0.25],
    )
    recording["wav_hex"] = recording.pop("wav").hex()
    invalid = {}
    too_deep = 0
    for _ in range(65):
        too_deep = [too_deep]
    for name, samples in {
        "ragged": [[0], [1, 2]],
        "none": [None],
        "bad_string": ["junk"],
        "nan_string": ["NaN"],
        "inf_string": ["inf"],
        "object": [{}],
        "mixed_ragged": [[], [[], [1]]],
        "invalid_string_repr": ["oops\u00a0\u2028"],
        "too_many_dimensions": too_deep,
    }.items():
        try:
            PhonogramRecorder().push(samples)
        except (ValueError, TypeError, OverflowError) as error:
            invalid[name] = {
                "samples": samples,
                "error_type": type(error).__name__,
                "message": str(error),
            }
        else:
            raise AssertionError("Original unexpectedly accepted " + name)
    result = {
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "source_sha256": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in (
                "radioinspector/phonogram.py",
                "radioinspector/audio_stream.py",
                "radioinspector/session_service.py",
            )
        },
        "limits": {
            "pre_history_seconds": phonogram.PRE_HISTORY_SECONDS,
            "max_recording_seconds": phonogram.MAX_PHONOGRAM_SECONDS,
            "max_push_samples": phonogram.MAX_PUSH_SAMPLES,
            "max_live_samples": audio_stream.MAX_BLOCK_SAMPLES,
            "max_subscribers": audio_stream.MAX_SUBSCRIBERS,
            "max_queued_blocks": audio_stream.MAX_QUEUED_BLOCKS,
        },
        "quantization": quantization_vectors(),
        "invalid_samples": invalid,
        "recording": recording,
        "hardware_validation": "not_run",
    }
    return result


if __name__ == "__main__":
    import argparse
    import hashlib
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proof", type=Path)
    args = parser.parse_args()
    began = datetime.now(timezone.utc).isoformat()
    result = capture()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.proof:
        args.proof.write_text(
            json.dumps(
                {
                    "actual_begin_utc": began,
                    "actual_end_utc": datetime.now(timezone.utc).isoformat(),
                    "oracle_fixture_sha256": hashlib.sha256(
                        args.output.read_bytes()
                    ).hexdigest(),
                    "source_sha256": result["source_sha256"],
                    "python_version": result["python_version"],
                    "numpy_version": result["numpy_version"],
                    "quantization_vectors": len(result["quantization"]),
                    "invalid_vectors": len(result["invalid_samples"]),
                    "hardware_validation": "not_run",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "quantization_vectors": len(result["quantization"]),
                "invalid_vectors": len(result["invalid_samples"]),
                "hardware_validation": result["hardware_validation"],
            }
        )
    )
