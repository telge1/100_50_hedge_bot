"""4h floor short-block guard (causal prefix pane). Used by find_short_entry causal scan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class H4Snapshot:
    decision_time: datetime
    pane_4h_len: int
    tip: str
    lowers: list[dict]
    deepest_active: dict | None
    pool_below_deepest: bool
    gap_pct_below: float | None

FLOOR_POOL_GAP_PCT = 0.5
NEAR_TOP_PCT = 1.0
EPISODE_HOURS = 48
EPISODE_MAX_DIST_PCT = 5.0


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class H4FloorGuard:
    """Stateful guard: advance once per 15m bar in chronological order."""

    symbol: str
    pane_from: datetime
    pane_to: datetime
    _snap_cache: dict[tuple[str, datetime], Any] = field(default_factory=dict)
    _candles_4h: list | None = None
    session_floor_top: float | None = None
    session_floor_bottom: float | None = None
    block_until: datetime | None = None
    last_reason: str | None = None

    def _load_4h(self) -> list:
        if self._candles_4h is not None:
            return self._candles_4h
        from dashboard.research_charts.lld_research_kernel import load_pane_candles

        _packed, candles = load_pane_candles(
            self.symbol,
            "4h",
            from_unix=int(self.pane_from.timestamp()),
            to_unix=int(self.pane_to.timestamp()),
        )
        self._candles_4h = candles
        return candles

    def _snapshot(self, decision_time: datetime) -> Any:
        from dashboard.research_charts.lld_research_kernel import (
            compute_lld_selected,
            pool_known_at,
            pool_row,
            resolve_tip,
            ui_lld_config,
        )
        from pool_scan.clock import bar_close

        decision_time = _utc(decision_time)
        key_moment = decision_time
        for c in self._load_4h():
            ct = bar_close(_utc(c.timestamp), "4h")
            if ct <= decision_time:
                key_moment = ct
        cache_key = (self.symbol, key_moment)
        if cache_key in self._snap_cache:
            return self._snap_cache[cache_key]

        prefix = [
            c
            for c in self._load_4h()
            if bar_close(_utc(c.timestamp), "4h") <= decision_time
        ]
        if not prefix:
            snap = H4Snapshot(
                decision_time=decision_time,
                pane_4h_len=0,
                tip="",
                lowers=[],
                deepest_active=None,
                pool_below_deepest=False,
                gap_pct_below=None,
            )
            self._snap_cache[cache_key] = snap
            return snap

        cfg = ui_lld_config("4h")
        selected, _, _, _ = compute_lld_selected(
            prefix, "4h", cfg, tip=resolve_tip(prefix, None)
        )
        active_lowers = []
        for pool in selected:
            row = pool_row(pool)
            if not row or row["side"] != "lower":
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
            pane_4h_len=len(prefix),
            tip=resolve_tip(prefix, None).isoformat(),
            lowers=[],
            deepest_active=deepest,
            pool_below_deepest=pool_below,
            gap_pct_below=None,
        )
        self._snap_cache[cache_key] = snap
        return snap

    def advance(self, bar: dict, moment: datetime) -> tuple[bool, str | None]:
        """Update episode state; return whether shorts should be blocked on this bar."""
        moment = _utc(moment)
        if self.block_until is not None and moment >= self.block_until:
            self.block_until = None
            self.session_floor_top = None
            self.session_floor_bottom = None

        snap = self._snapshot(moment)
        deepest = snap.deepest_active
        if deepest is None or snap.pool_below_deepest:
            self.last_reason = None
            return False, None

        bottom = deepest["bottom"]
        top = deepest["top"]
        close = float(bar["close"])
        low = float(bar["low"])
        high = float(bar["high"])

        # Episode window extends on each touch; no reset when a deeper 4h lower appears (see analyze script).
        touched = low <= top and high >= bottom
        if touched:
            self.session_floor_top = (
                top if self.session_floor_top is None else min(self.session_floor_top, top)
            )
            self.session_floor_bottom = (
                bottom if self.session_floor_bottom is None else min(self.session_floor_bottom, bottom)
            )
            self.block_until = moment + timedelta(hours=EPISODE_HOURS)

        if touched:
            self.last_reason = "A1_touch"
            return True, self.last_reason
        if bottom <= close <= top:
            self.last_reason = "A3_in_zone"
            return True, self.last_reason
        if close > top and (close - top) / close * 100.0 <= NEAR_TOP_PCT:
            self.last_reason = "A2_near_top"
            return True, self.last_reason

        if (
            self.block_until is not None
            and moment < self.block_until
            and self.session_floor_top is not None
        ):
            dist = (close - self.session_floor_top) / close * 100.0
            if dist <= EPISODE_MAX_DIST_PCT:
                self.last_reason = "B_episode"
                return True, self.last_reason

        self.last_reason = None
        return False, None


def guard_spec() -> dict[str, float | int]:
    return {
        "floor_pool_gap_pct": FLOOR_POOL_GAP_PCT,
        "near_top_pct": NEAR_TOP_PCT,
        "episode_hours": EPISODE_HOURS,
        "episode_max_dist_pct": EPISODE_MAX_DIST_PCT,
        "episode_reset_on_deeper_pool": False,
    }
