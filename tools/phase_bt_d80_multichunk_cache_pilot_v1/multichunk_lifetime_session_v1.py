"""Route decision closes to per-chunk ChunkLifetimeSessionV1 instances."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

D79 = Path(__file__).resolve().parents[1] / "phase_bt_d79_chunk_acceleration_pilot_v1"
if str(D79) not in sys.path:
    sys.path.insert(0, str(D79))

from chunk_lifetime_density_v1 import ChunkLifetimeSessionV1  # noqa: E402


class MultiChunkLifetimeSessionV1:
    def __init__(self, sessions: list[ChunkLifetimeSessionV1]) -> None:
        self._sessions = sessions
        self._route: dict[str, ChunkLifetimeSessionV1] = {}
        for s in sessions:
            for c in s.closes:
                if c in self._route:
                    raise ValueError(f"duplicate close in chunks: {c}")
                self._route[c] = s
        self.timing: dict[str, Any] = {}

    @classmethod
    def build(cls, chunks: list[list[str]]) -> MultiChunkLifetimeSessionV1:
        sessions: list[ChunkLifetimeSessionV1] = []
        build_times: list[float] = []
        t0 = time.perf_counter()
        for ch in chunks:
            t1 = time.perf_counter()
            sessions.append(ChunkLifetimeSessionV1.build(ch))
            build_times.append(time.perf_counter() - t1)
        inst = cls(sessions)
        inst.timing = {
            "chunk_count": len(chunks),
            "chunk_build_s": [round(x, 4) for x in build_times],
            "total_build_s": round(time.perf_counter() - t0, 4),
        }
        return inst

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        sess = self._route.get(decision_close_utc)
        if sess is None:
            raise KeyError(f"close not in multichunk plan: {decision_close_utc}")
        return sess.collect_at_close(decision_close_utc)

    def covers(self, decision_close_utc: str) -> bool:
        return decision_close_utc in self._route
