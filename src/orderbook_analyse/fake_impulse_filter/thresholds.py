from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FrozenGateThresholds:
    """Exact frozen ONDO positive-case effective thresholds (do not retune)."""

    tbr_thr: float = 0.8623619360235284
    cvd_thr: float = 30563.831199999997
    imb_thr: float = 0.20478833633333332
    flow_clean_tbr_min: float = 0.48
    rv_rel_thr: float = 1.5
    vol_busy_thr: float = 1.8
    ret5_confirmed: float = 0.0025

    @property
    def sell_tbr_max(self) -> float:
        return 1.0 - self.tbr_thr

    @property
    def flow_clean_tbr_max_short(self) -> float:
        return 1.0 - self.flow_clean_tbr_min


FROZEN_DEFAULT = FrozenGateThresholds()


@dataclass(frozen=True)
class ResearchExploreParams:
    """Investigational fake-filter params — not live-activated.

    Values are candidates for validation; do not treat as production.
    """

    persistence_horizons_s: tuple[int, ...] = (30, 60, 90, 180)
    giveback_horizons_s: tuple[int, ...] = (30, 60, 90, 180)
    outcome_horizons_s: tuple[int, ...] = (30, 60, 90, 180, 300)
    # exploratory giveback marks (investigate band 50–70%)
    giveback_mark_low: float = 0.50
    giveback_mark_high: float = 0.70
    # research confirmation requires persistence at this horizon
    confirm_persist_s: int = 60
    min_confirm_samples_same_dir: int = 3
    # whipsaw cooldowns to evaluate
    reversal_cooldowns_s: tuple[int, ...] = (60, 120, 180, 300)
    primary_cooldown_s: int = 180
    # min impulse displacement (fraction) to evaluate giveback
    min_impulse_move: float = 0.0010  # 10 bps
    # research: require frozen confirming before CONFIRMED
    require_frozen_confirming_for_confirmed: bool = True
    # if OB required sources missing → INCONCLUSIVE rather than CONFIRMED
    require_ob_for_confirmed: bool = True
    require_trades_for_confirmed: bool = True


DEFAULT_RESEARCH = ResearchExploreParams()
