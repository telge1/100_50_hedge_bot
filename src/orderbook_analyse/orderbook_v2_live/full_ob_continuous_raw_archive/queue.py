"""Bounded archive queue with fatal overflow semantics."""

from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Any


class ArchiveQueueOverflow(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveQueueItem:
    symbol: str
    record: dict[str, Any]


class FatalBoundedQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("queue capacity must be positive")
        self._queue: queue.Queue[ArchiveQueueItem | None] = queue.Queue(maxsize=capacity)
        self.capacity = capacity
        self.high_watermark = 0

    def put(self, item: ArchiveQueueItem) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full as exc:
            raise ArchiveQueueOverflow("full_ob_archive_queue_full") from exc
        self.high_watermark = max(self.high_watermark, self._queue.qsize())

    def get(self, timeout: float | None = None) -> ArchiveQueueItem | None:
        return self._queue.get(timeout=timeout)

    def task_done(self) -> None:
        self._queue.task_done()

    def join(self) -> None:
        self._queue.join()

    def stop(self) -> None:
        try:
            self._queue.put(None, timeout=2.0)
        except queue.Full as exc:
            raise ArchiveQueueOverflow("full_ob_archive_stop_queue_full") from exc

    def qsize(self) -> int:
        return self._queue.qsize()
