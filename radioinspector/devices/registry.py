"""Реестр средств измерений и фабрика подключений.

Строки подключения имеют следующий вид:

===========================  =================================================
``scpi://host[:port]``       SCPI поверх TCP-сокета (порт 5025 по умолчанию)
``vxi11://host``             VXI-11 / LXI через внешнюю библиотеку
``serial://COM3:9600``       SCPI через COM-порт
``soapy://<args>``           SDR через SoapySDR
``rtl_tcp://host:port``      SDR через rtl_tcp
``sim://``                   Встроенный генератор измерений
===========================  =================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .base import Instrument, InstrumentError


@dataclass
class DeviceDescriptor:
    """Описание поддерживаемого типа средства измерений."""

    key: str
    title: str
    vendor: str
    transport: str
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "vendor": self.vendor,
            "transport": self.transport,
            "notes": self.notes,
        }


#: Перечень приборов, работающих с исходным комплексом (по руководству
#: «Особенности подключения разных приборов»). Приведённые значения
#: отражают заявленный производителем состав поддерживаемого оборудования.
SUPPORTED_DEVICE_FAMILIES: list[DeviceDescriptor] = [
    DeviceDescriptor("scpi", "Анализаторы спектра с интерфейсом SCPI", "Универсальный", "SCPI over TCP",
                     "Agilent/Keysight CXA, EXA, MXA; Rohde&Schwarz; Tektronix; SignalHound; Anritsu; Aeroflex; Advantest; Narda"),
    DeviceDescriptor("vxi11", "Приборы по протоколу VXI-11/LXI", "Универсальный", "VXI-11",
                     "Управление по сети через мост GPIB или LXI-приборы"),
    DeviceDescriptor("serial", "Приборы с последовательным интерфейсом", "Универсальный", "COM",
                     "IFR 2394/2395/2397; HP 8560–8565; AOR; ICOM"),
    DeviceDescriptor("soapy", "Программно-определяемые радиоприёмники", "Универсальный", "SoapySDR",
                     "USRP B200/B210; LimeSDR; bladeRF; HackRF; RTL-SDR; Airspy"),
    DeviceDescriptor("rtl_tcp", "SDR через rtl_tcp", "Realtek", "TCP",
                     "Совместимость с rtl_tcp и rtl_power"),
    DeviceDescriptor("sim", "Встроенный генератор измерений", "OpenRadioInspector", "Внутренний",
                     "Работа без оборудования: демонстрация, тесты, отладка"),
]


def open_instrument(connection: str, **kwargs) -> Instrument:
    """Создаёт средство измерений по строке подключения."""
    scheme, _, remainder = connection.partition("://")
    scheme = scheme.lower()

    if scheme == "sim":
        from .simulator import SimulatedInstrument

        return SimulatedInstrument(**kwargs)

    if scheme == "scpi":
        from .scpi import ScpiInstrument

        host, _, port = remainder.partition(":")
        return ScpiInstrument(host, int(port) if port else 5025, **kwargs)

    if scheme == "vxi11":
        from .scpi import Vxi11Instrument

        host, _, resource = remainder.partition("/")
        return Vxi11Instrument(host, resource)

    if scheme == "serial":
        from .serial_scpi import SerialScpiInstrument

        port, _, baud = remainder.partition(":")
        return SerialScpiInstrument(port, int(baud) if baud else 9600, **kwargs)

    if scheme == "soapy":
        from .soapy import SoapyInstrument

        return SoapyInstrument(remainder, **kwargs)

    if scheme in ("rtl_tcp", "rtltcp"):
        from .rtl import RtlTcpInstrument

        host, _, port = remainder.partition(":")
        return RtlTcpInstrument(host, int(port) if port else 1234, **kwargs)

    raise InstrumentError(f"Неизвестная схема подключения: {connection}")


def list_schemes() -> list[str]:
    """Перечень поддерживаемых схем подключения."""
    return [d.key for d in SUPPORTED_DEVICE_FAMILIES]


def describe_devices() -> list[dict]:
    """Описание поддерживаемых типов оборудования."""
    return [d.as_dict() for d in SUPPORTED_DEVICE_FAMILIES]
