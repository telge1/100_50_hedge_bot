"""H4FloorGuard with V2 PD10 lowers — original ``advance`` on inner guard, V2 ``_snapshot`` only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .provider import HistoricalLmV2PoolStore
from .strategy_paths import ensure_strategy_paths


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class V2H4FloorGuard:
    """
    Delegates ``advance`` to ``H4FloorGuard.advance(inner, ...)`` with ``inner._snapshot``
    patched to load V2 4h lowers. Episode state remains on ``inner``.
    """

    symbol: str
    pane_from: datetime
    pane_to: datetime
    store: HistoricalLmV2PoolStore
    pool_detail_4h: int = 10
    four_h_bar_closes: list[datetime] = field(default_factory=list)
    _v2_snap_cache: dict[tuple[str, datetime], Any] = field(default_factory=dict, repr=False)
    _four_h_closes_sorted: list[datetime] = field(default_factory=list, init=False, repr=False)
    _inner: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        ensure_strategy_paths()
        from short_block_4h_guard import H4FloorGuard

        self._four_h_closes_sorted = sorted(_utc(c) for c in self.four_h_bar_closes)
        inner = H4FloorGuard(self.symbol, self.pane_from, self.pane_to)
        inner._snapshot = self._snapshot  # type: ignore[method-assign]
        self._inner = inner

    @property
    def inner(self):
        return self._inner

    def _resolve_key_moment(self, decision_time: datetime) -> datetime:
        decision_time = _utc(decision_time)
        key_moment = decision_time
        for ct in self._four_h_closes_sorted:
            if ct <= decision_time:
                key_moment = ct
        return key_moment

    def _snapshot(self, decision_time: datetime):
        from dashboard.research_charts.lld_research_kernel import pool_known_at
        from short_block_4h_guard import FLOOR_POOL_GAP_PCT, H4Snapshot, _utc as guard_utc

        decision_time = guard_utc(decision_time)
        key_moment = self._resolve_key_moment(decision_time)
        cache_key = (self.symbol, key_moment)
        if cache_key in self._v2_snap_cache:
            return self._v2_snap_cache[cache_key]

        pool_rows = self.store.load_legacy_pools("4h", self.pool_detail_4h, key_moment)

        active_lowers = []
        for row in pool_rows:
            if row["side"] != "lower":
                continue
            if not pool_known_at(row, decision_time):
                continue
            active_lowers.append(
                {
                    "pool_id": row["pool_id"],
                    "bottom": float(row["bottom"]),
                    "top": float(row["top"]),
                }
            )
        active_lowers.sort(key=lambda r: r["bottom"])
        deepest = active_lowers[0] if active_lowers else None
        pool_below = False
        if deepest:
            for r in active_lowers[1:]:
                if r["top"] < deepest["bottom"]:
                    gap = (deepest["bottom"] - r["top"]) / deepest["bottom"] * 100.0
                    if gap >= FLOOR_POOL_GAP_PCT:
                        pool_below = True
                        break

        snap = H4Snapshot(
            decision_time=decision_time,
            pane_4h_len=len(self._four_h_closes_sorted),
            tip=key_moment.isoformat(),
            lowers=[],
            deepest_active=deepest,
            pool_below_deepest=pool_below,
            gap_pct_below=None,
        )
        self._v2_snap_cache[cache_key] = snap
        return snap

    def advance(self, bar: dict, moment: datetime) -> tuple[bool, str | None]:
        ensure_strategy_paths()
        from short_block_4h_guard import H4FloorGuard

        return H4FloorGuard.advance(self._inner, bar, moment)
