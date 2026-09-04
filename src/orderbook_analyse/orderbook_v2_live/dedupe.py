"""Bounded recent-u dedupe for live orderbook updates.

Bybit ``data.u`` advances by 1 per applied delta (see ``book.apply_delta``).
Duplicates are same-``u`` redeliveries on the WebSocket. A bounded window of
the most recent update IDs is sufficient for that redelivery window; an
unbounded ``set`` grows linearly with runtime (OOM after multi-day runs).

Capacity rationale (protocol, not RAM):
- orderbook.200 can burst toward ~100–200 updates/s on liquid symbols
- WS/TCP redelivery of already-seen ``u`` is expected within seconds, not days
- 8192 entries ≈ 40–80s at high rate — larger than plausible reorder/redeliver
"""

from __future__ import annotations

from collections import deque


# Max recent update IDs retained per symbol clock / generation.
DEFAULT_DEDUPE_CAPACITY = 8192


class BoundedRecentU:
    """O(1) membership with FIFO eviction; maxlen fixed."""

    __slots__ = ("capacity", "_order", "_set", "evictions", "hits")

    def __init__(self, capacity: int = DEFAULT_DEDUPE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("dedupe capacity must be >= 1")
        self.capacity = capacity
        self._order: deque[int] = deque()
        self._set: set[int] = set()
        self.evictions = 0
        self.hits = 0

    def __contains__(self, u: int) -> bool:
        return u in self._set

    def __len__(self) -> int:
        return len(self._set)

    def add(self, u: int) -> bool:
        """Record ``u``. Returns True if newly inserted, False if already present."""
        if u in self._set:
            self.hits += 1
            return False
        if len(self._order) >= self.capacity:
            old = self._order.popleft()
            self._set.discard(old)
            self.evictions += 1
        self._order.append(u)
        self._set.add(u)
        return True

    def clear(self) -> None:
        self._order.clear()
        self._set.clear()
