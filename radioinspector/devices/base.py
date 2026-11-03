"""Абстракция средств измерений.

Определяет единый интерфейс управления анализаторами спектра, измерительными
и сканирующими приёмниками, а также приёмниками GPS. Конкретные драйверы
реализуются в подмодулях :mod:`radioinspector.devices`.

Абстракция повторяет подход исходного комплекса, где работа с прибором
вынесена в отдельную программу управления оборудованием, а прикладные
программы обращаются к ней через измерительную схему. Здесь разделение
выполнено на уровне модулей: прикладной код работает только с
:class:`Instrument`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..trace import Trace


@dataclass
class InstrumentInfo:
    """Идентификационные данные средства измерений."""

    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    interface: str = ""

    @property
    def idn(self) -> str:
        return f"{self.manufacturer},{self.model},{self.serial},{self.firmware}"

    def as_dict(self) -> dict:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
            "interface": self.interface,
        }


@dataclass
class SweepConfig:
    """Параметры сканирования диапазона частот."""

    start_hz: float
    stop_hz: float
    resolution_bandwidth_hz: float = 100_000.0
    video_bandwidth_hz: float = 100_000.0
    points: int = 801
    reference_level_db: float = 0.0
    attenuation_db: float = 0.0
    detector: str = "MaxPeak"
    sweep_time_s: float = 0.0

    @property
    def step_hz(self) -> float:
        if self.points < 2:
            return 0.0
        return (self.stop_hz - self.start_hz) / (self.points - 1)

    def as_dict(self) -> dict:
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "resolution_bandwidth_hz": self.resolution_bandwidth_hz,
            "video_bandwidth_hz": self.video_bandwidth_hz,
            "points": self.points,
            "reference_level_db": self.reference_level_db,
            "attenuation_db": self.attenuation_db,
            "detector": self.detector,
            "sweep_time_s": self.sweep_time_s,
        }


@dataclass
class Calibration:
    """Калибровочные характеристики измерительного тракта.

    Хранятся погрешности измерения частоты и уровня, а также коэффициент
    калибровки антенны и промежуточного тракта. Именно эти данные исходный
    комплекс учитывает при формировании результатов измерений.
    """

    antenna: str = "Ant"
    tract: str = "Tract"
    frequency_uncertainty_hz: float = 0.0
    level_uncertainty_db: float = 0.0
    calibration_db: float = 0.0
    antenna_gain_db: float = 0.0
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "antenna": self.antenna,
            "tract": self.tract,
            "frequency_uncertainty_hz": self.frequency_uncertainty_hz,
            "level_uncertainty_db": self.level_uncertainty_db,
            "calibration_db": self.calibration_db,
            "antenna_gain_db": self.antenna_gain_db,
            "notes": self.notes,
        }


class InstrumentError(RuntimeError):
    """Ошибка взаимодействия со средством измерений."""


class Instrument(abc.ABC):
    """Базовый интерфейс средства измерений."""

    #: Имя типа прибора, используется в имени файла базы данных панорам.
    device_name: str = "Unknown"

    @abc.abstractmethod
    def connect(self) -> InstrumentInfo:
        """Устанавливает соединение и возвращает идентификационные данные."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Разрывает соединение."""

    @abc.abstractmethod
    def sweep(self, config: SweepConfig) -> Trace:
        """Выполняет измерение и возвращает амплитудно-частотную характеристику."""

    def identify(self) -> InstrumentInfo:
        """Идентификационные данные без переподключения."""
        return InstrumentInfo(interface=self.device_name)

    def set_center_frequency(self, frequency_hz: float) -> None:
        """Устанавливает центральную частоту для режима прямого контроля."""
        raise InstrumentError(
            f"{self.device_name}: настройка центральной частоты не поддерживается"
        )

    def set_squelch(self, level: int) -> None:
        """Устанавливает уровень шумоподавителя (0 — выключен)."""
        raise InstrumentError(
            f"{self.device_name}: управление шумоподавителем не поддерживается"
        )

    def set_volume(self, level: int) -> None:
        """Устанавливает уровень громкости демодулированного аудиосигнала."""
        raise InstrumentError(
            f"{self.device_name}: управление громкостью не поддерживается"
        )

    def __enter__(self) -> "Instrument":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()
