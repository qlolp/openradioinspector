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
from urllib.parse import parse_qsl

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
    scheme, path, params = split_connection(connection)
    profile = params.get("instrument", "")

    if scheme == "sim":
        from .simulator import SimulatedInstrument

        return SimulatedInstrument(**kwargs)

    if scheme == "scpi":
        from .scpi import ScpiInstrument

        host, port = host_port(path, 5025)
        return ScpiInstrument(host, port, profile=profile, **kwargs)

    if scheme == "vxi11":
        from .scpi import Vxi11Instrument

        host, _, resource = path.partition("/")
        return Vxi11Instrument(host, resource)

    if scheme == "serial":
        from .serial_scpi import SerialScpiInstrument

        port, baud = host_port(path, 9600)
        return SerialScpiInstrument(port, baud, **kwargs)

    if scheme == "soapy":
        from .soapy import SoapyInstrument

        return SoapyInstrument(path, **kwargs)

    if scheme in ("rtl_tcp", "rtltcp"):
        from .rtl import RtlTcpInstrument

        host, port = host_port(path, 1234)
        return RtlTcpInstrument(host, port, **kwargs)

    raise InstrumentError(f"Неизвестная схема подключения: {connection}")


def list_schemes() -> list[str]:
    """Перечень поддерживаемых схем подключения."""
    return [d.key for d in SUPPORTED_DEVICE_FAMILIES]


def describe_devices() -> list[dict]:
    """Описание поддерживаемых типов оборудования."""
    return [d.as_dict() for d in SUPPORTED_DEVICE_FAMILIES]


def split_connection(connection: str) -> tuple[str, str, dict[str, str]]:
    """Разбирает строку подключения на схему, путь и параметры запроса.

    Параметры после ``?`` не попадают в номер порта. Для SoapySDR строка
    аргументов сохраняется целиком: знак ``?`` там может быть частью
    аргументов драйвера.
    """
    scheme, separator, remainder = connection.partition("://")
    if not separator or not scheme:
        raise InstrumentError(f"Неизвестная схема подключения: {connection}")
    scheme = scheme.lower()
    if scheme == "soapy":
        return scheme, remainder, {}
    path, _, query = remainder.partition("?")
    params = dict(parse_qsl(query, keep_blank_values=False))
    return scheme, path, params


def host_port(path: str, default_port: int) -> tuple[str, int]:
    """Отделяет порт, если последний фрагмент после ``:`` состоит из цифр."""
    host, separator, port = path.rpartition(":")
    if not separator or not port.isdigit():
        return path, default_port
    return host, int(port)


#: Фрагменты ответа ``*IDN?`` и имя профиля. Профиль запоминается для
#: оператора; набор команд сканирования остаётся общим.
_IDN_PROFILES: tuple[tuple[str, str], ...] = (
    ("n9020", "keysight-mx"),
    ("n9010", "keysight-mx"),
    ("n9000", "keysight-mx"),
    ("n9342", "keysight-field"),
    ("n9340", "keysight-field"),
    ("n991", "keysight-fieldfox"),
    ("n996", "keysight-fieldfox"),
    ("fsq", "rohde-schwarz"),
    ("fsp", "rohde-schwarz"),
    ("fsh", "rohde-schwarz"),
    ("esci", "rohde-schwarz"),
    ("eb200", "rohde-schwarz"),
    ("pr100", "rohde-schwarz"),
    ("em100", "rohde-schwarz"),
    ("ms272", "anritsu"),
    ("ms271", "anritsu"),
    ("2397", "ifr"),
    ("2395", "ifr"),
    ("2394", "ifr"),
    ("ar-dv1", "aor"),
    ("sr2200", "aor"),
    ("bb60", "signalhound"),
    ("sa124", "signalhound"),
    ("rsa306", "tektronix"),
)


def profile_from_idn(idn: str) -> str:
    """Имя профиля по ответу ``*IDN?``. Пустая строка — профиль не опознан."""
    text = idn.lower()
    for needle, profile in _IDN_PROFILES:
        if needle in text:
            return profile
    return ""
