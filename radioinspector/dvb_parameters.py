"""DVB-T parameter evaluation against explicit thresholds.

Backlog item 8: «Измерение параметров DVB-T» — MER, BER после Витерби и
после Рида — Соломона, защитный интервал и скорость кодирования. The
original keeps «Пороговые значения параметров DVB-T» and a result code
table (``MEAS_DVB_RESULTCODE``): values are compared with thresholds and
the verdict is stored apart from the numbers.

This module evaluates operator-supplied parameters against operator-supplied
thresholds and returns per-parameter verdicts plus a result code. It does
not demodulate IQ data: MER and BER measurement from a receiver capture
needs the instrument and stays out of scope. The archived source data are
never changed; the evaluation is a derived record ready for
``measurement_results`` (methodology ``dvb_t_parameters``).
"""

from __future__ import annotations

from typing import Any

MAX_DVB_REQUEST_BYTES = 8192

#: Методика записи результата в каталоге ``measurement_results``.
DVB_METHODOLOGY = "dvb_t_parameters"

#: Стандартные защитные интервалы DVB-T (доля полезного символа).
GUARD_INTERVALS = ("1/32", "1/16", "1/8", "1/4")

#: Стандартные скорости кодирования DVB-T.
CODE_RATES = ("1/2", "2/3", "3/4", "5/6", "7/8")

#: Поля измеренных параметров: MER и два BER, как в оригинале.
PARAMETER_FIELDS = ("mer_db", "ber_after_viterbi", "ber_after_rs")

#: Поля пороговых значений, «Пороговые значения параметров DVB-T».
THRESHOLD_FIELDS = ("min_mer_db", "max_ber_after_viterbi", "max_ber_after_rs")


class DvbEvaluationError(ValueError):
    """The parameters or thresholds cannot support a DVB-T evaluation."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DvbEvaluationError(f"{name} должен быть конечным числом")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise DvbEvaluationError(f"{name} должен быть конечным числом")
    return value


def validate_dvb_request(payload: Any) -> dict[str, Any]:
    """Exact request shape: measured parameters, thresholds and the two rates."""
    if not isinstance(payload, dict) or set(payload) != {"parameters", "thresholds", "guard_interval", "code_rate"}:
        raise ValueError("Разрешены только поля parameters, thresholds, guard_interval и code_rate")
    parameters = payload["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != set(PARAMETER_FIELDS):
        raise ValueError(f"parameters требует ровно поля: {', '.join(PARAMETER_FIELDS)}")
    thresholds = payload["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != set(THRESHOLD_FIELDS):
        raise ValueError(f"thresholds требует ровно поля: {', '.join(THRESHOLD_FIELDS)}")
    guard = payload["guard_interval"]
    if guard not in GUARD_INTERVALS:
        raise ValueError(f"guard_interval должен быть одним из: {', '.join(GUARD_INTERVALS)}")
    code_rate = payload["code_rate"]
    if code_rate not in CODE_RATES:
        raise ValueError(f"code_rate должен быть одним из: {', '.join(CODE_RATES)}")
    values = {name: _finite(parameters[name], name) for name in PARAMETER_FIELDS}
    limits = {name: _finite(thresholds[name], name) for name in THRESHOLD_FIELDS}
    if not 0.0 <= values["ber_after_viterbi"] <= 1.0:
        raise DvbEvaluationError("ber_after_viterbi должен быть в диапазоне от 0 до 1")
    if not 0.0 <= values["ber_after_rs"] <= 1.0:
        raise DvbEvaluationError("ber_after_rs должен быть в диапазоне от 0 до 1")
    if not 0.0 <= limits["max_ber_after_viterbi"] <= 1.0:
        raise DvbEvaluationError("max_ber_after_viterbi должен быть в диапазоне от 0 до 1")
    if not 0.0 <= limits["max_ber_after_rs"] <= 1.0:
        raise DvbEvaluationError("max_ber_after_rs должен быть в диапазоне от 0 до 1")
    return {
        "parameters": values, "thresholds": limits,
        "guard_interval": guard, "code_rate": code_rate,
    }


def evaluate_dvb_parameters(payload: Any) -> dict[str, Any]:
    """Compare each parameter with its threshold and derive a result code.

    The result code is a verdict on the supplied numbers only — never a
    measurement and never a normative certification. ``norm`` requires every
    parameter inside its threshold; ``not_norm`` marks at least one breach.
    Ties count as passing (``>=`` for MER, ``<=`` for BER).
    """
    request = validate_dvb_request(payload)
    values = request["parameters"]
    limits = request["thresholds"]
    verdicts = {
        "mer_db": values["mer_db"] >= limits["min_mer_db"],
        "ber_after_viterbi": values["ber_after_viterbi"] <= limits["max_ber_after_viterbi"],
        "ber_after_rs": values["ber_after_rs"] <= limits["max_ber_after_rs"],
    }
    result_code = "norm" if all(verdicts.values()) else "not_norm"
    return {
        "methodology": DVB_METHODOLOGY,
        "result_code": result_code,
        "guard_interval": request["guard_interval"],
        "code_rate": request["code_rate"],
        "parameters": values,
        "thresholds": limits,
        "verdicts": verdicts,
        "values": {
            **values,
            "guard_interval_ratio": _guard_ratio(request["guard_interval"]),
            "code_rate_ratio": _code_ratio(request["code_rate"]),
        },
    }


def _guard_ratio(label: str) -> float:
    numerator, denominator = label.split("/")
    return int(numerator) / int(denominator)


def _code_ratio(label: str) -> float:
    numerator, denominator = label.split("/")
    return int(numerator) / int(denominator)
