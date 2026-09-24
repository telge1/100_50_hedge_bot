"""Simple in-process POST rate limit (per principal)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import RATE_LIMIT_MAX_POSTS, RATE_LIMIT_WINDOW_SEC

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def allow_post(principal: str, *, now: float | None = None) -> bool:
    key = str(principal or "anonymous")
    ts = float(now if now is not None else time.time())
    with _lock:
        q = _hits[key]
        while q and ts - q[0] > RATE_LIMIT_WINDOW_SEC:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX_POSTS:
            return False
        q.append(ts)
        return True


def reset_for_tests() -> None:
    with _lock:
        _hits.clear()
