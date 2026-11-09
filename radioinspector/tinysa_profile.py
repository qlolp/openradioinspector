"""Conservative application policy, not a tinySA hardware specification.

The limits deliberately cover only the existing receive workflow. Firmware
identity is checked separately, before any setup command is sent. Historical
acceptance is evidence for a profile, never validation of the current device.
"""
from __future__ import annotations

from typing import Any, Mapping

PROFILE_ID = "tinysa-ultra-zs405-receive-v1"
FIRMWARE = "tinySA4_v1.4-235-gea71dfd"
HARDWARE_VERSION_LINE = "HW Version:V0.4.5.1"
MIN_FREQUENCY_HZ = 100_000
MAX_FREQUENCY_HZ = 800_000_000
MIN_POINTS = 2
MAX_POINTS = 290
RBW_REQUESTS_HZ = (10_000, 30_000)


def profile_description() -> dict[str, Any]:
    """Return fresh JSON data; callers must not mutate shared policy."""
    return {
        "id": PROFILE_ID,
        "scope": "application_receive_policy",
        "model": "tinySA Ultra ZS405",
        "firmware": FIRMWARE,
        "frequency_min_hz": MIN_FREQUENCY_HZ,
        "frequency_max_hz": MAX_FREQUENCY_HZ,
        "points_min": MIN_POINTS,
        "points_max": MAX_POINTS,
        "rbw_requests_hz": list(RBW_REQUESTS_HZ),
        "extended_range": False,
        "lna_enable": False,
        "generator_enable": False,
        "settings_readback": False,
        "hardware_validation": "not_run",
    }


def validate_receive_settings(settings: Mapping[str, Any]) -> dict[str, int]:
    """Validate before port acquisition; never clamp or reinterpret a request."""
    if settings.get("receive_profile", PROFILE_ID) != PROFILE_ID:
        raise ValueError(f"tinySA: неизвестный receive_profile; поддержан {PROFILE_ID}")
    values = {}
    for name, default, minimum, maximum in (
        ("start_hz", 88_000_000, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ),
        ("stop_hz", 108_000_000, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ),
        ("points", 201, MIN_POINTS, MAX_POINTS),
        ("rbw_hz", 10_000, min(RBW_REQUESTS_HZ), max(RBW_REQUESTS_HZ)),
    ):
        value = settings.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"tinySA {name}: политика {PROFILE_ID} требует целое {minimum}…{maximum}")
        values[name] = value
    if values["stop_hz"] <= values["start_hz"]:
        raise ValueError("stop_hz должно быть больше start_hz")
    if values["rbw_hz"] not in RBW_REQUESTS_HZ:
        raise ValueError(f"tinySA rbw_hz: поддержаны запросы {RBW_REQUESTS_HZ}")
    return values


def matches_identity(info: str, version: str) -> bool:
    # Exact model line and firmware token, not a substring accepting Ultra+
    # or arbitrary firmware merely because it starts with tinySA4_.
    return (
        any(line.strip().casefold() == "tinysa ultra zs405" for line in info.splitlines())
        and tuple(version.strip().splitlines()) in (
            (FIRMWARE,), (FIRMWARE, HARDWARE_VERSION_LINE),
        )
    )
