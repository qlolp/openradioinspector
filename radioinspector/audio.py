"""Запись и анализ демодулированного аудиосигнала.

Исходный комплекс записывает демодулированный низкочастотный аудиосигнал
через звуковую карту компьютера, ведёт низкочастотный осциллограф и анализатор
спектра демодулированного сигнала, а также редактирует записанные фонограммы.
Реализованы:

* захват аудио с устройства ввода (модуль ``sounddevice``, устанавливается
  отдельно) с буфером предварительной записи;
* низкочастотный осциллограф: отображение отрезка сигнала длительностью до
  5 с при частотах дискретизации 8, 12, 24 и 48 кГц;
* непрерывный и третьоктавный спектр демодулированного сигнала;
* критерий «сигнал пропал» по третьоктавным полосам: за критерий принимается
  отсутствие изменений в каждой полосе более чем на половину математического
  ожидания в линейных величинах;
* сохранение фонограммы в несжатом WAV-файле.
"""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

#: Частоты дискретизации, поддерживаемые низкочастотным осциллографом.
SAMPLE_RATES = (8_000, 12_000, 24_000, 48_000)

#: Максимальная длительность отображаемого отрезка сигнала, с.
MAX_SCOPE_SECONDS = 5.0

#: Границы третьоктавных полос для анализа спектра аудиосигнала.
THIRD_OCTAVE_BANDS = (
    (20.0, 25.0),
    (25.0, 31.5),
    (31.5, 40.0),
    (40.0, 50.0),
    (50.0, 63.0),
    (63.0, 80.0),
    (80.0, 100.0),
    (100.0, 125.0),
    (125.0, 160.0),
    (160.0, 200.0),
    (200.0, 250.0),
    (250.0, 315.0),
    (315.0, 400.0),
    (400.0, 500.0),
    (500.0, 630.0),
    (630.0, 800.0),
    (800.0, 1000.0),
    (1000.0, 1250.0),
    (1250.0, 1600.0),
    (1600.0, 2000.0),
    (2000.0, 2500.0),
    (2500.0, 3150.0),
    (3150.0, 4000.0),
    (4000.0, 5000.0),
)


@dataclass
class AudioCapture:
    """Результат захвата аудиосигнала."""

    samples: np.ndarray
    sample_rate_hz: int
    captured_at: Optional[str] = None

    @property
    def duration_s(self) -> float:
        if self.sample_rate_hz <= 0:
            return 0.0
        return float(self.samples.size / self.sample_rate_hz)

    def write_wav(self, path: str | Path) -> Path:
        """Сохранение фонограммы в несжатом WAV-файле."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = np.asarray(self.samples, dtype=np.float64)
        data = np.clip(data, -1.0, 1.0)
        pcm = (data * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate_hz)
            handle.writeframes(pcm.tobytes())
        return path


class LowFrequencyScope:
    """Низкочастотный осциллограф демодулированного сигнала."""

    def __init__(self, sample_rate_hz: int = 48_000) -> None:
        if sample_rate_hz not in SAMPLE_RATES:
            raise ValueError(
                f"Частота дискретизации должна быть одной из {SAMPLE_RATES}"
            )
        self.sample_rate_hz = sample_rate_hz
        self._buffer = np.zeros(0)

    def push(self, samples: np.ndarray) -> None:
        """Добавление новых отсчётов в кольцевой буфер осциллографа."""
        self._buffer = np.concatenate([self._buffer, np.asarray(samples, dtype=float)])

    def window(self, seconds: float = 1.0) -> np.ndarray:
        """Последний отрезок сигнала заданной длительности."""
        seconds = min(max(seconds, 0.0), MAX_SCOPE_SECONDS)
        count = int(seconds * self.sample_rate_hz)
        if count <= 0 or self._buffer.size == 0:
            return np.zeros(0)
        return self._buffer[-count:]

    def clear(self) -> None:
        self._buffer = np.zeros(0)


class AudioRecorder:
    """Запись демодулированного аудиосигнала через устройство ввода.

    Параметр ``prebuffer_s`` задаёт длительность фрагмента, предшествующего
    моменту начала записи: он сохраняется в файл при ручном запуске записи,
    что позволяет зафиксировать уже прозвучавшую информацию.
    """

    def __init__(
        self,
        sample_rate_hz: int = 48_000,
        device: Optional[int | str] = None,
        prebuffer_s: float = 5.0,
        channels: int = 1,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.device = device
        self.prebuffer_s = prebuffer_s
        self.channels = channels
        self._ring: list[np.ndarray] = []
        self._ring_size = 0
        self._stream = None

    def _ensure_stream(self):
        if self._stream is not None:
            return self._stream
        try:
            import sounddevice  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise RuntimeError(
                "Для записи аудио требуется модуль sounddevice "
                "(pip install sounddevice)"
            ) from exc
        self._stream = sounddevice.RawInputStream(
            samplerate=self.sample_rate_hz,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            blocksize=1024,
        )
        self._stream.start()
        return self._stream

    def _callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover
        if status:
            return
        block = np.asarray(indata).reshape(-1).copy()
        self._ring.append(block)
        self._ring_size += block.size
        limit = int(self.prebuffer_s * self.sample_rate_hz)
        while self._ring_size > limit and len(self._ring) > 1:
            removed = self._ring.pop(0)
            self._ring_size -= removed.size

    def start(self) -> None:
        """Запуск непрерывного захвата в фоновом режиме."""
        stream = self._ensure_stream()
        stream.callback = self._callback
        stream.start()

    def stop(self) -> None:
        """Остановка захвата."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def take(self) -> AudioCapture:
        """Возврат накопленного фрагмента с последующей очисткой буфера."""
        if not self._ring:
            return AudioCapture(np.zeros(0), self.sample_rate_hz)
        samples = np.concatenate(self._ring)
        self._ring.clear()
        self._ring_size = 0
        return AudioCapture(samples, self.sample_rate_hz)


def third_octave_levels(
    samples: np.ndarray, sample_rate_hz: int
) -> list[tuple[float, float, float]]:
    """Уровни в третьоктавных полосах.

    Возвращает список кортежей (нижняя граница, верхняя граница, уровень в дБ)
    для каждой полосы, в пределах которой сигнал имеет мощность.
    """
    if samples.size < 8 or sample_rate_hz <= 0:
        return []
    spectrum = np.abs(np.fft.rfft(samples)) ** 2
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate_hz)
    total = float(np.sum(spectrum))
    result: list[tuple[float, float, float]] = []
    for low, high in THIRD_OCTAVE_BANDS:
        if low > freqs[-1]:
            break
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        power = float(np.sum(spectrum[mask]))
        level = 10.0 * math.log10(max(power / max(total, 1e-30), 1e-30))
        result.append((low, high, level))
    return result


def signal_disappeared(
    band_levels: Sequence[float],
    reference: Sequence[float],
    tolerance: float = 0.5,
) -> bool:
    """Критерий «сигнал пропал» по третьоктавным полосам.

    За критерий принимается отсутствие изменений в каждой полосе более чем на
    половину математического ожидания в линейных величинах. Возвращает
    ``True``, если сигнал считается исчезнувшим.
    """
    if not band_levels or not reference:
        return False
    count = min(len(band_levels), len(reference))
    changed = 0
    for index in range(count):
        current = math.pow(10.0, band_levels[index] / 10.0)
        expected = math.pow(10.0, reference[index] / 10.0)
        if expected <= 0:
            continue
        if abs(current - expected) / expected > tolerance:
            changed += 1
    return changed == 0


def spectrum_of_audio(
    samples: np.ndarray, sample_rate_hz: int
) -> tuple[np.ndarray, np.ndarray]:
    """Спектр демодулированного аудиосигнала.

    Возвращает массивы частот (Гц) и уровней (дБ).
    """
    if samples.size < 8:
        return np.array([]), np.array([])
    window = np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(samples * window)) ** 2
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate_hz)
    levels = 10.0 * np.log10(np.maximum(spectrum, 1e-30))
    return freqs, levels


def waveform_filename(
    timestamp: str, frequency_hz: float, bandwidth_hz: Optional[float] = None
) -> str:
    """Правило формирования имени файла фонограммы.

    Имя образуется как «дата, время, частота [(полоса занимаемых частот)]»;
    при автоматическом запуске записи добавляется префикс ``a_``.
    """
    name = f"{timestamp} {frequency_hz:.6f}"
    if bandwidth_hz:
        name += f" ({bandwidth_hz:.1f})"
    return name.replace(":", "_").replace(" ", "_") + ".wav"


def default_recording_directory(base: str | Path) -> Path:
    """Каталог хранения фонограмм."""
    return Path(base) / "WAVE"
