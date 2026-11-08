"""Measurement results bound to an explicit methodology, instrument and session.

Third conclusion of the methodology catalog (docs/measurement-methods.md):
the methodology is a field of the result, never a property of the storage
format. Values and the result code are separate entities, as the original
``*_RESULT`` / ``*_RESULTCODE`` tables are: the code records whether the
measurement met its norm, not measured at all, or produced values.

This module owns the catalog and the record shape only. The archive schema,
the service and the clients attach provenance (session, stream, interval);
no measurement is executed here.
"""

from __future__ import annotations

import copy
from typing import Any

from .contracts import to_primitive, utc_now
from .sweep_references import normalize_reference_name

MAX_RESULTS_PER_REQUEST = 256
MAX_VALUES_PER_RESULT = 32
MAX_VALUE_NAME_LENGTH = 64
MAX_FREQUENCY_HZ = 9_007_199_254_740_991

#: Код результата — независимый от значений признак годности (как *_RESULTCODE).
RESULT_CODES = ("norm", "not_norm", "not_measured")

#: Методики, реализованные в OpenRadioInspector (см. docs/measurement-methods.md §1).
#: Каждая задаёт точный набор числовых полей результата и единицу измерения.
METHODOLOGIES: dict[str, dict[str, Any]] = {
    "beta2_bandwidth": {
        "title": "Ширина занимаемых частот методом β/2",
        "original_table": "MEAS_B2_RESULT",
        "unit": "Hz",
        "fields": ("bandwidth_hz", "lower_edge_hz", "upper_edge_hz", "center_hz"),
    },
    "bandwidth_at_level": {
        "title": "Ширина полосы по уровню −N дБ",
        "original_table": "MEAS_B2_30DB_RESULT",
        "unit": "Hz",
        "fields": ("bandwidth_hz", "level_db"),
    },
    "control_bandwidth": {
        "title": "Контрольная ширина полосы и сверка с нормой",
        "original_table": "MEAS_N1902_RESULTCBW",
        "unit": "Hz",
        "fields": ("control_bandwidth_hz", "norm_hz"),
    },
    "power_over_threshold": {
        "title": "Мощность сигнала над порогом",
        "original_table": "MEAS_POWER_RESULT",
        "unit": "W",
        "fields": ("power_w",),
    },
    "complex_center_frequency": {
        "title": "Оценка центральной частоты",
        "original_table": "MEAS_ESTIMATE_RESULTFC",
        "unit": "Hz",
        "fields": ("center_frequency_hz",),
    },
    "signal_to_noise": {
        "title": "Отношение сигнал/шум",
        "original_table": "MEAS_B2_RESULT_SN",
        "unit": "dB",
        "fields": ("snr_db",),
    },
    "coordinates": {
        "title": "Координаты точки измерения",
        "original_table": "MEAS_LATLONG_LONGITUDE",
        "unit": "deg",
        "fields": ("latitude_deg", "longitude_deg"),
    },
    "frequency_counter": {
        "title": "Встроенный частотомер",
        "original_table": "MEAS_COUNTER_FC_RESULTCODE",
        "unit": "Hz",
        "fields": ("measured_frequency_hz", "resolution_hz", "uncertainty_hz"),
    },
}


def validate_result_payload(payload: Any) -> dict[str, Any]:
    """Exact request shape: methodology, code, frequency, interval and values."""
    if not isinstance(payload, dict):
        raise ValueError("Тело запроса должно быть JSON-объектом")
    allowed = {"methodology", "result_code", "frequency_hz", "interval", "values"}
    if set(payload) != allowed:
        raise ValueError("Разрешены только поля methodology, result_code, frequency_hz, interval и values")
    methodology = payload["methodology"]
    if not isinstance(methodology, str) or methodology not in METHODOLOGIES:
        raise ValueError(f"Методика должна быть одной из: {', '.join(sorted(METHODOLOGIES))}")
    code = payload["result_code"]
    if code not in RESULT_CODES:
        raise ValueError(f"result_code должен быть одним из: {', '.join(RESULT_CODES)}")
    frequency = payload["frequency_hz"]
    if isinstance(frequency, bool) or not isinstance(frequency, int):
        raise ValueError("frequency_hz должен быть целым числом Гц")
    if not 0 <= frequency <= MAX_FREQUENCY_HZ:
        raise ValueError("frequency_hz вне диапазона безопасных целых Hz")
    interval = payload["interval"]
    if not isinstance(interval, dict) or set(interval) != {"started_at", "ended_at"}:
        raise ValueError("interval должен содержать только started_at и ended_at")
    for key in ("started_at", "ended_at"):
        value = interval[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} должен быть непустой строкой UTC")
    values = payload["values"]
    expected = METHODOLOGIES[methodology]["fields"]
    if code == "not_measured":
        if values != {}:
            raise ValueError("Код not_measured не допускает значений результата")
    else:
        if not isinstance(values, dict) or set(values) != set(expected):
            raise ValueError(
                f"Методика {methodology} требует ровно поля: {', '.join(expected)}"
            )
        for name in expected:
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Значение {name} должно быть конечным числом")
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"Значение {name} должно быть конечным числом")
    return {
        "methodology": methodology,
        "result_code": code,
        "frequency_hz": frequency,
        "interval": dict(interval),
        "values": copy.deepcopy(values),
    }


def make_result_record(
    session_id: str, stream_id: str, payload: dict[str, Any], result_id: str,
) -> dict[str, Any]:
    """Assemble an archived record; values are stored apart from the code."""
    record = validate_result_payload(payload)
    record.update({
        "id": result_id,
        "session_id": session_id,
        "stream_id": stream_id,
        "created_at": to_primitive(utc_now()),
    })
    return record


def describe_catalog() -> list[dict[str, Any]]:
    """The methodology catalog as data: ids, titles, original tables and fields."""
    return [
        {
            "methodology": key,
            "title": spec["title"],
            "original_table": spec["original_table"],
            "unit": spec["unit"],
            "fields": list(spec["fields"]),
            "result_codes": list(RESULT_CODES),
        }
        for key, spec in sorted(METHODOLOGIES.items())
    ]
