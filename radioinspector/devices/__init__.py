"""Драйверы средств измерений.

Реализуют работу с анализаторами спектра и приёмниками по открытым
протоколам: SCPI поверх TCP-сокета (порт 5025), VXI-11/LXI через внешнюю
библиотеку, SCPI через COM-порт, а также через SoapySDR и rtl_tcp.

Выбор драйвера осуществляется автоматически по строке подключения, см.
:func:`radioinspector.devices.registry.open_instrument`.
"""

from __future__ import annotations

from .base import (
    Calibration,
    Instrument,
    InstrumentError,
    InstrumentInfo,
    SweepConfig,
)

__all__ = [
    "Calibration",
    "Instrument",
    "InstrumentError",
    "InstrumentInfo",
    "SweepConfig",
]
