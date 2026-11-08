"""Phonogram recording with a fixed five-second pre-history window.

Backlog item 9: «Запись фонограмм с пятисекундным пред-history» —
предыстория записи. The recorder keeps a rolling window of the latest
audio frames; an explicit start freezes that window as the recording's
prefix, so the phonogram already contains what sounded before the
operator pressed record.

The recorder is runtime state per session: frames come from a device
worker or a test harness, nothing is written to the archive, and the
source samples are never modified. The result is a derived artifact:
pre-history plus recorded frames, with the split reported explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import numpy as np

from .audio import SAMPLE_RATES
from .contracts import to_primitive, utc_now

#: Фиксированная длительность предыстории записи, как в оригинале.
PRE_HISTORY_SECONDS = 5.0

#: Максимум отсчётов в одном вызове push.
MAX_PUSH_SAMPLES = 480_000

#: Максимум общей длительности фонограммы, с.
MAX_PHONOGRAM_SECONDS = 600.0


class PhonogramError(ValueError):
    """The recorder state or frames cannot support this operation."""


@dataclass
class Phonogram:
    """Готовая фонограмма: предыстория плюс записанная часть."""

    sample_rate_hz: int
    pre_history_samples: int
    started_at: datetime
    ended_at: datetime
    samples: np.ndarray = field(repr=False)

    @property
    def recorded_samples(self) -> int:
        return int(self.samples.size) - self.pre_history_samples

    @property
    def pre_history_seconds(self) -> float:
        return self.pre_history_samples / self.sample_rate_hz

    @property
    def recorded_seconds(self) -> float:
        return self.recorded_samples / self.sample_rate_hz

    @property
    def duration_seconds(self) -> float:
        return float(self.samples.size) / self.sample_rate_hz

    def as_dict(self) -> dict[str, Any]:
        """Итог без сырых отсчётов: клиенты не получают аудио в JSON."""
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "pre_history_samples": self.pre_history_samples,
            "recorded_samples": self.recorded_samples,
            "pre_history_seconds": self.pre_history_seconds,
            "recorded_seconds": self.recorded_seconds,
            "duration_seconds": self.duration_seconds,
            "started_at": to_primitive(self.started_at),
            "ended_at": to_primitive(self.ended_at),
        }

    def wav_bytes(self) -> bytes:
        """Несжатый моно WAV 16 бит, как сохранение фонограммы в файл."""
        import io
        import wave

        data = np.clip(np.asarray(self.samples, dtype=np.float64), -1.0, 1.0)
        pcm = (data * 32767.0).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate_hz)
            handle.writeframes(pcm.tobytes())
        return buffer.getvalue()


class PhonogramRecorder:
    """Кольцевой буфер предыстории и явный жизненный цикл записи."""

    def __init__(
        self,
        sample_rate_hz: int = 48_000,
        *,
        pre_history_seconds: float = PRE_HISTORY_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if sample_rate_hz not in SAMPLE_RATES:
            raise PhonogramError(f"Частота дискретизации должна быть одной из {SAMPLE_RATES}")
        if pre_history_seconds != PRE_HISTORY_SECONDS:
            raise PhonogramError(f"Предыстория записи фиксирована: {PRE_HISTORY_SECONDS} с")
        self.sample_rate_hz = sample_rate_hz
        self.pre_history_seconds = pre_history_seconds
        self._clock = clock
        self._ring: list[np.ndarray] = []
        self._ring_samples = 0
        self._recording: Optional[list[np.ndarray]] = None
        self._pre_samples = 0
        self._recorded_samples = 0
        self._started_at: Optional[datetime] = None
        self._last: Optional[Phonogram] = None

    def _ring_limit(self) -> int:
        return int(self.pre_history_seconds * self.sample_rate_hz)

    @property
    def recording(self) -> bool:
        return self._recording is not None

    @property
    def buffered_seconds(self) -> float:
        return self._ring_samples / self.sample_rate_hz

    def push(self, samples: Any) -> int:
        """Добавить отсчёты в буфер предыстории (и в активную запись)."""
        block = np.asarray(samples, dtype=np.float64).reshape(-1)
        if block.size == 0:
            return 0
        if block.size > MAX_PUSH_SAMPLES:
            raise PhonogramError(f"Один вызов не более {MAX_PUSH_SAMPLES} отсчётов")
        if not np.all(np.isfinite(block)):
            raise PhonogramError("Отсчёты должны быть конечными числами")
        if self._recording is not None:
            total = self._pre_samples + self._recorded_samples + block.size
            if total > int(MAX_PHONOGRAM_SECONDS * self.sample_rate_hz):
                raise PhonogramError(f"Фонограмма не длиннее {MAX_PHONOGRAM_SECONDS} с")
        block = block.copy()
        self._ring.append(block)
        self._ring_samples += block.size
        limit = self._ring_limit()
        # Trim exactly to the limit: popping whole blocks would drop an
        # oversized head entirely and lose the window.
        while self._ring_samples > limit and self._ring:
            excess = self._ring_samples - limit
            head = self._ring[0]
            if head.size <= excess:
                self._ring.pop(0)
                self._ring_samples -= head.size
            else:
                self._ring[0] = head[excess:]
                self._ring_samples -= excess
        if self._recording is not None:
            self._recording.append(block)
            self._recorded_samples += block.size
        return block.size

    def start(self) -> dict[str, Any]:
        """Начать запись; текущее окно буфера станет предысторией."""
        if self._recording is not None:
            raise PhonogramError("Запись уже идёт")
        self._recording = list(self._ring)
        self._pre_samples = self._ring_samples
        self._recorded_samples = 0
        self._started_at = self._clock()
        self._last = None
        return {
            "recording": True,
            "sample_rate_hz": self.sample_rate_hz,
            "pre_history_seconds": self._pre_samples / self.sample_rate_hz,
            "started_at": to_primitive(self._started_at),
        }

    def stop(self) -> Phonogram:
        """Завершить запись и вернуть фонограмму с предысторией."""
        if self._recording is None:
            raise PhonogramError("Запись не начата")
        samples = np.concatenate(self._recording) if self._recording else np.zeros(0)
        phonogram = Phonogram(
            sample_rate_hz=self.sample_rate_hz,
            pre_history_samples=self._pre_samples,
            started_at=self._started_at or self._clock(),
            ended_at=self._clock(),
            samples=samples,
        )
        self._recording = None
        self._pre_samples = 0
        self._recorded_samples = 0
        self._started_at = None
        self._last = phonogram
        return phonogram

    def status(self) -> dict[str, Any]:
        """Состояние рекордера без сырых отсчётов."""
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "recording": self.recording,
            "pre_history_seconds": self.pre_history_seconds,
            "buffered_seconds": self.buffered_seconds,
            "recorded_seconds": self._recorded_samples / self.sample_rate_hz,
            "started_at": to_primitive(self._started_at) if self._started_at else None,
            "last": self._last.as_dict() if self._last is not None else None,
        }

    def last_wav(self) -> bytes:
        """WAV последней завершённой фонограммы."""
        if self._last is None:
            raise PhonogramError("Фонограмма ещё не записана")
        return self._last.wav_bytes()
