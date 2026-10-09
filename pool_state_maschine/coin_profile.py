"""Learned thresholds for one coin. The pattern stays fixed, the cutoffs do not."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoinProfile:
    symbol: str
    lookback_days: int = 180
    cluster_gap_pct: float = 3.0
    standalone_gap_pct: float = 3.0
    wide_upper_gap_pct: float = 4.5
    min_stair_steps: int = 2
    stair_drop_pct: float = 6.0
    stair_separation_pct: float = 1.2
    relevant_pool_width_pct: float = 2.5
    reclaim_from_low_pct: float = 10.0
    floor_hold_bars: int = 30
    floor_clear_bars: int = 18
    floor_break_pct: float = 1.5

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "symbol": self.symbol,
            "lookback_days": self.lookback_days,
            "cluster_gap_pct": self.cluster_gap_pct,
            "standalone_gap_pct": self.standalone_gap_pct,
            "wide_upper_gap_pct": self.wide_upper_gap_pct,
            "min_stair_steps": self.min_stair_steps,
            "stair_drop_pct": self.stair_drop_pct,
            "stair_separation_pct": self.stair_separation_pct,
            "relevant_pool_width_pct": self.relevant_pool_width_pct,
            "reclaim_from_low_pct": self.reclaim_from_low_pct,
            "floor_hold_bars": self.floor_hold_bars,
            "floor_clear_bars": self.floor_clear_bars,
            "floor_break_pct": self.floor_break_pct,
        }
