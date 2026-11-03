"""Синтетический генератор измерений.

Позволяет запускать полный цикл «сканирование → накопление панорам → порог →
список сигналов → постобработка» без реального оборудования. Используется в
демонстрационных сценариях, в тестах и при первом запуске системы.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentInfo, SweepConfig


@dataclass
class Emitter:
    """Виртуальный излучатель."""

    frequency_hz: float
    bandwidth_hz: float = 25_000.0
    level_db: float = -40.0
    amplitude_modulation: bool = False
    duty_cycle: float = 0.5
    period_s: float = 2.0


@dataclass
class SimulatedInstrument(Instrument):
    """Генератор спектра с набором виртуальных излучателей и шумом."""

    device_name: str = "Sim"
    emitters: list[Emitter] = field(default_factory=list)
    noise_db: float = -100.0
    noise_sigma_db: float = 2.0
    sweep_rate_hz_per_s: float = 2.2e9
    seed: int = 12345
    _rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(12345))
    _connected: bool = False
    _squelch: int = 0
    _volume: int = 0
    _center_hz: float = 0.0
    _elapsed_s: float = 0.0

    def connect(self) -> InstrumentInfo:
        self._rng = np.random.default_rng(self.seed)
        self._connected = True
        return InstrumentInfo(
            manufacturer="OpenRadioInspector",
            model="SimulatedInstrument",
            serial=f"SIM-{self.seed:06d}",
            firmware="1.0",
            interface="simulation",
        )

    def disconnect(self) -> None:
        self._connected = False

    def identify(self) -> InstrumentInfo:
        return InstrumentInfo(
            manufacturer="OpenRadioInspector",
            model="SimulatedInstrument",
            serial=f"SIM-{self.seed:06d}",
            firmware="1.0",
            interface="simulation",
        )

    def add_emitter(self, emitter: Emitter) -> None:
        self.emitters.append(emitter)

    def sweep(self, config: SweepConfig) -> Trace:
        """Формирует панораму с учётом текущего положения «источника»."""
        if not self._connected:
            self.connect()
        points = max(2, int(config.points))
        freqs = np.linspace(config.start_hz, config.stop_hz, points)
        levels = np.full(points, self.noise_db) + self._rng.normal(
            0.0, self.noise_sigma_db, points
        )

        for emitter in self.emitters:
            if emitter.amplitude_modulation:
                phase = (self._elapsed_s % emitter.period_s) / emitter.period_s
                if phase > emitter.duty_cycle:
                    continue
            half = max(emitter.bandwidth_hz / 2.0, config.step_hz)
            mask = np.abs(freqs - emitter.frequency_hz) <= half
            if not np.any(mask):
                continue
            profile = np.clip(
                1.0 - (np.abs(freqs[mask] - emitter.frequency_hz) / half) ** 2, 0.0, 1.0
            )
            levels[mask] = emitter.level_db + 12.0 * np.log10(np.maximum(profile, 1e-9))

        # Моделирование конечной полосы пропускания: сглаживание спектра.
        if config.resolution_bandwidth_hz > 0 and config.step_hz > 0:
            width = max(1, int(config.resolution_bandwidth_hz / config.step_hz))
            if width > 1:
                kernel = np.hanning(width + 2)[1:-1]
                kernel /= kernel.sum()
                padded = np.pad(levels, width // 2, mode="edge")
                levels = np.convolve(padded, kernel, mode="same")[width // 2 : width // 2 + points]

        self._elapsed_s += 1.0 / max(self.sweep_rate_hz_per_s, 1.0)
        return Trace(freqs, levels, "dBm")

    def set_center_frequency(self, frequency_hz: float) -> None:
        self._center_hz = frequency_hz

    def set_squelch(self, level: int) -> None:
        self._squelch = level

    def set_volume(self, level: int) -> None:
        self._volume = level

    @property
    def squelch(self) -> int:
        return self._squelch

    @property
    def volume(self) -> int:
        return self._volume
