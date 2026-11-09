"""O(1) high/low range queries over a closed 5m series (research only)."""

from __future__ import annotations

import math
from datetime import datetime


class CandleRangeIndex:
    def __init__(self, close_unix: list[int], highs: list[float], lows: list[float]) -> None:
        if not (len(close_unix) == len(highs) == len(lows)):
            raise ValueError("candle range arrays differ in length")
        self.close_unix = close_unix
        self.n = len(close_unix)
        self._max: list[list[float]] = []
        self._min: list[list[float]] = []
        if self.n == 0:
            return
        self._max.append(list(highs))
        self._min.append(list(lows))
        k = 1
        while (1 << k) <= self.n:
            span = 1 << (k - 1)
            width = 1 << k
            prev_h = self._max[-1]
            prev_l = self._min[-1]
            nh: list[float] = []
            nl: list[float] = []
            limit = self.n - width + 1
            for i in range(limit):
                nh.append(prev_h[i] if prev_h[i] >= prev_h[i + span] else prev_h[i + span])
                nl.append(prev_l[i] if prev_l[i] <= prev_l[i + span] else prev_l[i + span])
            self._max.append(nh)
            self._min.append(nl)
            k += 1
        self._log = [0] * (self.n + 1)
        for i in range(2, self.n + 1):
            self._log[i] = self._log[i // 2] + 1

    def query(self, left: int, right: int) -> tuple[float, float] | None:
        """Inclusive index range. None when empty."""
        if left > right or left < 0 or right >= self.n:
            if left > right:
                return None
            raise IndexError(f"range {left}:{right} outside 0..{self.n}")
        length = right - left + 1
        k = self._log[length]
        span = 1 << k
        hi = self._max[k][left]
        hi2 = self._max[k][right - span + 1]
        lo = self._min[k][left]
        lo2 = self._min[k][right - span + 1]
        return (hi if hi >= hi2 else hi2), (lo if lo <= lo2 else lo2)

    def eligible_index_span(self, known_unix: int, decision_unix: int) -> tuple[int, int] | None:
        """Candles with close_unix > known_unix and close_unix <= decision_unix."""
        import bisect

        left = bisect.bisect_right(self.close_unix, known_unix)
        right = bisect.bisect_right(self.close_unix, decision_unix) - 1
        if left > right:
            return None
        return left, right


def unix_utc(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return int(dt.timestamp())


def content_hash_ohlcv(close_unix: list[int], ohlcv: list[tuple[float, float, float, float, float]]) -> str:
    import hashlib

    h = hashlib.sha256()
    for u, row in zip(close_unix, ohlcv):
        h.update(f"{u}|{row[0]}|{row[1]}|{row[2]}|{row[3]}|{row[4]}\n".encode())
    return h.hexdigest()


def sparse_depth(n: int) -> int:
    if n <= 1:
        return 1
    return int(math.log2(n)) + 1
