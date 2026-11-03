"""Подключение SDR-приёмников через SoapySDR.

SoapySDR — единый аппаратно-независимый интерфейс к программно-определяемым
радиоприёмникам; через него работают драйверы USRP, LimeSDR, bladeRF, HackRF,
RTL-SDR и другие. Библиотека устанавливается отдельно
(``pip install SoapySDR``), при её отсутствии драйвер сообщает об ошибке.

Измеренная полоса вычисляется как суммарная мощность (в мВт) на частоте
перестройки; матрица измерений формируется сдвигом по частоте с шагом,
кратным шагу перестройки приёмника.
"""

from __future__ import annotations

import cmath
import math
from typing import Optional

import numpy as np

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig

#: Размер блока БПФ.
FFT_SIZE = 1024

#: Число накоплений одной точки частотной сетки.
AVERAGES = 4


class SoapyInstrument(Instrument):
    """Приёмник SDR через SoapySDR."""

    device_name = "SoapySDR"

    def __init__(
        self,
        device_args: str = "",
        sample_rate: float = 2.0e6,
        center_frequency_hz: float = 100.0e6,
        gain_db: float = 20.0,
    ) -> None:
        self.device_args = device_args
        self.sample_rate = sample_rate
        self.center_frequency_hz = center_frequency_hz
        self.gain_db = gain_db
        self._device = None
        self._stream = None
        self._info = InstrumentInfo(interface=f"soapy:{device_args or 'auto'}")

    def connect(self) -> InstrumentInfo:
        try:
            import SoapySDR  # type: ignore
            from SoapySDR import SOAPY_SDR_CF32  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise InstrumentError(
                "Для работы с SDR требуется SoapySDR "
                "(apt install soapysdr-tools / pip install SoapySDR)"
            ) from exc

        self._SoapySDR = SoapySDR
        self._device = SoapySDR.Device.make(self.device_args)
        if self._device is None:
            raise InstrumentError(f"Устройство не найдено: {self.device_args}")

        self._device.setSampleRate(SOAPY_SDR_RX, 0, self.sample_rate)
        self._device.setFrequency(SOAPY_SDR_RX, 0, self.center_frequency_hz)
        self._device.setGain(SOAPY_SDR_RX, 0, self.gain_db)
        self._device.setGainMode(SOAPY_SDR_RX, 0, True)

        self._stream = self._device.setupStream(
            SOAPY_SDR_RX, SOAPY_SDR_CF32, [0]
        )
        self._stream.activate()
        self._info = InstrumentInfo(
            manufacturer=str(self._device.getHardwareInfo().get("origin", "")),
            model=str(self._device.getHardwareInfo().get("label", "SDR")),
            serial="",
            firmware="",
            interface=f"soapy:{self.device_args or 'auto'}",
        )
        return self._info

    def disconnect(self) -> None:
        if self._stream is not None:
            try:
                self._stream.deactivate()
                self._stream.close()
            except Exception:  # pragma: no cover - зависит от устройства
                pass
            self._stream = None
        if self._device is not None:
            try:
                self._device.unmake()
            except Exception:  # pragma: no cover - зависит от устройства
                pass
            self._device = None

    def identify(self) -> InstrumentInfo:
        return self._info

    def sweep(self, config: SweepConfig) -> Trace:
        """Измерение панорамы перестройкой частоты."""
        if self._device is None:
            self.connect()

        points = max(2, int(config.points))
        freqs = np.linspace(config.start_hz, config.stop_hz, points)
        levels = np.zeros(points)

        for index, freq in enumerate(freqs):
            self._device.setFrequency(self._SoapySDR.SOAPY_SDR_RX, 0, float(freq))
            total_power = 0.0
            for _ in range(AVERAGES):
                buffer = np.zeros(FFT_SIZE, dtype=np.complex64)
                flags = self._SoapySDR.SOAPY_SDR_END_BURST | self._SoapySDR.SOAPY_SDR_END_ABRUPT
                received = self._stream.readStream(buffer, flags, timeout_us=200_000)
                if received <= 0:
                    continue
                samples = buffer[:received]
                spectrum = np.fft.fftshift(np.abs(np.fft.fft(samples)) ** 2)
                total_power += float(np.mean(spectrum))
            # Перевод суммарной мощности в эквивалентный уровень на частоте.
            levels[index] = 10.0 * math.log10(max(total_power, 1e-30)) - 10.0 * math.log10(FFT_SIZE)

        return Trace(freqs, levels, "dBm")

    def set_center_frequency(self, frequency_hz: float) -> None:
        self.center_frequency_hz = frequency_hz
        if self._device is not None:
            self._device.setFrequency(self._SoapySDR.SOAPY_SDR_RX, 0, frequency_hz)
