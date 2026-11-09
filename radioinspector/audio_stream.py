"""Network delivery of demodulated audio: a bounded fan-out of PCM blocks.

Backlog item 10: «Передача демодулированного аудио по сети» — the
original's RemoteMD mode. A hub publishes each accepted audio block to
every subscribed network client as PCM 16-bit little-endian mono, the
same encoding the phonogram files use. The stream is pull-based at the
transport level (the client opens one long-lived HTTP response) and
push-based at the data level (blocks appear as the recorder accepts
frames).

The hub never stores audio beyond the bounded queue of each subscriber:
when a client falls behind, its oldest blocks are dropped and the loss is
counted. The source frames are never modified; this is delivery, not
recording.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

#: Максимум одновременных подписчиков на один поток.
MAX_SUBSCRIBERS = 4

#: Размер очереди подписчика в блоках; при переполнении старые блоки теряются.
MAX_QUEUED_BLOCKS = 64

#: Максимум отсчётов в одном блоке.
MAX_BLOCK_SAMPLES = 48_000


class AudioStreamError(ValueError):
    """The hub cannot serve this subscription."""


def pcm16_bytes(samples: Any) -> bytes:
    """Кодирование отсчётов в PCM 16 бит LE, как в WAV-файлах фонограмм."""
    data = np.clip(np.asarray(samples, dtype=np.float64).reshape(-1), -1.0, 1.0)
    return (data * 32767.0).astype("<i2").tobytes()


@dataclass
class StreamSubscription:
    """Очередь одного сетевого клиента со счётчиком потерь."""

    blocks: "queue.Queue[bytes]" = field(default_factory=lambda: queue.Queue(maxsize=MAX_QUEUED_BLOCKS))
    dropped_blocks: int = 0
    delivered_bytes: int = 0

    def offer(self, block: bytes) -> None:
        try:
            self.blocks.put_nowait(block)
        except queue.Full:
            # Drop the oldest block and count the loss: a slow client must
            # not stall the recorder or the other subscribers.
            try:
                self.blocks.get_nowait()
                self.dropped_blocks += 1
            except queue.Empty:
                pass
            try:
                self.blocks.put_nowait(block)
            except queue.Full:
                self.dropped_blocks += 1

    def stats(self) -> dict[str, Any]:
        return {
            "dropped_blocks": self.dropped_blocks,
            "delivered_bytes": self.delivered_bytes,
            "queued_blocks": self.blocks.qsize(),
        }


class AudioStreamHub:
    """Fan-out of accepted audio blocks to subscribed network clients."""

    def __init__(self, *, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self._max_subscribers = max_subscribers
        self._lock = threading.Lock()
        self._subscribers: dict[tuple[str, str], list[StreamSubscription]] = {}

    def subscribe(self, session_id: str, stream_id: str) -> StreamSubscription:
        key = (session_id, stream_id)
        with self._lock:
            current = self._subscribers.setdefault(key, [])
            if len(current) >= self._max_subscribers:
                raise AudioStreamError(f"Не более {self._max_subscribers} подписчиков на поток")
            subscription = StreamSubscription()
            current.append(subscription)
            return subscription

    def unsubscribe(self, session_id: str, stream_id: str, subscription: StreamSubscription) -> None:
        key = (session_id, stream_id)
        with self._lock:
            current = self._subscribers.get(key)
            if not current:
                return
            if subscription in current:
                current.remove(subscription)
            if not current:
                self._subscribers.pop(key, None)

    def publish(self, session_id: str, stream_id: str, samples: Any) -> int:
        """Разослать блок подписчикам; без подписчиков ничего не копится."""
        if isinstance(samples, (bytes, bytearray)):
            block = bytes(samples)
        else:
            block = pcm16_bytes(samples)
        if len(block) > MAX_BLOCK_SAMPLES * 2:
            raise AudioStreamError(f"Блок не больше {MAX_BLOCK_SAMPLES} отсчётов")
        key = (session_id, stream_id)
        with self._lock:
            subscribers = list(self._subscribers.get(key, ()))
        for subscription in subscribers:
            subscription.offer(block)
            subscription.delivered_bytes += 0  # delivered grows as the client reads
        return len(subscribers)

    def status(self, session_id: str, stream_id: str) -> dict[str, Any]:
        key = (session_id, stream_id)
        with self._lock:
            subscribers = list(self._subscribers.get(key, ()))
        return {
            "session_id": session_id, "stream_id": stream_id,
            "subscribers": len(subscribers),
            "max_subscribers": self._max_subscribers,
            "queued_blocks": sum(item.blocks.qsize() for item in subscribers),
            "dropped_blocks": sum(item.dropped_blocks for item in subscribers),
        }
