"""Data contracts for the validation run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TouchEvent:
    """One test: price met a reference level, and something happened next.

    `favorable_sign` fixes what "worked" means for this event (+1 = up), so
    MFE/MAE are directional rather than absolute.
    """

    symbol: str
    hypothesis: str
    variant: str
    ref_window_id: str
    ref_label: str
    ref_shape_kind: str
    ref_shape_letter: str
    ref_range: float
    ref_direction: float
    ref_va_range_share: float
    ref_directional_share: float
    ref_poc_position: float
    test_window_id: str
    level_kind: str
    level_price: float
    poc_price: float
    approach: str
    favorable_sign: int
    touch_ts: datetime
    touch_price: float
    target_price: float
    stop_price: float
    outcome: str
    resolution_ts: datetime | None
    bars_to_resolution: int | None
    mfe_frac: float
    mae_frac: float
    reward_risk: float

    @property
    def breakeven_rate(self) -> float:
        """Hit rate this trade would need just to break even, before costs."""
        return 1.0 / (1.0 + self.reward_risk) if self.reward_risk > 0 else 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["touch_ts"] = _iso(self.touch_ts)
        d["resolution_ts"] = _iso(self.resolution_ts)
        d["breakeven_rate"] = self.breakeven_rate
        return d


@dataclass(frozen=True)
class RevisitEvent:
    """H3: was the reference POC touched during the next window?

    Measured at several horizons on purpose. Over a whole following day the
    POC is touched almost every time regardless of the reference class, so the
    full-window flag has no discriminating power; the short horizons are where
    a magnet effect would have to show up.
    """

    symbol: str
    ref_window_id: str
    ref_label: str
    ref_shape_kind: str
    ref_shape_letter: str
    ref_range: float
    test_window_id: str
    poc_price: float
    poc_distance_frac: float
    revisited: bool
    revisit_ts: datetime | None
    minutes_to_revisit: int | None
    revisited_60m: bool = False
    revisited_240m: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["revisit_ts"] = _iso(self.revisit_ts)
        return d


@dataclass(frozen=True)
class RateEstimate:
    """A proportion with three uncertainty views.

    `wilson_*` treats every event as independent, which it is not — crypto
    symbols move together. The clustered intervals resample whole symbols and
    whole dates and are the ones to believe.
    """

    label: str
    successes: int
    trials: int
    rate: float
    wilson_low: float
    wilson_high: float
    cluster_symbol_low: float
    cluster_symbol_high: float
    cluster_date_low: float
    cluster_date_high: float
    ambiguous: int
    timeouts: int
    worst_case_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationConfig:
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    anchor_mode: str
    value_area_pct: float
    target_bins: int
    edge_margin_frac: float
    poc_unit_frac: float
    edge_margin_grid: tuple[float, ...]
    poc_unit_grid: tuple[float, ...]
    max_horizon_min: int
    use_final: bool
    bootstrap_iters: int
    seed: int
    cost_bps: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["symbols"] = list(self.symbols)
        d["edge_margin_grid"] = list(self.edge_margin_grid)
        d["poc_unit_grid"] = list(self.poc_unit_grid)
        d["start"] = _iso(self.start)
        d["end"] = _iso(self.end)
        return d


@dataclass(frozen=True)
class SymbolRun:
    symbol: str
    windows: int
    profiles: int
    skipped_thin: int
    touch_events: tuple[TouchEvent, ...] = field(default_factory=tuple)
    revisit_events: tuple[RevisitEvent, ...] = field(default_factory=tuple)
    error: str | None = None
