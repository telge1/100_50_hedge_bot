"""Floor guard (short_block_4h_guard) with 4h pools from LMV2 CH SSOT — no LLD recompute."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from research_charts.historical_lmv2.strategy_paths import ensure_strategy_paths
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError
from research_charts.historical_lmv2_ssot_four_h_pools_v1 import (
    load_ssot_legacy_pools_4h_at_close,
    resolve_published_4h_close_at_or_before,
)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class SsotH4FloorGuard:
    """Same ``H4FloorGuard.advance`` semantics; ``_snapshot`` uses CH 4h ACTIVE pools."""

    symbol: str
    pane_from: datetime
    pane_to: datetime
    pool_detail_4h: int = 10
    four_h_bar_closes: list[datetime] = field(default_factory=list)
    client: Any = None
    database: str | None = None
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

        try:
            resolve_published_4h_close_at_or_before(key_moment)
        except HistoricalPoolStateError:
            snap = H4Snapshot(
                decision_time=decision_time,
                pane_4h_len=len(self._four_h_closes_sorted),
                tip=key_moment.isoformat(),
                lowers=[],
                deepest_active=None,
                pool_below_deepest=False,
                gap_pct_below=None,
            )
            self._v2_snap_cache[cache_key] = snap
            return snap

        pool_rows, _meta = load_ssot_legacy_pools_4h_at_close(
            key_moment,
            client=self.client,
            database=self.database,
            allow_nearest_published=True,
        )

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


def guard_contract() -> dict[str, Any]:
    ensure_strategy_paths()
    from short_block_4h_guard import guard_spec

    return {
        "module": "results/pool_scan/context_study_v1/short_block_4h_guard.py",
        "advance_delegation": "H4FloorGuard.advance unchanged",
        "four_h_pool_source": "historical_lmv2_ssot_four_h_pools_v1 (CH publications)",
        "spec": guard_spec(),
        "freeze_note": "Guard logic not in find_short_entry SHA256 manifest",
    }
