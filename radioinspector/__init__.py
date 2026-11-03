"""OpenRadioInspector — открытый комплекс программ для исследования
радиочастотного спектра и измерения параметров высокочастотных излучений.

Пакет реализует функциональный аналог комплекса измерительных программ
«Инспектор» (RadioInspector) на открытых компонентах:

* :mod:`radioinspector.units` — преобразования единиц измерения;
* :mod:`radioinspector.trace` — модель спектральной характеристики;
* :mod:`radioinspector.measurements` — алгоритмы измерения параметров
  излучений (полоса β/2, полоса по уровню −N дБ, напряжённость поля, оценка
  центральной частоты);
* :mod:`radioinspector.threshold` — линия порога;
* :mod:`radioinspector.signals` — список сигналов, превысивших порог;
* :mod:`radioinspector.panorama` — база данных панорам;
* :mod:`radioinspector.assignments` — база данных частотных присвоений;
* :mod:`radioinspector.occupancy` — оценка загрузки спектра;
* :mod:`radioinspector.interference` — поиск источников помех;
* :mod:`radioinspector.geoloc` — карты покрытия и амплитудная пеленгация;
* :mod:`radioinspector.devices` — управление средствами измерений;
* :mod:`radioinspector.export` — форматы обмена данными;
* :mod:`radioinspector.reporting` — формирование протоколов;
* :mod:`radioinspector.web` — веб-интерфейс.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .assignments import Assignment, AssignmentDatabase
from .interference import detect_interference
from .measurements import (
    bandwidth_at_level,
    center_frequency_centroid,
    field_strength,
    measure_signal,
    occupied_bandwidth_beta2,
)
from .occupancy import band_occupancy, occupancy_from_store
from .panorama import PanoramaLibrary, PanoramaStore, ScanSettings
from .signals import SignalList
from .threshold import ThresholdLine
from .trace import Trace

__all__ = [
    "__version__",
    "Assignment",
    "AssignmentDatabase",
    "PanoramaLibrary",
    "PanoramaStore",
    "ScanSettings",
    "SignalList",
    "ThresholdLine",
    "Trace",
    "bandwidth_at_level",
    "band_occupancy",
    "center_frequency_centroid",
    "detect_interference",
    "field_strength",
    "measure_signal",
    "occupancy_from_store",
    "occupied_bandwidth_beta2",
]
