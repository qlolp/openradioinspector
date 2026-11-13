"""Алгоритмы измерения радиотехнических параметров излучений.

Реализованы четыре метода из раздела 11 «Алгоритмы выполнения измерений
радиотехнических параметров излучений» руководства оператора Книги 6:

* :func:`occupied_bandwidth_beta2` — ширина полосы занимаемых частот методом
  β/2 (п. 3.4.1 справочника МСЭ, рекомендация ITU-R SM.328-10);
* :func:`bandwidth_at_level` — ширина полосы частот по уровню −N дБ
  (ГОСТ 30318-95, нормы 19-02-21);
* :func:`field_strength` — напряжённость электрического поля;
* :func:`center_frequency_centroid` — оценка центральной частоты методом
  центра тяжести.

Все алгоритмы работают по массиву «амплитуда — частота» и возвращают
результат вместе с погрешностью и предупреждениями о применимости метода.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .trace import Trace

#: Значение β/2 по умолчанию. Согласно ITU-R SM.328-10 и справочнику МСЭ,
#: если иное не оговорено для конкретного класса излучения, β/2 = 0,5 %.
DEFAULT_BETA = 1.0

#: Рекомендуемое отношение сигнал/шум для метода β/2 (справочник МСЭ,
#: рекомендация ITU-R SM.328-10; ECC Recommendation (06)01).
MIN_SNR_BETA2_DB = 40.0

#: Рекомендуемое отношение сигнал/шум для метода центра тяжести.
MIN_SNR_CENTROID_DB = 30.0

#: Минимальное число точек в полосе обзора для метода центра тяжести.
MIN_POINTS_CENTROID = 200

#: Порог выделения сигнала над шумовой дорожкой в методе центра тяжести.
CENTROID_NOISE_MARGIN_DB = 6.0


@dataclass
class MeasurementResult:
    """Результат измерения с погрешностью и предупреждениями."""

    name: str
    value: float
    unit: str
    uncertainty: float = 0.0
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "uncertainty": self.uncertainty,
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


@dataclass
class BandwidthResult:
    """Результат измерения полосы занимаемых частот."""

    bandwidth_hz: float
    lower_edge_hz: float
    upper_edge_hz: float
    center_hz: float
    method: str
    uncertainty_hz: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "bandwidth_hz": self.bandwidth_hz,
            "lower_edge_hz": self.lower_edge_hz,
            "upper_edge_hz": self.upper_edge_hz,
            "center_hz": self.center_hz,
            "method": self.method,
            "uncertainty_hz": self.uncertainty_hz,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def _require_finite_levels(traces: Sequence[Trace] | Trace) -> None:
    """Отклоняет трассу с нечисловыми уровнями до расчёта.

    Нечисловые значения (``NaN``, ``Infinity``) в исходном измерении означают,
    что часть точек не получена. Если пропустить их дальше, результат измерения
    молча окажется ``NaN`` и попадёт в отчёт как «нет данных», что неверно:
    точка не получена и сигнал не измерен — это разные случаи.
    """
    items = [traces] if isinstance(traces, Trace) else list(traces)
    for item in items:
        if not isinstance(item, Trace):
            raise TypeError("Ожидается спектральная трасса")
        if item.size and not bool(np.all(np.isfinite(item.levels))):
            raise ValueError(
                "Уровни измерения должны быть конечными числами: в трассе есть "
                "неполученные точки (NaN или бесконечность)"
            )


def _finite_trace(function):
    """Проверяет конечность уровней до расчёта, сохраняя контракт функции."""

    @functools.wraps(function)
    def wrapper(traces, *args, **kwargs):
        _require_finite_levels(traces)
        return function(traces, *args, **kwargs)

    return wrapper


def _db_to_mw(levels_db: np.ndarray) -> np.ndarray:
    """Перевод уровней из дБ в линейные мощности, мВт."""
    return np.power(10.0, levels_db / 10.0)


def _interpolate_crossing(
    f_a: float,
    v_a: float,
    f_b: float,
    v_b: float,
    target: float,
) -> float:
    """Линейная интерполяция абсциссы пересечения уровня ``target``.

    Уровни ``v_a`` и ``v_b`` задаются в линейных величинах; абсциссы
    ``f_a`` и ``f_b`` — в Гц. Значения ``f_a`` и ``f_b`` должны различаться.
    """
    if f_b == f_a:
        return f_a
    ratio = (target - v_a) / (v_b - v_a)
    return f_a + ratio * (f_b - f_a)


def _estimate_snr_db(trace: Trace) -> float:
    """Оценка отношения сигнал/шум: пик минус медиана шумовой части."""
    if trace.size < 3:
        return float("nan")
    peak = float(np.max(trace.levels))
    noise = float(np.median(trace.levels))
    return peak - noise


# --------------------------------------------------------------------------
# 1. Ширина полосы занимаемых частот методом β/2
# --------------------------------------------------------------------------

@_finite_trace
def occupied_bandwidth_beta2(
    trace: Trace,
    beta: float = DEFAULT_BETA,
    frequency_uncertainty_hz: float = 0.0,
) -> BandwidthResult:
    """Ширина полосы, в которой сосредоточено (100 − β)% мощности сигнала.

    Алгоритм (§11.1):

    1. Все значения уровней переводятся из дБ в мВт.
    2. Рассчитывается общая мощность ``P`` как сумма элементов массива.
    3. Рассчитывается мощность ``Pb/2 = (P / 100 * β) / 2``.
    4. Определяется левая граница: накопленная мощность слева направо
       достигает ``Pb/2``; уточнение частоты границы выполняется
       кусочно-линейной интерполяцией.
    5. Аналогично определяется правая граница.
    6. Результат равен ``Fп − Fл``.

    Модель распределения мощности
    -----------------------------
    Каждая точка измерения соответствует конечной полосе частот шириной
    ``шаг перестройки``; её мощность считается равномерно распределённой по
    этой полосе. Накопленная мощность является кусочно-линейной функцией
    частоты, поэтому граница полосы находится точным обращением этой
    функции. Такой подход совпадает с численным интегрированием спектра,
    рекомендуемым в ITU-R SM.328-10 и справочнике МСЭ (п. 3.4.1), и
    отличается от дискретного суммирования по строкам лишь на величину
    половины шага перестройки.

    Параметры
    ----------
    trace:
        Спектральная характеристика сигнала.
    beta:
        Процент отбрасываемой мощности, %. Значение 1 соответствует 99%
        мощности в полосе; по умолчанию используется 1.
    frequency_uncertainty_hz:
        Погрешность измерения частоты средством измерений, Гц.
    """
    if trace.size < 2:
        raise ValueError("Для измерения полосы необходимо не менее двух точек")
    if not 0 < beta < 100:
        raise ValueError("β должно лежать в интервале (0, 100)")

    warnings: list[str] = []
    freqs = trace.frequencies
    power_mw = _db_to_mw(trace.levels)
    total_power = float(np.sum(power_mw))
    if total_power <= 0:
        raise ValueError("Суммарная мощность сигнала не положительна")

    half_power = (total_power / 100.0 * beta) / 2.0
    step = float(np.median(np.diff(freqs)))

    # Левая граница: мощность накапливается слева направо. Граница
    # находится внутри полосы той точки, в которой накопленная мощность
    # впервые достигает Pb/2.
    cumulative = np.cumsum(power_mw)
    idx_left = int(np.searchsorted(cumulative, half_power))
    idx_left = min(idx_left, power_mw.size - 1)
    before_left = cumulative[idx_left - 1] if idx_left > 0 else 0.0
    cell_power = power_mw[idx_left]
    if cell_power <= 0:
        f_left = float(freqs[idx_left])
    else:
        share = (half_power - before_left) / cell_power
        share = min(max(share, 0.0), 1.0)
        f_left = float(freqs[idx_left] - step / 2.0 + share * step)

    # Правая граница: мощность накапливается справа налево.
    reverse_cumulative = np.cumsum(power_mw[::-1])
    idx_right = int(np.searchsorted(reverse_cumulative, half_power))
    idx_right = min(idx_right, power_mw.size - 1)
    actual_right = power_mw.size - 1 - idx_right
    before_right = reverse_cumulative[idx_right - 1] if idx_right > 0 else 0.0
    cell_power_r = power_mw[actual_right]
    if cell_power_r <= 0:
        f_right = float(freqs[actual_right])
    else:
        share = (half_power - before_right) / cell_power_r
        share = min(max(share, 0.0), 1.0)
        f_right = float(freqs[actual_right] + step / 2.0 - share * step)

    bandwidth = max(f_right - f_left, 0.0)

    snr_db = _estimate_snr_db(trace)
    if np.isfinite(snr_db) and snr_db < MIN_SNR_BETA2_DB:
        warnings.append(
            f"Отношение сигнал/шум {snr_db:.1f} дБ ниже рекомендуемых "
            f"{MIN_SNR_BETA2_DB:.0f} дБ — точность измерения снижена"
        )
    if trace.span < 1.5 * bandwidth:
        warnings.append(
            "Полоса обзора должна составлять 1,5–2 полосы занимаемых частот; "
            "иначе результат недостоверен"
        )
    if trace.step and trace.step > bandwidth / 10:
        warnings.append(
            "Шаг перестройки частоты слишком велик относительно измеренной полосы"
        )

    return BandwidthResult(
        bandwidth_hz=bandwidth,
        lower_edge_hz=f_left,
        upper_edge_hz=f_right,
        center_hz=0.5 * (f_left + f_right),
        method=f"beta/2 (beta={beta:g}%)",
        uncertainty_hz=frequency_uncertainty_hz,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 2. Ширина полосы частот по уровню −N дБ
# --------------------------------------------------------------------------

@_finite_trace
def bandwidth_at_level(
    trace: Trace,
    n_db: float = 30.0,
    frequency_uncertainty_hz: float = 0.0,
) -> BandwidthResult:
    """Ширина полосы, внутри которой уровень не ниже ``Umax − N``.

    Алгоритм (§11.2):

    1. В массиве находится точка с максимальным уровнем ``Umax``.
    2. Рассчитывается порог ``Uпорог = Umax − N``.
    3. Перебором влево от ``Umax`` находится ближайшая точка ``n`` вне полосы
       сигнала, для которой ``U(n) < Uпорог``; частота левой границы ``Fл``
       уточняется кусочно-линейной интерполяцией между ``n`` и ``n+1``
       (в линейных величинах) — то есть решается уравнение ``y = Uпорог``.
    4. Аналогично определяется правая граница: ближайшая справа точка ``n``
       вне полосы сигнала, интерполяция между ``n−1`` и ``n``.
    5. Результат равен ``Fп − Fл``.

    Замечание об индексации. В исходном описании алгоритма условие перехода
    записано как ``U(n) < Uпорог`` и ``U(n−1) > Uпорог``, что соответствует
    массиву, упорядоченному по убыванию частоты. В настоящей реализации
    частоты возрастают, поэтому для левой границы используется пара ``(n, n+1)``,
    а для правой — ``(n−1, n)``. Смысл перехода — ближайшая к сигналу точка
    вне полосы — сохраняется в обоих случаях.

    Нормативная база: ГОСТ 30318-95 (ширину полосы нормируют на уровне
    минус 30 дБ; контрольные уровни внеполосных излучений — −40, −50 и
    −60 дБ), Нормы ГКРЧ 19-21 (контрольная ширина полосы частот на уровне
    минус 30 дБ).
    """
    if trace.size < 2:
        raise ValueError("Для измерения полосы необходимо не менее двух точек")
    if n_db <= 0:
        raise ValueError("Уровень N должен быть положительным")

    warnings: list[str] = []
    freqs = trace.frequencies
    levels = trace.levels
    imax = int(np.argmax(levels))
    u_max = float(levels[imax])
    threshold = u_max - n_db

    # Перевод в линейные величины выполняется по мощности, поэтому
    # интерполяция ведётся между мощностями.
    lin = _db_to_mw(levels)
    thr_lin = _db_to_mw(np.array([threshold]))[0]

    # Левая граница: ближайшая слева от Umax точка вне полосы сигнала.
    # Переход между ней и следующей точкой (внутри полосы) уточняется
    # кусочно-линейной интерполяцией.
    f_left = float(freqs[0])
    left_found = False
    for i in range(imax, 0, -1):
        if levels[i] < threshold:
            f_left = _interpolate_crossing(
                float(freqs[i]),
                float(lin[i]),
                float(freqs[i + 1]),
                float(lin[i + 1]),
                thr_lin,
            )
            left_found = True
            break
    if not left_found:
        warnings.append(
            "Сигнал не отделён от левого края полосы обзора — левая граница "
            "принята равной частоте начала обзора"
        )

    # Правая граница: ближайшая справа от Umax точка вне полосы сигнала.
    f_right = float(freqs[-1])
    right_found = False
    for i in range(imax, freqs.size - 1):
        if levels[i] < threshold:
            f_right = _interpolate_crossing(
                float(freqs[i]),
                float(lin[i]),
                float(freqs[i - 1]),
                float(lin[i - 1]),
                thr_lin,
            )
            right_found = True
            break
    if not right_found:
        warnings.append(
            "Сигнал не отделён от правого края полосы обзора — правая граница "
            "принята равной частоте конца обзора"
        )

    bandwidth = max(f_right - f_left, 0.0)

    snr_db = _estimate_snr_db(trace)
    if np.isfinite(snr_db) and snr_db < MIN_SNR_BETA2_DB:
        warnings.append(
            f"Отношение сигнал/шум {snr_db:.1f} дБ ниже рекомендуемых "
            f"{MIN_SNR_BETA2_DB:.0f} дБ"
        )
    if trace.step and trace.step > bandwidth / 10:
        warnings.append(
            "Шаг перестройки частоты слишком велик относительно измеренной полосы"
        )

    return BandwidthResult(
        bandwidth_hz=bandwidth,
        lower_edge_hz=f_left,
        upper_edge_hz=f_right,
        center_hz=0.5 * (f_left + f_right),
        method=f"-{n_db:g} dB",
        uncertainty_hz=frequency_uncertainty_hz,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 2a. Контрольная полоса занимаемых частот и сравнение с нормой
# --------------------------------------------------------------------------

#: Уровень, на котором измеряется контрольная ширина полосы частот, дБ.
CONTROL_BANDWIDTH_LEVEL_DB = 30.0

#: Допуск при сравнении измеренной контрольной полосы с нормой, доля.
CONTROL_BANDWIDTH_TOLERANCE = 0.10

#: Допустимое превышение уровня шумов над измеренным уровнем сигнала, дБ.
NOISE_MARGIN_DB = 10.0


@dataclass
class ControlBandwidthResult:
    """Контрольная ширина полосы частот и результат её сравнения с нормой.

    Нормативная база: Нормы ГКРЧ 19-21 «Нормы на ширину полосы радиочастот и
    внеполосные излучения радиопередатчиков гражданского применения» (п. 4.5,
    5.1.3). Контрольная ширина полосы измеряется на уровне −30 дБ; ширина
    полосы по уровням −40, −50 и −60 дБ относительно нулевого уровня
    контролирует внеполосовые излучения.
    """

    bandwidth_hz: float
    level_db: float
    norm_hz: float | None = None
    tolerance: float = CONTROL_BANDWIDTH_TOLERANCE
    verdict: str = ""
    norm_with_tolerance_hz: float | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "bandwidth_hz": self.bandwidth_hz,
            "level_db": self.level_db,
            "norm_hz": self.norm_hz,
            "tolerance": self.tolerance,
            "verdict": self.verdict,
            "norm_with_tolerance_hz": self.norm_with_tolerance_hz,
            "warnings": list(self.warnings),
        }


@_finite_trace
def control_bandwidth(
    trace: Trace,
    norm_hz: float | None = None,
    level_db: float = CONTROL_BANDWIDTH_LEVEL_DB,
    tolerance: float = CONTROL_BANDWIDTH_TOLERANCE,
    frequency_uncertainty_hz: float = 0.0,
) -> ControlBandwidthResult:
    """Измерение контрольной ширины полосы частот и сравнение с нормой.

    Параметры
    ----------
    trace:
        Спектральная трасса сигнала.
    norm_hz:
        Норма на контрольную полосу частот, Гц. При ``None`` норма не
        задаётся и вердикт не формируется.
    level_db:
        Уровень отсчёта, дБ. По Нормам ГКРЧ 19-21 контрольная ширина полосы
        измеряется на уровне −30 дБ.
    tolerance:
        Допуск при сравнении с нормой, доля. Исходный комплекс применяет
        допуск 10 %: норма, увеличенная на 10 %, используется как верхняя
        граница при проверке.
    """
    measured = bandwidth_at_level(
        trace, n_db=level_db, frequency_uncertainty_hz=frequency_uncertainty_hz
    )
    warnings = list(measured.warnings)

    verdict = ""
    norm_with_tolerance = None
    if norm_hz is not None and norm_hz > 0:
        norm_with_tolerance = norm_hz * (1.0 + tolerance)
        if measured.bandwidth_hz <= norm_with_tolerance:
            verdict = "норма"
        else:
            verdict = "не норма"

    # Условие применимости методики: уровень шумов должен быть ниже
    # измеренного уровня сигнала не менее чем на 10 дБ.
    snr_db = _estimate_snr_db(trace)
    if np.isfinite(snr_db) and snr_db < NOISE_MARGIN_DB:
        warnings.append(
            f"Отношение сигнал/шум {snr_db:.1f} дБ меньше требуемых "
            f"{NOISE_MARGIN_DB:.0f} дБ — условия методики контроля не выполнены"
        )

    return ControlBandwidthResult(
        bandwidth_hz=measured.bandwidth_hz,
        level_db=level_db,
        norm_hz=norm_hz,
        tolerance=tolerance,
        verdict=verdict,
        norm_with_tolerance_hz=norm_with_tolerance,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 2b. Комплексная оценка центральной частоты
# --------------------------------------------------------------------------


@dataclass
class ComplexFrequencyResult:
    """Комплексная оценка центральной частоты сигнала.

    Исходный комплекс определяет комплексную оценку как среднее между
    оценкой частоты методом поиска центра тяжести спектра сигнала и серединой
    полосы занимаемых частот по уровню −30 дБ (по уровню −N дБ, если N < 30 дБ).
    """

    value_hz: float
    centroid_hz: float
    midpoint_hz: float
    bandwidth_hz: float
    method: str = "среднее между центром тяжести и серединой полосы −30 дБ"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "value_hz": self.value_hz,
            "centroid_hz": self.centroid_hz,
            "midpoint_hz": self.midpoint_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "method": self.method,
            "warnings": list(self.warnings),
        }


@_finite_trace
def complex_center_frequency(
    trace: Trace,
    n_db: float = CONTROL_BANDWIDTH_LEVEL_DB,
    frequency_uncertainty_hz: float = 0.0,
) -> ComplexFrequencyResult:
    """Комплексная оценка центральной частоты сигнала."""
    centroid = center_frequency_centroid(
        trace, frequency_uncertainty_hz=frequency_uncertainty_hz
    )
    level = min(n_db, CONTROL_BANDWIDTH_LEVEL_DB)
    bandwidth = bandwidth_at_level(
        trace, n_db=level, frequency_uncertainty_hz=frequency_uncertainty_hz
    )
    warnings = list(centroid.warnings) + list(bandwidth.warnings)

    centroid_hz = float(centroid.value)
    midpoint_hz = float(bandwidth.center_hz)
    if not np.isfinite(centroid_hz):
        warnings.append(
            "Оценка методом центра тяжести недоступна — используется середина "
            "полосы занимаемых частот"
        )
        value = midpoint_hz
    else:
        value = 0.5 * (centroid_hz + midpoint_hz)

    return ComplexFrequencyResult(
        value_hz=value,
        centroid_hz=centroid_hz,
        midpoint_hz=midpoint_hz,
        bandwidth_hz=float(bandwidth.bandwidth_hz),
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 3. Напряжённость электрического поля
# --------------------------------------------------------------------------
@_finite_trace
def field_strength(
    traces: Sequence[Trace] | Trace,
    method: str = "median",
    calibration_db: float = 0.0,
    level_uncertainty_db: float = 0.0,
) -> MeasurementResult:
    """Напряжённость электрического поля по массиву панорам.

    Алгоритм (§11.3):

    1. Единичное измерение — максимальный уровень в исходном массиве ``E``.
    2. Для графиков максимальных или минимальных значений рассчитанное
       значение ``E`` и есть напряжённость поля.
    3. Для графика текущих значений собирается выборка ``Ei`` по результатам
       поиска максимума в каждом массиве.
    4. Над выборкой выполняется медианный, усредняющий или максимальный
       расчёт в зависимости от выбранного метода.

    Параметры
    ----------
    calibration_db:
        Коэффициент калибровки измерительного тракта на частоте измерений, дБ.
        Прибавляется к показаниям средства измерений.
    """
    if isinstance(traces, Trace):
        traces = [traces]
    traces = list(traces)
    if not traces:
        raise ValueError("Не задано ни одной панорамы")

    method = method.lower()
    if method not in ("median", "mean", "max"):
        raise ValueError("Метод измерения должен быть median, mean или max")

    warnings: list[str] = []
    samples: list[float] = []
    for tr in traces:
        if tr.size == 0:
            continue
        samples.append(float(np.max(tr.levels)))

    if not samples:
        raise ValueError("Все переданные панорамы пусты")

    if method == "median":
        value = float(np.median(samples))
    elif method == "mean":
        value = float(np.mean(samples))
    else:
        value = float(np.max(samples))

    value += calibration_db

    if len(traces) == 1 and method == "median":
        warnings.append(
            "Медианный метод на единственной панораме сводится к максимуму; "
            "для достоверной оценки нужно несколько панорам"
        )

    return MeasurementResult(
        name="Напряжённость поля",
        value=value,
        unit="dB",
        uncertainty=level_uncertainty_db,
        warnings=warnings,
        details={
            "method": method,
            "samples": samples,
            "sample_count": len(samples),
            "calibration_db": calibration_db,
        },
    )


# --------------------------------------------------------------------------
# 4. Оценка центральной частоты методом центра тяжести
# --------------------------------------------------------------------------

def _autocorrelation(levels: np.ndarray) -> np.ndarray:
    """Нормированная автокорреляционная функция конечного отрезка."""
    x = levels - float(np.mean(levels))
    n = x.size
    if n < 2:
        return np.zeros(max(n, 1))
    result = np.correlate(x, x, mode="full")[n - 1 :]
    if result[0] == 0:
        return result
    return result / result[0]


@_finite_trace
def center_frequency_centroid(
    trace: Trace,
    frequency_uncertainty_hz: float = 0.0,
) -> MeasurementResult:
    """Центральная частота сигнала методом центра тяжести.

    Алгоритм (§11.4):

    1. Выделяется сигнал на уровне шума.
    2. Ищется отрезок массива, у которого автокорреляционная функция имеет
       минимальное значение; математическое ожидание элементов этого отрезка
       принимается за уровень шума. Шумовая дорожка ищется справа и слева от
       середины графика, за итоговый уровень шума принимается максимум из
       двух значений.
    3. Выделяется сигнал над уровнем шумовой дорожки: границы ищутся от
       середины массива влево и вправо; за порог принимается уровень шума
       + 6 дБ.
    4. В выделенных границах находится точка, для которой средняя мощность
       сигнала слева равна средней мощности справа; частота этой точки и
       является расчётной частотой сигнала.

    Условия применимости: симметричный спектр, отношение сигнал/шум не ниже
    30 дБ, сигнал в центре полосы обзора, не менее 200 точек в полосе.
    """
    if trace.size < 3:
        raise ValueError("Для оценки центральной частоты необходимо не менее трёх точек")

    warnings: list[str] = []
    freqs = trace.frequencies
    levels = trace.levels
    n = levels.size

    if n < MIN_POINTS_CENTROID:
        warnings.append(
            f"В полосе обзора {n} точек при рекомендуемых {MIN_POINTS_CENTROID}; "
            "точность оценки снижена"
        )

    snr_db = _estimate_snr_db(trace)
    if np.isfinite(snr_db) and snr_db < MIN_SNR_CENTROID_DB:
        warnings.append(
            f"Отношение сигнал/шум {snr_db:.1f} дБ ниже рекомендуемых "
            f"{MIN_SNR_CENTROID_DB:.0f} дБ"
        )

    # --- Шаг 2: уровень шумовой дорожки --------------------------------
    acf = _autocorrelation(levels)
    half = n // 2
    window = max(3, n // 20)

    def _noise_track(start: int, end: int) -> float:
        """Минимум АКФ в окне и среднее уровней этого окна."""
        lo = max(0, start)
        hi = min(n, end)
        if hi - lo < 3:
            return float(np.median(levels))
        segment = acf[lo:hi]
        pos = int(np.argmin(segment)) + lo
        w0 = max(0, pos - window // 2)
        w1 = min(n, w0 + window)
        return float(np.mean(levels[w0:w1]))

    left_noise = _noise_track(0, half)
    right_noise = _noise_track(half, n)
    noise_level = max(left_noise, right_noise)

    # --- Шаг 3: границы сигнала над порогом шум + 6 дБ ------------------
    threshold = noise_level + CENTROID_NOISE_MARGIN_DB
    power = _db_to_mw(levels)

    center_idx = half
    i_left = 0
    for i in range(center_idx, 0, -1):
        if levels[i] > threshold and levels[i - 1] <= threshold:
            i_left = i
            break
    else:
        i_left = 0

    i_right = n - 1
    for i in range(center_idx, n - 1):
        if levels[i] > threshold and levels[i + 1] <= threshold:
            i_right = i
            break
    else:
        i_right = n - 1

    if i_right - i_left < 3:
        warnings.append("Сигнал не выделен над уровнем шума — проверьте исходные данные")
        return MeasurementResult(
            name="Центральная частота (метод центра тяжести)",
            value=float("nan"),
            unit="Гц",
            uncertainty=frequency_uncertainty_hz + (trace.step or 0.0),
            warnings=warnings,
            details={
                "noise_level_db": noise_level,
                "threshold_db": threshold,
                "left_edge_hz": float(freqs[i_left]),
                "right_edge_hz": float(freqs[i_right]),
            },
        )

    # --- Шаг 4: точка равенства средних мощностей слева и справа --------
    segment = power[i_left : i_right + 1]
    cumulative = np.cumsum(segment)
    half_total = cumulative[-1] / 2.0
    pos = int(np.searchsorted(cumulative, half_total))
    # Samples represent bins centered on their frequencies. Invert the
    # cumulative power inside the selected bin, not the following interval.
    pos = min(pos, segment.size - 1)
    previous = cumulative[pos - 1] if pos else 0.0
    fraction = (half_total - previous) / segment[pos] if segment[pos] > 0 else 0.5
    idx = i_left + pos
    left = (freqs[idx - 1] + freqs[idx]) / 2 if idx else freqs[0] - (freqs[1] - freqs[0]) / 2
    right = (freqs[idx] + freqs[idx + 1]) / 2 if idx + 1 < n else freqs[-1] + (freqs[-1] - freqs[-2]) / 2
    f_center = float(left + fraction * (right - left))

    if not (freqs[0] <= f_center <= freqs[-1]):
        warnings.append("Расчётная частота выходит за пределы полосы обзора")

    return MeasurementResult(
        name="Центральная частота (метод центра тяжести)",
        value=f_center,
        unit="Гц",
        uncertainty=frequency_uncertainty_hz + (trace.step or 0.0),
        warnings=warnings,
        details={
            "noise_level_db": noise_level,
            "left_noise_db": left_noise,
            "right_noise_db": right_noise,
            "threshold_db": threshold,
            "left_edge_hz": float(freqs[i_left]),
            "right_edge_hz": float(freqs[i_right]),
            "snr_db": snr_db,
        },
    )


# --------------------------------------------------------------------------
# Сводный расчёт параметров сигнала
# --------------------------------------------------------------------------

@_finite_trace
def measure_signal(
    trace: Trace,
    beta: float = DEFAULT_BETA,
    n_db: float = 30.0,
    field_method: str = "median",
    calibration_db: float = 0.0,
    frequency_uncertainty_hz: float = 0.0,
    level_uncertainty_db: float = 0.0,
    norm_hz: float | None = None,
) -> dict:
    """Выполняет полный набор измерений параметров отдельного сигнала.

    Возвращает словарь с результатами измерения полосы методом β/2, полосы
    по уровню −N дБ, контрольной полосы занимаемых частот с сравнением с
    нормой, напряжённости поля, оценки центральной частоты и комплексной
    оценки центральной частоты.
    """
    result: dict = {"trace_points": trace.size}
    result["occupied_bandwidth_beta2"] = occupied_bandwidth_beta2(
        trace, beta=beta, frequency_uncertainty_hz=frequency_uncertainty_hz
    ).as_dict()
    result["bandwidth_at_level"] = bandwidth_at_level(
        trace, n_db=n_db, frequency_uncertainty_hz=frequency_uncertainty_hz
    ).as_dict()
    result["control_bandwidth"] = control_bandwidth(
        trace,
        norm_hz=norm_hz,
        level_db=CONTROL_BANDWIDTH_LEVEL_DB,
        frequency_uncertainty_hz=frequency_uncertainty_hz,
    ).as_dict()
    result["field_strength"] = field_strength(
        trace,
        method=field_method,
        calibration_db=calibration_db,
        level_uncertainty_db=level_uncertainty_db,
    ).as_dict()
    result["center_frequency_centroid"] = center_frequency_centroid(
        trace, frequency_uncertainty_hz=frequency_uncertainty_hz
    ).as_dict()
    result["center_frequency_complex"] = complex_center_frequency(
        trace,
        n_db=n_db,
        frequency_uncertainty_hz=frequency_uncertainty_hz,
    ).as_dict()
    return result
