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
import math
from numbers import Real
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..trace import Trace

#: Версия спецификации SigMF, поддерживаемая модулем.
SIGMF_VERSION = "1.2.0"
_CHUNK_SAMPLES = 65536
_MAX_METADATA_BYTES = 1024 * 1024


def _finite(value, name, minimum=None):
    if not isinstance(value, Real) or isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result) or minimum is not None and result < minimum:
        raise ValueError(f"invalid {name}")
    return result


class SigmfPublicationError(OSError):
    """A failed rollback retains prior files in ``recovery_directory``."""
    def __init__(self, recovery_directory):
        self.recovery_directory = recovery_directory
        super().__init__(f"SigMF publication rollback failed; recovery files: {recovery_directory}")


def _publish(temporary, destinations):
    recovery = None
    snapshots = {}
    published = []
    keep_recovery = False
    try:
        for destination in destinations:
            try:
                mode = destination.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise ValueError("SigMF output must be a regular file, not a link or directory")
            if recovery is None:
                recovery = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".sigmf-recovery-"))
            backup = recovery / destination.name
            try:
                os.link(destination, backup)
            except OSError:
                shutil.copy2(destination, backup)
            snapshots[destination] = backup
        try:
            for source, destination in zip(temporary, destinations):
                os.replace(source, destination)
                published.append(destination)
        except BaseException as publication_error:
            for destination in reversed(published):
                try:
                    if destination in snapshots:
                        os.replace(snapshots[destination], destination)
                    else:
                        destination.unlink()
                except OSError:
                    keep_recovery = True
            if keep_recovery:
                raise SigmfPublicationError(recovery) from publication_error
            raise
    finally:
        if recovery is not None and not keep_recovery:
            shutil.rmtree(recovery)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
    sample_origin: str = "unspecified"
    source_level_unit: Optional[str] = None

    def _validate(self) -> np.ndarray:
        samples = self.samples
        if not isinstance(samples, np.ndarray) or np.ma.isMaskedArray(samples) \
                or samples.ndim != 1 or not samples.size \
                or samples.dtype.kind not in "iufc":
            raise ValueError("samples require a nonempty one-dimensional numeric NumPy array")
        if self.datatype not in {"cf32_le", "ci16_le", "rf32_le"}:
            raise ValueError("unsupported SigMF datatype")
        if self.sample_origin not in {"unspecified", "synthetic_spectrum_zero_phase"}:
            raise ValueError("unsupported SigMF sample origin")
        if self.source_level_unit is not None and (not isinstance(self.source_level_unit, str)
                or not 1 <= len(self.source_level_unit) <= 128):
            raise ValueError("invalid source level unit")
        if _finite(self.sample_rate_hz, "sample rate", 0) == 0:
            raise ValueError("sample rate must be positive")
        _finite(self.center_frequency_hz, "center frequency", 0)
        if self.captured_at is not None and (not isinstance(self.captured_at, datetime)
                or self.captured_at.utcoffset() is None):
            raise ValueError("capture time requires an explicit timezone")
        if self.location is not None:
            if len(self.location) != 2 or abs(_finite(self.location[0], "latitude")) > 90 \
                    or abs(_finite(self.location[1], "longitude")) > 180:
                raise ValueError("invalid geographic coordinates")
        for value in [self.author, self.hardware, self.description]:
            if not isinstance(value, str) or len(value) > 65536:
                raise ValueError("invalid SigMF text metadata")
        if not isinstance(self.annotations, list) or len(self.annotations) > 10000:
            raise ValueError("too many SigMF annotations")
        for annotation in self.annotations:
            if not isinstance(annotation, SigmfAnnotation) \
                    or type(annotation.sample_start) is not int \
                    or not 0 <= annotation.sample_start < samples.size:
                raise ValueError("annotation start must reference an existing sample")
            if annotation.sample_count is not None and (type(annotation.sample_count) is not int
                    or not 1 <= annotation.sample_count <= samples.size - annotation.sample_start):
                raise ValueError("annotation exceeds sample data")
            for text in [annotation.label, annotation.comment, annotation.generator]:
                if not isinstance(text, str) or len(text) > 65536:
                    raise ValueError("invalid annotation text")
            for name in ["freq_lower_edge_hz", "freq_upper_edge_hz"]:
                value = getattr(annotation, name)
                if value is not None: _finite(value, name, 0)
            if annotation.freq_lower_edge_hz is not None and annotation.freq_upper_edge_hz is not None \
                    and annotation.freq_lower_edge_hz > annotation.freq_upper_edge_hz:
                raise ValueError("annotation frequency interval is reversed")
        for start in range(0, samples.size, _CHUNK_SAMPLES):
            chunk = samples[start:start + _CHUNK_SAMPLES]
            if not np.isfinite(chunk).all():
                raise ValueError("nonfinite SigMF samples")
            if self.datatype == "ci16_le":
                for component in [chunk.real, chunk.imag]:
                    wide = np.asarray(component, dtype=np.longdouble)
                    if np.any(wide < -32768) or np.any(wide > 32767) \
                            or np.any(component != np.rint(component)):
                        raise ValueError("ci16 requires integral I/Q counts in [-32768, 32767]")
            else:
                if self.datatype == "rf32_le" and np.any(chunk.imag != 0):
                    raise ValueError("real SigMF datatype cannot discard a Q component")
                maximum = np.finfo(np.float32).max
                if np.any(np.abs(chunk.real) > maximum) or np.any(np.abs(chunk.imag) > maximum):
                    raise ValueError("sample cannot be represented as finite float32")
        return samples

    def _chunks(self, samples):
        for start in range(0, samples.size, _CHUNK_SAMPLES):
            chunk = samples[start:start + _CHUNK_SAMPLES]
            if self.datatype == "ci16_le":
                interleaved = np.empty(chunk.size * 2, dtype="<i2")
                interleaved[0::2], interleaved[1::2] = chunk.real, chunk.imag
                yield interleaved.tobytes()
            else:
                yield np.asarray(chunk if self.datatype == "cf32_le" else chunk.real,
                                 dtype="<c8" if self.datatype == "cf32_le" else "<f4").tobytes()

    def metadata(self) -> dict:
        """Объект метаданных SigMF."""
        self._validate()
        global_obj: dict = {
            "core:datatype": self.datatype,
            "core:sample_rate": float(self.sample_rate_hz),
            "core:version": SIGMF_VERSION,
            "core:recorder": self.author,
            "core:extensions": [{"name": "ori", "version": "1.0.0", "optional": True}],
            "ori:sample_origin": self.sample_origin,
            "ori:synthetic": self.sample_origin == "synthetic_spectrum_zero_phase",
            "ori:physical_capture_verified": False,
        }
        if self.source_level_unit is not None:
            global_obj["ori:source_level_unit"] = self.source_level_unit
        if self.hardware:
            global_obj["core:hw"] = self.hardware
        if self.description:
            global_obj["core:description"] = self.description
        if self.location is not None:
            global_obj["core:geolocation"] = {
                "type": "Point",
                "coordinates": [float(self.location[1]), float(self.location[0])],
            }

        capture = {
            "core:sample_start": 0,
            "core:frequency": float(self.center_frequency_hz),
        }
        if self.captured_at is not None:
            capture["core:datetime"] = _utc(self.captured_at)

        return {
            "global": global_obj,
            "captures": [capture],
            "annotations": [a.to_dict() for a in self.annotations],
        }

    def write(self, base_path: str | Path) -> tuple[Path, Path]:
        """Записывает пару файлов SigMF.

        Возвращает пути к файлу данных и файлу метаданных.
        """
        metadata = self.metadata()
        # Serialize metadata before creating output files. NaN or malformed
        # annotations must not leave an apparently usable recording behind.
        metadata["global"]["core:sha512"] = "0" * 128
        total = 0
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
        for fragment in encoder.iterencode(metadata):
            total += len(fragment.encode("utf-8"))
            if total > _MAX_METADATA_BYTES:
                raise ValueError("SigMF metadata exceeds 1 MiB")
        base = Path(base_path)
        data_path = base.with_suffix(".sigmf-data")
        meta_path = base.with_suffix(".sigmf-meta")
        base.parent.mkdir(parents=True, exist_ok=True)
        temporary = []
        try:
            with tempfile.NamedTemporaryFile(dir=base.parent, prefix=".sigmf-", delete=False) as data:
                temporary.append(Path(data.name))
                digest = hashlib.sha512()
                for chunk in self._chunks(self.samples):
                    data.write(chunk)
                    digest.update(chunk)
                data.flush(); os.fsync(data.fileno())
            metadata["global"]["core:sha512"] = digest.hexdigest()
            with tempfile.NamedTemporaryFile(dir=base.parent, prefix=".sigmf-", mode="w",
                    encoding="utf-8", newline="\n", delete=False) as meta:
                temporary.append(Path(meta.name))
                json.dump(metadata, meta, ensure_ascii=False, indent=2, allow_nan=False)
                meta.flush(); os.fsync(meta.fileno())
            _publish(temporary, [data_path, meta_path])
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)
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
    if trace.levels.ndim != 1 or trace.frequencies.ndim != 1 or trace.empty \
            or not np.isfinite(trace.levels).all() or not np.isfinite(trace.frequencies).all():
        raise ValueError("synthesis requires a finite nonempty source spectrum")
    with np.errstate(over="raise", invalid="raise"):
        try:
            amplitudes = np.power(10.0, trace.levels / 20.0)
        except FloatingPointError as error:
            raise ValueError("spectrum amplitude cannot be represented") from error
    samples = amplitudes.astype(np.complex64)
    recording = SigmfRecording(
        samples=samples,
        sample_rate_hz=sample_rate_hz,
        center_frequency_hz=float(0.5 * (trace.start + trace.stop)),
        captured_at=captured_at,
        description=description,
        location=location,
        sample_origin="synthetic_spectrum_zero_phase",
        source_level_unit=trace.unit,
    )
    return recording.write(path)
