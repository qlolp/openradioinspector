"""Read-only reference envelopes and RF Explorer single-window power estimates."""

from __future__ import annotations

import math

from .contracts import Sweep, to_primitive
from .measurements import channel_power_dbm
from .spectral_mask import MAX_FREQUENCY_HZ, MaskIncompatibleError
from .sweep_comparison import MAX_COMPARISON_POINTS
from .sweep_settings import acquisition_settings, number, same_json

MAX_RESEARCH_REQUEST_BYTES = 8192
RF_EXPLORER = "rf-explorer-3g-serial"


class BandPowerUnavailable(ValueError):
    """The archived frame cannot support a band power estimate."""


def _sweep_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("ID свипа должен содержать от 1 до 128 символов")
    return value


def tolerance_request(payload):
    fields = {"reference_id", "target_id", "upper_deviation_db", "lower_deviation_db"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Нужны reference_id, target_id, upper_deviation_db и lower_deviation_db")
    for field in ("upper_deviation_db", "lower_deviation_db"):
        if not number(payload[field]) or payload[field] < 0:
            raise ValueError(f"{field} должен быть конечным неотрицательным числом dB")
    return (_sweep_id(payload["reference_id"]), _sweep_id(payload["target_id"]),
            float(payload["upper_deviation_db"]), float(payload["lower_deviation_db"]))


def band_power_request(payload):
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "start_hz", "stop_hz"}:
        raise ValueError("Нужны sweep_id, start_hz и stop_hz")
    start, stop = payload["start_hz"], payload["stop_hz"]
    if type(start) is not int or type(stop) is not int or not 0 <= start < stop <= MAX_FREQUENCY_HZ:
        raise ValueError("Границы полосы должны быть возрастающими целыми Hz")
    return _sweep_id(payload["sweep_id"]), start, stop


def _rf_window(sweep: Sweep) -> None:
    """Check the recorded readback and complete measured grid, including fixed-window records."""
    settings, hz = sweep.settings, sweep.frequencies_hz
    if (settings.get("adapter") != RF_EXPLORER
            or settings.get("receive_profile") != "rf-explorer-3g-combo-standard-1.34"
            or settings.get("device_mode") != "spectrum_analyzer"
            or settings.get("settings_readback") is not True
            or settings.get("settings_source") != "device_readback"
            or settings.get("timing_source") != "host_frame_read"
            or type(settings.get("simulated")) is not bool):
        raise ValueError("Требуется сохранённое окно RF Explorer с подтверждёнными настройками прибора")
    identity = settings.get("device_identity")
    inputs = {"main": "left", "expansion": "right"}
    if (not isinstance(identity, dict) or identity.get("main_model") != 3 or identity.get("expansion_model") != 5
            or identity.get("firmware") != "01.34" or not isinstance(settings.get("active_board"), str)
            or settings["active_board"] not in inputs
            or settings.get("active_input") != inputs[settings["active_board"]]):
        raise ValueError("Не подтверждены модель RF Explorer и активный вход")
    rbw = settings.get("rbw_hz")
    if type(rbw) is not int or not 3_000 <= rbw <= 600_000:
        raise ValueError("Требуется RBW из ответа RF Explorer в герцах")
    step = settings.get("step_hz")
    if (type(step) is not int or step <= 0 or type(settings.get("points")) is not int
            or not 16 <= len(hz) == settings["points"] <= 255
            or type(settings.get("start_hz")) is not int or settings["start_hz"] != hz[0]
            or type(settings.get("stop_hz")) is not int or settings["stop_hz"] != hz[-1]
            or any(f != hz[0] + i * step for i, f in enumerate(hz))):
        raise ValueError("Сетка исходного окна неполна или расходится с ответом прибора")
    detector = settings.get("detector_code")
    modes = ("normal", "max", "avg", "overwrite", "max_hold", "max_historical")
    if (type(detector) is not int or not 0 <= detector < len(modes)
            or settings.get("detector") != modes[detector]
            or settings.get("artifact_kind") != ("raw_spectrum" if detector == 0 else "processed_trace")):
        raise ValueError("Отсутствуют согласованные режим детектора и тип исходной трассы")
    offset = settings.get("offset_db")
    if (type(offset) is not int or not -999 <= offset <= 9999
            or settings.get("level_encoding") != "dBm = offset_db - u8 / 2"
            or any(not 0 <= offset - level <= 127.5 or not ((offset - level) * 2).is_integer()
                   for level in sweep.levels)):
        raise ValueError("Уровни не соответствуют записанному смещению и шагу RF Explorer")


def _envelope_settings(sweep: Sweep):
    if (sweep.level_unit != "dBm" or len(sweep.levels) > MAX_COMPARISON_POINTS
            or any(not 0 <= f <= MAX_FREQUENCY_HZ for f in sweep.frequencies_hz)):
        raise MaskIncompatibleError("Нужен архивный dBm-свип с частотами в целых Hz")
    try:
        settings = acquisition_settings(sweep.settings)
        if settings.get("adapter") == RF_EXPLORER:
            _rf_window(sweep)
            # Axes vary between tiles. The exact overlap is joined below. Pass
            # bookkeeping travels with both originals; all other extensions
            # remain acquisition settings and must compare equal.
            for key in ("start_hz", "stop_hz"):
                del settings[key]
            if "survey" in settings:
                survey = settings.pop("survey")
                if not isinstance(survey, dict):
                    raise ValueError("Некорректные метаданные прохода")
                known = {"start_hz", "stop_hz", "step_hz", "pass", "pass_id", "tile", "tiles"}
                extra = {key: value for key, value in survey.items() if key not in known}
                if extra:
                    settings["survey"] = extra
        return settings
    except ValueError as exc:
        raise MaskIncompatibleError(str(exc)) from exc


def calculate_tolerance_envelope(session_id, reference, target, upper, lower):
    if (target.session_id != session_id or reference.session_id != session_id
            or target.stream_id != reference.stream_id):
        raise MaskIncompatibleError("Эталон и исходный свип должны принадлежать одному сеансу и потоку")
    if not same_json(_envelope_settings(reference), _envelope_settings(target)):
        raise MaskIncompatibleError("Настройки измерения эталона и исходного свипа различаются")

    bins = []
    counts = {"above": 0, "below": 0, "within": 0, "uncovered": 0}
    ref_index = 0
    for i, (frequency, level) in enumerate(zip(target.frequencies_hz, target.levels)):
        while ref_index < len(reference.levels) and reference.frequencies_hz[ref_index] < frequency:
            ref_index += 1
        matched = low = high = delta = None
        classification = "uncovered"
        if ref_index < len(reference.levels) and reference.frequencies_hz[ref_index] == frequency:
            matched = ref_index
            baseline = reference.levels[ref_index]
            low, high, delta = baseline - lower, baseline + upper, level - baseline
            if not all(math.isfinite(v) for v in (low, high, delta)):
                raise MaskIncompatibleError("Уровень допуска выходит за конечный диапазон")
            classification = "below" if level < low else "above" if level > high else "within"
            ref_index += 1
        counts[classification] += 1
        bins.append({"target_index": i, "reference_index": matched, "frequency_hz": frequency,
                     "lower_dbm": low, "upper_dbm": high, "delta_db": delta,
                     "classification": classification})
    return {"session_id": session_id, "stream_id": target.stream_id, "level_unit": "dBm", "delta_unit": "dB",
            "upper_deviation_db": upper, "lower_deviation_db": lower, "reference": to_primitive(reference),
            "target": to_primitive(target), "bins": bins, "counts": counts}


def calculate_band_power(sweep, start_hz, stop_hz):
    try:
        _rf_window(sweep)
    except ValueError as exc:
        raise BandPowerUnavailable(str(exc)) from exc
    settings = sweep.settings
    if sweep.level_unit != "dBm" or settings["detector"] != "normal":
        raise BandPowerUnavailable("Нужен один исходный dBm-свип RF Explorer в режиме Normal")
    if settings["step_hz"] > settings["rbw_hz"]:
        raise BandPowerUnavailable("Шаг превышает RBW, в выбранной полосе остаются промежутки")
    try:
        first = sweep.frequencies_hz.index(start_hz)
        last = sweep.frequencies_hz.index(stop_hz)
    except ValueError:
        raise BandPowerUnavailable("Обе границы полосы должны совпадать с отсчётами одного окна") from None
    section = slice(first, last + 1)
    value = channel_power_dbm(sweep.frequencies_hz[section], sweep.levels[section], settings["rbw_hz"])
    return {"source": to_primitive(sweep), "start_index": first, "stop_index": last,
            "start_hz": start_hz, "stop_hz": stop_hz, "rbw_hz": settings["rbw_hz"],
            "value": value, "unit": "dBm", "integration": "trapezoid_over_actual_grid",
            "enbw_correction": "not_calibrated", "uncertainty_db": None, "hardware_validation": "not_run"}
