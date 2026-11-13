"""Экспорт записанных сигналов в формат SigMF.

SigMF (Signal Metadata Format) — открытый стандарт записи радиосигналов,
версия 1.2.0. Запись представляет собой два файла с общим именем:

* ``<имя>.sigmf-data`` — двоичные отсчёты сигнала;
* ``<имя>.sigmf-meta`` — JSON-файл с метаданными.

Метаданные содержат три объекта: ``global`` (свойства всей записи),
``captures`` (сегменты захвата с частотой и временем) и ``annotations``
(размеченные области сигнала). Метаданные позволяют сторонним средствам
корректно интерпретировать запись без потери сведений о захвате.

Спецификация: https://github.com/sigmf/SigMF
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..trace import Trace

#: Версия спецификации SigMF, поддерживаемая модулем.
SIGMF_VERSION = "1.2.0"


@dataclass
class SigmfAnnotation:
    """Размеченная область сигнала."""

    sample_start: int
    sample_count: Optional[int] = None
    freq_lower_edge_hz: Optional[float] = None
    freq_upper_edge_hz: Optional[float] = None
    label: str = ""
    comment: str = ""
    generator: str = "OpenRadioInspector"

    def to_dict(self) -> dict:
        payload: dict = {
            "core:sample_start": int(self.sample_start),
            "core:label": self.label,
            "core:generator": self.generator,
        }
        if self.sample_count is not None:
            payload["core:sample_count"] = int(self.sample_count)
        if self.freq_lower_edge_hz is not None:
            payload["core:freq_lower_edge_hz"] = float(self.freq_lower_edge_hz)
        if self.freq_upper_edge_hz is not None:
            payload["core:freq_upper_edge_hz"] = float(self.freq_upper_edge_hz)
        if self.comment:
            payload["core:comment"] = self.comment
        return payload


@dataclass
class SigmfRecording:
    """Запись сигнала в формате SigMF."""

    samples: np.ndarray
    sample_rate_hz: float
    center_frequency_hz: float
    captured_at: Optional[datetime] = None
    datatype: str = "cf32_le"
    author: str = "OpenRadioInspector"
    hardware: str = ""
    description: str = ""
    location: Optional[tuple[float, float]] = None
    annotations: list[SigmfAnnotation] = field(default_factory=list)

    def metadata(self) -> dict:
        """Объект метаданных SigMF."""
        global_obj: dict = {
            "core:datatype": self.datatype,
            "core:sample_rate": float(self.sample_rate_hz),
            "core:version": SIGMF_VERSION,
            "core:recorder": self.author,
        }
        if self.captured_at is not None:
            global_obj["core:datetime"] = self.captured_at.astimezone(
                timezone.utc
            ).isoformat()
        if self.hardware:
            global_obj["core:hw"] = self.hardware
        if self.description:
            global_obj["core:description"] = self.description
        if self.location is not None:
            global_obj["core:geolocation"] = {
                "type": "Point",
                "coordinates": [self.location[1], self.location[0]],
            }

        capture = {
            "core:sample_start": 0,
            "core:frequency": float(self.center_frequency_hz),
        }
        if self.captured_at is not None:
            capture["core:datetime"] = self.captured_at.astimezone(
                timezone.utc
            ).isoformat()

        return {
            "global": global_obj,
            "captures": [capture],
            "annotations": [a.to_dict() for a in self.annotations],
        }

    def write(self, base_path: str | Path) -> tuple[Path, Path]:
        """Записывает пару файлов SigMF.

        Возвращает пути к файлу данных и файлу метаданных.
        """
        base = Path(base_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        data_path = base.with_suffix(".sigmf-data")
        meta_path = base.with_suffix(".sigmf-meta")

        if self.datatype == "cf32_le":
            payload = np.asarray(self.samples, dtype=np.complex64)
        elif self.datatype == "ci16_le":
            payload = np.asarray(self.samples, dtype=np.complex64)
        else:
            payload = np.asarray(self.samples, dtype=np.float32)

        payload.tofile(data_path)

        metadata = self.metadata()
        digest = hashlib.sha512(data_path.read_bytes()).hexdigest()
        metadata["global"]["core:sha512"] = digest
        meta_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return data_path, meta_path


def export_sigmf(
    trace: Trace,
    path: str | Path,
    sample_rate_hz: float = 1.0e6,
    captured_at: Optional[datetime] = None,
    description: str = "",
    location: Optional[tuple[float, float]] = None,
) -> tuple[Path, Path]:
    """Формирует запись SigMF из спектральной характеристики.

    Уровни спектра преобразуются в комплексные отсчёты с нулевой фазой;
    амплитуда соответствует уровню сигнала. Такой синтез пригоден для
    демонстрации формата и проверки совместимости сторонних средств.
    """
    amplitudes = np.power(10.0, trace.levels / 20.0)
    samples = amplitudes.astype(np.complex64)
    recording = SigmfRecording(
        samples=samples,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=float(0.5 * (trace.start + trace.stop)),
        captured_at=captured_at,
        description=description,
        location=location,
    )
    return recording.write(path)
