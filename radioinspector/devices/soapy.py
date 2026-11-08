"""Подключение SDR-приёмников через SoapySDR.

SoapySDR — единый аппаратно-независимый интерфейс к программно-определяемым
радиоприёмникам; через него работают драйверы USRP, LimeSDR, bladeRF, HackRF,
RTL-SDR и другие. Библиотека устанавливается отдельно
(вместе с Python bindings), при её отсутствии драйвер сообщает об ошибке.

Калиброванный sweep пока не реализован: некалиброванные IQ-отсчёты
не выдаются за физические dBm. Поддерживаются подключение и настройка RX.
"""

from __future__ import annotations

from ..trace import Trace
from .base import Instrument, InstrumentError, InstrumentInfo, SweepConfig


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
        self._active = False
        self._info = InstrumentInfo(interface=f"soapy:{device_args or 'auto'}")

    def connect(self) -> InstrumentInfo:
        if self._device is not None:
            return self._info
        try:
            import SoapySDR  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise InstrumentError("Для работы с SDR требуется SoapySDR и его Python bindings") from exc

        self._SoapySDR = SoapySDR
        try:
            self._device = SoapySDR.Device(self.device_args)
            if self._device is None:
                raise InstrumentError(f"Устройство не найдено: {self.device_args}")
            rx = self._SoapySDR.SOAPY_SDR_RX
            self._device.setSampleRate(rx, 0, self.sample_rate)
            self._device.setFrequency(rx, 0, self.center_frequency_hz)
            # gain_db is manual gain; do not silently enable AGC afterwards.
            if self._device.hasGainMode(rx, 0):
                self._device.setGainMode(rx, 0, False)
            self._device.setGain(rx, 0, self.gain_db)
            self._stream = self._device.setupStream(rx, self._SoapySDR.SOAPY_SDR_CF32, [0])
            if self._stream is None:
                raise InstrumentError("SoapySDR: setupStream не вернул поток")
            status = self._device.activateStream(self._stream)
            if status != 0:
                raise InstrumentError(f"SoapySDR: activateStream вернул {status}")
            self._active = True
            hardware = self._device.getHardwareInfo()
            self._info = InstrumentInfo(
                manufacturer=str(hardware.get("origin", "")),
                model=str(hardware.get("label", "SDR")),
                interface=f"soapy:{self.device_args or 'auto'}",
            )
        except Exception as exc:
            self.disconnect()
            raise InstrumentError(f"SoapySDR: ошибка подключения: {exc}") from exc
        return self._info

    def disconnect(self) -> None:
        device, stream, active = self._device, self._stream, self._active
        self._device = self._stream = None
        self._active = False
        if device is None:
            return
        if stream is not None:
            if active:
                try:
                    device.deactivateStream(stream)
                except Exception:
                    pass
            # A deactivation failure must not prevent stream/device release.
            try:
                device.closeStream(stream)
            except Exception:
                pass
        try:
            # Python close() also marks the wrapper closed for its destructor.
            device.close()
        except Exception:
            pass

    def identify(self) -> InstrumentInfo:
        return self._info

    def sweep(self, config: SweepConfig) -> Trace:
        """Не выдавать некалиброванные IQ за физические уровни мощности."""
        raise InstrumentError(
            "SoapySDR: sweep недоступен без реализованного калибровочного профиля; "
            "некалиброванные IQ нельзя представлять в dBm"
        )

    def set_center_frequency(self, frequency_hz: float) -> None:
        if self._device is not None:
            try:
                self._device.setFrequency(self._SoapySDR.SOAPY_SDR_RX, 0, frequency_hz)
            except Exception as exc:
                self.disconnect()
                raise InstrumentError(f"SoapySDR: ошибка перестройки: {exc}") from exc
        self.center_frequency_hz = frequency_hz
