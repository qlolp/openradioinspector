"""Анализ сетей Wi-Fi.

Исходный комплекс содержит отдельное приложение для поиска, регистрации и
анализа зон обслуживания беспроводных сетей Wi-Fi. Для каждой сети
определяются: идентификатор сети (SSID), MAC-адрес точки доступа, каналы
работы сети, уровень сигнала (дБм), качество приёма и характеристики доступа;
каждое измерение привязывается ко времени и географическим координатам.

Реализация использует перехват кадров в мониторном режиме. Библиотека
``scapy`` устанавливается отдельно; при её отсутствии доступен генератор
синтетических данных, достаточный для проверки интерфейса.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence


@dataclass
class WifiNetwork:
    """Зафиксированная беспроводная сеть."""

    ssid: str
    bssid: str
    channel: int
    level_dbm: float
    frequency_hz: Optional[float] = None
    encryption: str = ""
    quality: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    observed_at: Optional[datetime] = None

    def as_dict(self) -> dict:
        return {
            "ssid": self.ssid,
            "bssid": self.bssid,
            "channel": self.channel,
            "level_dbm": self.level_dbm,
            "frequency_hz": self.frequency_hz,
            "encryption": self.encryption,
            "quality": self.quality,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


#: Соответствие каналов Wi-Fi частотам для диапазонов 2,4 и 5 ГГц.
CHANNEL_FREQUENCIES = {
    **{ch: 2_407_000_000 + ch * 5_000_000 for ch in range(1, 14)},
    **{ch: 5_000_000_000 + ch * 5_000_000 for ch in range(32, 177, 2)},
}


def channel_frequency(channel: int) -> Optional[int]:
    """Частота канала Wi-Fi, Гц."""
    return CHANNEL_FREQUENCIES.get(channel)


def quality_from_level(level_dbm: float) -> float:
    """Оценка качества связи по уровню сигнала, проценты.

    Используется кусочно-линейная аппроксимация по шкале, принятой в
    утилитах анализа Wi-Fi: −30 дБм и выше — 100 %, −70 дБм и ниже — 0 %.
    """
    if level_dbm >= -30:
        return 100.0
    if level_dbm <= -70:
        return 0.0
    return (level_dbm + 70.0) / 40.0 * 100.0


class WifiScanner:
    """Сканер беспроводных сетей.

    Параметры
    ----------
    interface:
        Имя сетевого интерфейса в мониторном режиме.
    whitelist / blacklist:
        Фильтры по SSID: разрешённые и запрещённые сети.
    """

    def __init__(
        self,
        interface: str = "wlan0",
        whitelist: Optional[Sequence[str]] = None,
        blacklist: Optional[Sequence[str]] = None,
    ) -> None:
        self.interface = interface
        self.whitelist = set(whitelist or ())
        self.blacklist = set(blacklist or ())
        self.networks: dict[str, WifiNetwork] = {}

    def accepts(self, ssid: str) -> bool:
        """Проверка прохождения фильтров по SSID."""
        if self.blacklist and ssid in self.blacklist:
            return False
        if self.whitelist:
            return ssid in self.whitelist
        return True

    def register(
        self,
        network: WifiNetwork,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> Optional[WifiNetwork]:
        """Регистрирует наблюдение сети с учётом фильтров.

        Для каждой сети хранится последний зафиксированный уровень сигнала и
        число наблюдений.
        """
        if not self.accepts(network.ssid):
            return None
        network.quality = quality_from_level(network.level_dbm)
        network.observed_at = network.observed_at or datetime.now(timezone.utc)
        if latitude is not None:
            network.latitude = latitude
        if longitude is not None:
            network.longitude = longitude
        existing = self.networks.get(network.bssid)
        if existing is None:
            self.networks[network.bssid] = network
            return network
        existing.level_dbm = network.level_dbm
        existing.quality = network.quality
        existing.observed_at = network.observed_at
        if network.latitude is not None:
            existing.latitude = network.latitude
            existing.longitude = network.longitude
        return existing

    def snapshot(self) -> list[WifiNetwork]:
        """Текущий снимок наблюдаемых сетей, отсортированный по уровню."""
        return sorted(self.networks.values(), key=lambda n: n.level_dbm, reverse=True)

    def scan_with_scapy(
        self,
        duration_s: float = 10.0,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> list[WifiNetwork]:
        """Перехват кадров в мониторном режиме с помощью Scapy.

        Требуется установленный ``scapy`` и адаптер, переведённый в мониторный
        режим (например ``airmon-ng start wlan0``).
        """
        try:
            from scapy.all import Dot11, Dot11Beacon, Dot11Elt, RadioTap, sniff  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise RuntimeError(
                "Для сканирования Wi-Fi требуется библиотека scapy (pip install scapy)"
            ) from exc

        captured: list[WifiNetwork] = []

        def _handle(packet) -> None:
            if not packet.haslayer(Dot11Beacon):
                return
            bssid = packet[Dot11].addr2
            ssid = ""
            if packet.haslayer(Dot11Elt):
                try:
                    ssid = packet[Dot11Elt].info.decode("utf-8", errors="ignore")
                except Exception:
                    ssid = ""
            level = -100.0
            if packet.haslayer(RadioTap):
                level = float(getattr(packet, "dBm_AntSignal", -100))
            stats = {}
            try:
                stats = packet[Dot11Beacon].network_stats()
            except Exception:
                stats = {}
            channel = stats.get("channel")
            encryption = stats.get("crypto") or ""
            network = WifiNetwork(
                ssid=ssid,
                bssid=bssid or "",
                channel=int(channel) if channel else 0,
                level_dbm=level,
                frequency_hz=channel_frequency(int(channel)) if channel else None,
                encryption=str(encryption),
                latitude=latitude,
                longitude=longitude,
            )
            registered = self.register(network, latitude, longitude)
            if registered is not None:
                captured.append(registered)

        sniff(
            iface=self.interface,
            prn=_handle,
            timeout=duration_s,
            store=False,
        )
        return captured

    def coverage_by_channel(self) -> dict[int, float]:
        """Максимальный уровень сигнала по каждому каналу, дБм."""
        result: dict[int, float] = {}
        for network in self.networks.values():
            current = result.get(network.channel)
            if current is None or network.level_dbm > current:
                result[network.channel] = network.level_dbm
        return result


def synthetic_networks(seed: int = 0) -> list[WifiNetwork]:
    """Набор синтетических сетей для демонстрации и тестов."""
    import random

    rng = random.Random(seed)
    names = ["OpenRadioInspector", "lab-2.4G", "office-guest", "test-ap-5G"]
    result: list[WifiNetwork] = []
    for index, name in enumerate(names):
        channel = rng.choice(list(range(1, 14)) + list(range(36, 165, 4)))
        level = -40 - rng.random() * 45
        result.append(
            WifiNetwork(
                ssid=name,
                bssid=":".join(f"{rng.randrange(256):02X}" for _ in range(6)),
                channel=channel,
                level_dbm=level,
                frequency_hz=channel_frequency(channel),
                encryption=rng.choice(["WPA2", "WPA3", "Open"]),
            )
        )
    return result
