"""Built-in frequency counter on archived sweeps, with explicit resolution.

Backlog item 7: «Встроенный частотомер» — измерение частоты с погрешностью
прибора. The original keeps this as ``MEAS_COUNTER_FC_RESULTCODE`` and
``MEAS_COUNTER_FC_STDOFFSET`` with a user-set
«Разрешение встроенного частотомера, Гц».

The counter measures the peak of one archived sweep inside an explicit
window. A parabolic interpolation of the three peak bins refines the
estimate only when the local raster is uniform; otherwise the peak bin
itself is the measurement. The stated uncertainty is an honest bound —
``max(resolution, raster) / 2`` — and never claims better than both the
requested resolution and the recorded raster. The archived source sweep
is never changed; the measurement is a derived record.
"""

from __future__ import annotations

from typing import Any

from .contracts import Sweep, to_primitive

MAX_COUNTER_REQUEST_BYTES = 8192
MAX_COUNTER_ID_LENGTH = 128
MAX_FREQUENCY_HZ = 9_007_199_254_740_991

#: Методика записи результата в каталоге ``measurement_results``.
COUNTER_METHODOLOGY = "frequency_counter"


class CounterIncompatibleError(ValueError):
    """The archived sweep cannot support the requested counter measurement."""


def validate_counter_request(payload: Any) -> dict[str, Any]:
    """Exact request shape: sweep, resolution and an explicit window."""
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "resolution_hz", "start_hz", "stop_hz"}:
        raise ValueError("Разрешены только поля sweep_id, resolution_hz, start_hz и stop_hz")
    sweep_id = payload["sweep_id"]
    if (not isinstance(sweep_id, str) or not sweep_id.strip()
            or len(sweep_id) > MAX_COUNTER_ID_LENGTH):
        raise ValueError("ID свипа должен быть непустой строкой не длиннее 128 символов")
    try:
        sweep_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ID свипа должен быть корректной строкой UTF-8") from exc
    resolution = payload["resolution_hz"]
    if isinstance(resolution, bool) or not isinstance(resolution, int) or resolution < 1:
        raise ValueError("resolution_hz должен быть положительным целым числом Гц")
    if resolution > MAX_FREQUENCY_HZ:
        raise ValueError("resolution_hz вне диапазона безопасных целых Hz")
    window = {}
    for field in ("start_hz", "stop_hz"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FREQUENCY_HZ:
            raise ValueError(f"{field} должен быть целым числом в диапазоне безопасных Hz")
        window[field] = value
    if window["start_hz"] >= window["stop_hz"]:
        raise ValueError("Окно измерения должно удовлетворять start_hz < stop_hz")
    return {"sweep_id": sweep_id, "resolution_hz": resolution, **window}


def calculate_frequency_counter(
    session_id: str, sweep: Sweep, *, resolution_hz: int, start_hz: int, stop_hz: int,
) -> dict[str, Any]:
    """Measure the peak frequency of one archived sweep inside the window.

    Parabolic interpolation applies only with a uniform local raster and a
    peak away from the window edges; every other case reports the peak bin
    itself. ``uncertainty_hz`` is the honest bound
    ``max(resolution_hz, raster_hz) / 2``; the archived bins are echoed
    through provenance only and never rewritten.
    """
    try:
        validate_counter_request({
            "sweep_id": sweep.id, "resolution_hz": resolution_hz,
            "start_hz": start_hz, "stop_hz": stop_hz,
        })
    except ValueError as exc:
        raise CounterIncompatibleError(str(exc)) from exc
    frequencies = sweep.frequencies_hz
    if any(type(f) is not int or not 0 <= f <= MAX_FREQUENCY_HZ for f in frequencies):
        raise CounterIncompatibleError("Частоты свипа выходят за диапазон безопасных целых Hz")
    if any(b < a for a, b in zip(frequencies, frequencies[1:])):
        raise CounterIncompatibleError("Частоты свипа должны идти в неубывающем порядке")
    inside = [index for index, hz in enumerate(frequencies) if start_hz <= hz <= stop_hz]
    if not inside:
        raise CounterIncompatibleError("Окно измерения не содержит ни одной точки свипа")
    # First maximum on ties: deterministic, documented.
    peak = max(inside, key=lambda index: (sweep.levels[index], -index))
    level = sweep.levels[peak]
    left = peak - 1 if peak - 1 in inside else None
    right = peak + 1 if peak + 1 in inside else None
    raster = 0
    if left is not None and right is not None:
        left_step = frequencies[peak] - frequencies[left]
        right_step = frequencies[right] - frequencies[peak]
        if left_step == right_step:
            raster = left_step
    measured = float(frequencies[peak])
    method = "peak_bin"
    if raster > 0:
        y_left, y_center, y_right = sweep.levels[left], level, sweep.levels[right]
        denominator = y_left - 2 * y_center + y_right
        if denominator != 0:
            shift = 0.5 * (y_left - y_right) / denominator
            # Clamp to the bin span: interpolation never extrapolates outside.
            shift = min(max(shift, -0.5), 0.5)
            measured = frequencies[peak] + shift * raster
            method = "parabolic"
    uncertainty = max(resolution_hz, raster) / 2.0
    return {
        "session_id": session_id, "stream_id": sweep.stream_id,
        "sweep": {
            "id": sweep.id, "interval": to_primitive(sweep.interval),
            "level_unit": sweep.level_unit,
            "settings": dict(sweep.settings),
        },
        "window": {"start_hz": start_hz, "stop_hz": stop_hz},
        "resolution_hz": resolution_hz, "raster_hz": raster,
        "method": method, "peak_index": peak,
        "peak_frequency_hz": frequencies[peak], "peak_level_dbm": level,
        "measured_frequency_hz": measured, "uncertainty_hz": uncertainty,
        "measurement_methodology": COUNTER_METHODOLOGY,
        "values": {
            "measured_frequency_hz": measured,
            "resolution_hz": float(resolution_hz),
            "uncertainty_hz": uncertainty,
        },
    }
