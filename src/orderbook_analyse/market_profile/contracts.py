"""Frozen data contracts for the market profile.

Every derived number a chart shows is also carried here so the JSON artifact
is a complete description of the rendered picture. Thresholds live in
:class:`ShapeThresholds` rather than inline, because they are provisional and
will need calibration against realised outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import (
    DEFAULT_HVN_FACTOR,
    DEFAULT_LVN_FACTOR,
    DEFAULT_NODE_MIN_SEPARATION_BINS,
    DEFAULT_SINGLE_PRINT_FRAC,
)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProfileBin:
    """One price bucket of the volume profile.

    `volume` is base quantity. `buy_volume` is taker-buy (aggressor hit the
    ask), `sell_volume` is taker-sell.
    """

    bin_index: int
    price_low: float
    price_high: float
    price_mid: float
    volume: float
    buy_volume: float
    sell_volume: float
    trades: int
    notional: float

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["delta"] = self.delta
        return d


@dataclass(frozen=True)
class ValueArea:
    """POC plus the price band holding `volume_share` of the total volume."""

    poc: float
    poc_volume: float
    poc_bin_index: int
    vah: float
    val: float
    requested_share: float
    volume_share: float
    bin_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NodeSet:
    """High/low volume nodes and the low-volume ranges price ran through."""

    hvn: tuple[float, ...]
    lvn: tuple[float, ...]
    single_print_ranges: tuple[tuple[float, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hvn": list(self.hvn),
            "lvn": list(self.lvn),
            "single_print_ranges": [list(r) for r in self.single_print_ranges],
        }


@dataclass(frozen=True)
class ShapeThresholds:
    """Provisional cut-offs for the balance/trend decision.

    These are unvalidated defaults chosen to be readable, not fitted to any
    outcome. `ShapeVerdict` always reports the raw metrics next to the verdict
    so thresholds can be re-tuned from the JSON artifact without recomputing
    the profiles.
    """

    # Centred on the observed distribution of 42 day-anchored BTCUSDT windows
    # (2026-07-20..2026-08-31): va_range_share median 0.52 (p25 0.46, p75 0.60),
    # |directional_share| median 0.40 (p25 0.19, p75 0.58). Direction leads the
    # rule because it is the stronger and more interpretable of the two.
    trend_directional_share: float = 0.50
    trend_va_range_share: float = 0.50
    balance_directional_share: float = 0.25
    balance_va_range_share: float = 0.55
    poc_central_low: float = 0.35
    poc_central_high: float = 0.65

    # Double distribution = two areas of acceptance split by a real area of
    # rejection. All three conditions must hold, otherwise a trend day that
    # paused at both ends of its range registers as double.
    double_distribution_min_separation: float = 0.40
    double_secondary_peak_frac: float = 0.50
    double_distribution_valley_frac: float = 0.35
    double_valley_min_bins: int = 3
    hvn_factor: float = DEFAULT_HVN_FACTOR
    lvn_factor: float = DEFAULT_LVN_FACTOR
    node_min_separation_bins: int = DEFAULT_NODE_MIN_SEPARATION_BINS
    single_print_frac: float = DEFAULT_SINGLE_PRINT_FRAC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShapeVerdict:
    """Balance/trend classification with the metrics it was derived from."""

    kind: str
    letter: str
    poc_position: float
    va_range_share: float
    poc_concentration: float
    directional_share: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


@dataclass(frozen=True)
class ProfileWindow:
    """The anchor of a profile: which slice of time it is computed over."""

    window_id: str
    anchor_mode: str
    label: str
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "anchor_mode": self.anchor_mode,
            "label": self.label,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }


@dataclass(frozen=True)
class MarketProfile:
    """A complete anchored profile: bins, levels, shape and later interaction."""

    symbol: str
    window: ProfileWindow
    price_step: float
    price_low: float
    price_high: float
    open_price: float
    close_price: float
    total_volume: float
    buy_volume: float
    sell_volume: float
    trades: int
    notional: float
    bins: tuple[ProfileBin, ...]
    value_area: ValueArea
    nodes: NodeSet
    shape: ShapeVerdict
    naked_poc: bool | None = None
    poc_revisit_ts: datetime | None = None
    naked_checked_until: datetime | None = None

    @property
    def price_range(self) -> float:
        return self.price_high - self.price_low

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    def to_dict(self, *, include_bins: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "window": self.window.to_dict(),
            "price_step": self.price_step,
            "price_low": self.price_low,
            "price_high": self.price_high,
            "price_range": self.price_range,
            "open_price": self.open_price,
            "close_price": self.close_price,
            "total_volume": self.total_volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "delta": self.delta,
            "trades": self.trades,
            "notional": self.notional,
            "value_area": self.value_area.to_dict(),
            "nodes": self.nodes.to_dict(),
            "shape": self.shape.to_dict(),
            "naked_poc": self.naked_poc,
            "poc_revisit_ts": _iso(self.poc_revisit_ts),
            "naked_checked_until": _iso(self.naked_checked_until),
        }
        if include_bins:
            out["bins"] = [b.to_dict() for b in self.bins]
        return out


@dataclass(frozen=True)
class RunSpec:
    """Everything needed to reproduce a run."""

    symbol: str
    start: datetime
    end: datetime
    anchor_mode: str
    sessions: tuple[str, ...]
    timeframe: str
    value_area_pct: float
    target_bins: int
    use_final: bool
    theme: str
    thresholds: ShapeThresholds = field(default_factory=ShapeThresholds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "anchor_mode": self.anchor_mode,
            "sessions": list(self.sessions),
            "timeframe": self.timeframe,
            "value_area_pct": self.value_area_pct,
            "target_bins": self.target_bins,
            "use_final": self.use_final,
            "theme": self.theme,
            "thresholds": self.thresholds.to_dict(),
        }
