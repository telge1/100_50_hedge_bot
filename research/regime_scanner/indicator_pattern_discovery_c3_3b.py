"""Phase C3.3B indicator-pattern discovery extension.

Research-only audit layer for DI / EMA sequencing, ADX as-of vs path
relationships, and EMA band dynamics. This module reuses the C3.3A discovery
frame and detectors, but keeps its own configuration, candidate selection, and
exports. It does not modify production regime classification or configs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_pattern_discovery import (
    build_discovery_frame,
    build_price_arrays,
    compute_horizon_outcome,
    detect_di_crosses,
    detect_ema_crosses,
    detect_ema_expansions,
    detect_range_breakouts,
    detect_trend_follow,
    events_content_hash,
    split_discovery_validation,
    _direction_side,
    _finite,
    _iso,
    _ts,
)
from research.regime_scanner.indicator_pattern_discovery import (
    PatternDiscoveryConfig as C33AConfig,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_detector_c3_3b_pine import (
    ASOF_PINE_NAME,
    OUTCOME_PINE_NAME,
    export_trend_detector_artifacts,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_3b_apt_pattern_discovery")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)


@dataclass(frozen=True)
class PatternDiscoveryC33BConfig:
    pre_bars: int = 12
    post_bars: int = 48
    horizons: tuple[int, ...] = (3, 6, 12, 24)
    optional_horizons: tuple[int, ...] = (48,)
    horizons_for_class: tuple[int, ...] = (3, 6, 12, 24)
    delayed_horizon: int = 12
    discovery_end: str | None = None
    min_pattern_events_discovery: int = 20
    min_pattern_events_validation: int = 10
    bootstrap_samples: int = 200
    bootstrap_seed: int = 42

    di_follow_window_max: int = 12
    di_follow_buckets: tuple[str, ...] = ("1", "2", "3", "4_6", "7_12")
    di_spread_expand_min: float = 0.20

    adx_bucket_lt_15: float = 15.0
    adx_bucket_15_20: float = 20.0
    adx_bucket_20_25: float = 25.0
    adx_level_confirmation_min: float = 20.0
    adx_level_strong_min: float = 25.0
    adx_rising_min_delta_1: float = 0.25
    adx_accel_min: float = 0.15

    ema_flat_slope_max_atr: float = 0.10
    ema_joint_slope_min_atr: float = 0.15
    band_expand_min_change_atr: float = 0.10
    compression_max: float = 0.45
    near_ema59_atr: float = 0.50
    near_ema200_atr: float = 1.00

    clean_mfe_min: float = 0.80
    clean_mae_max: float = 0.60
    weak_mfe_max: float = 0.25
    adverse_mae_min: float = 1.00
    early_adverse_mae: float = 0.75
    recovery_mfe_min: float = 0.80

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mean(values: Sequence[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _sign(v: float | None) -> int:
    if v is None or not math.isfinite(v):
        return 0
    if abs(v) < 1e-12:
        return 0
    return 1 if v > 0 else -1


def _adx_bucket(adx: float | None, cfg: PatternDiscoveryC33BConfig) -> str:
    if adx is None or not math.isfinite(adx):
        return "adx_unknown"
    if adx < cfg.adx_bucket_lt_15:
        return "adx_lt_15"
    if adx < cfg.adx_bucket_15_20:
        return "adx_15_20"
    if adx < cfg.adx_bucket_20_25:
        return "adx_20_25"
    return "adx_ge_25"


def _lag_bucket(lag: int | None) -> str:
    if lag is None:
        return "none"
    if lag == 0:
        return "0"
    if lag == 1:
        return "1"
    if lag == 2:
        return "2"
    if lag == 3:
        return "3"
    if 4 <= lag <= 6:
        return "4_6"
    if 7 <= lag <= 12:
        return "7_12"
    return "none"


def _band_state_from_row(row: Mapping[str, Any], cfg: PatternDiscoveryC33BConfig) -> str:
    band_change = _finite(row.get("band_change_3_atr"), 0.0)
    comp_score = _finite(row.get("ema_fast_compression_score"), 0.0)
    if band_change >= cfg.band_expand_min_change_atr:
        return "band_expand"
    if band_change <= -cfg.band_expand_min_change_atr or comp_score >= cfg.compression_max:
        return "band_compress"
    return "band_flat"


def _adx_motion(row: Mapping[str, Any], cfg: PatternDiscoveryC33BConfig) -> str:
    delta = _finite(row.get("adx_delta_1"), 0.0)
    accel = _finite(row.get("adx_accel"), 0.0)
    if delta >= cfg.adx_rising_min_delta_1 or accel >= cfg.adx_accel_min:
        return "rising"
    if delta <= -cfg.adx_rising_min_delta_1 or accel <= -cfg.adx_accel_min:
        return "falling"
    return "flat"


def _coerce_ts(value: object) -> pd.Timestamp:
    return _ts(value)


def enrich_discovery_frame(frame: pd.DataFrame, cfg: PatternDiscoveryC33BConfig) -> pd.DataFrame:
    out = frame.copy().reset_index(drop=True)
    adx = pd.to_numeric(out.get("adx_14"), errors="coerce").astype("float64")
    spread_atr = pd.to_numeric(out.get("ema_9_20_spread_atr"), errors="coerce").astype("float64")
    abs_spread = spread_atr.abs()
    s9 = pd.to_numeric(out.get("ema_9_slope_3_atr"), errors="coerce").astype("float64")
    s20 = pd.to_numeric(out.get("ema_20_slope_3_atr"), errors="coerce").astype("float64")
    di_spread = pd.to_numeric(out.get("di_spread"), errors="coerce").astype("float64")

    for w in (1, 2, 3, 5):
        out[f"adx_delta_{w}"] = adx - adx.shift(w)
        out[f"band_change_{w}_atr"] = abs_spread - abs_spread.shift(w)
        out[f"di_spread_change_{w}"] = di_spread - di_spread.shift(w)
        out[f"di_spread_abs_change_{w}"] = di_spread.abs() - di_spread.abs().shift(w)

    # Linear slope approximations (causal).
    out["adx_slope_lin_3"] = (adx - adx.shift(3)) / 3.0
    out["adx_slope_lin_5"] = (adx - adx.shift(5)) / 5.0
    out["adx_accel"] = out["adx_delta_1"] - out["adx_delta_1"].shift(1)

    rising = (out["adx_delta_1"] >= cfg.adx_rising_min_delta_1).fillna(False)
    streak = np.zeros(len(out), dtype=int)
    for i in range(len(out)):
        if bool(rising.iloc[i]):
            streak[i] = (streak[i - 1] + 1) if i > 0 else 1
        else:
            streak[i] = 0
    out["adx_rising_streak"] = streak

    out["ema_joint_slope_3_atr"] = (s9 + s20) / 2.0
    out["ema_joint_rising"] = out["ema_joint_slope_3_atr"] >= cfg.ema_joint_slope_min_atr
    out["ema_joint_falling"] = out["ema_joint_slope_3_atr"] <= -cfg.ema_joint_slope_min_atr
    out["ema_joint_flat"] = out["ema_joint_slope_3_atr"].abs() <= cfg.ema_flat_slope_max_atr
    out["ema_fast_rising_slow_flat"] = (s9.abs() >= cfg.ema_joint_slope_min_atr) & (
        s20.abs() <= cfg.ema_flat_slope_max_atr
    )
    out["ema_band_expanding"] = out["band_change_3_atr"] >= cfg.band_expand_min_change_atr
    out["ema_band_compressing"] = out["band_change_3_atr"] <= -cfg.band_expand_min_change_atr

    expanding = out["ema_band_expanding"].fillna(False)
    expand_streak = np.zeros(len(out), dtype=int)
    for i in range(len(out)):
        if bool(expanding.iloc[i]):
            expand_streak[i] = (expand_streak[i - 1] + 1) if i > 0 else 1
        else:
            expand_streak[i] = 0
    out["ema_band_expansion_duration"] = expand_streak
    out["ema_band_expansion_ge_2"] = expand_streak >= 2
    out["ema_band_expansion_ge_3"] = expand_streak >= 3
    out["ema_band_expansion_ge_5"] = expand_streak >= 5
    return out


def _core_sequence_label(
    *,
    event_type: str,
    prior_di_lag: int | None = None,
    next_ema_lag: int | None = None,
) -> str:
    et = str(event_type or "")
    if et == "di_cross":
        if next_ema_lag == 0:
            return "di_ema_coincident"
        if next_ema_lag is None:
            return "di_without_ema_follow"
        if next_ema_lag > 0:
            return "di_leads_ema"
        return "di_without_ema_follow"
    if et == "ema_cross":
        if prior_di_lag is None:
            return "ema_without_prior_di"
        if prior_di_lag == 0:
            return "di_ema_coincident"
        if prior_di_lag > 0:
            return "di_with_ema_follow"
        return "ema_without_prior_di"
    return _event_role(et)


def _event_role(event_type: str) -> str:
    if event_type == "di_cross":
        return "di_cross"
    if event_type == "ema_cross":
        return "ema_cross"
    if event_type == "ema_expansion_start":
        return "ema_expansion"
    if event_type.startswith("range_breakout"):
        return "breakout"
    if event_type.startswith("trend_follow"):
        return "trend_follow"
    return "other"


def _policy_flags(row: Mapping[str, Any], cfg: PatternDiscoveryC33BConfig) -> dict[str, bool]:
    adx = _finite(row.get("adx_14"), np.nan)
    delta1 = _finite(row.get("adx_delta_1"), 0.0)
    accel = _finite(row.get("adx_accel"), 0.0)
    joint = _finite(row.get("ema_joint_slope_3_atr"), 0.0)
    di_abs_change = _finite(row.get("di_spread_abs_change_1"), 0.0)
    et = str(row.get("event_type") or "")
    # As-of only: prior DI at EMA bar is causal; future EMA follow after DI is not.
    has_di = et == "di_cross" or bool(row.get("has_prior_di_cross_asof"))
    has_ema = et == "ema_cross" or bool(row.get("has_ema_cross_asof"))
    return {
        "has_di_cross": bool(has_di),
        "has_di_expansion": bool(di_abs_change >= cfg.di_spread_expand_min),
        "has_adx_level_confirmation": bool(
            math.isfinite(adx) and adx >= cfg.adx_level_confirmation_min
        ),
        "has_adx_rising": bool(math.isfinite(delta1) and delta1 >= cfg.adx_rising_min_delta_1),
        "has_adx_acceleration": bool(math.isfinite(accel) and accel >= cfg.adx_accel_min),
        "has_ema_cross": bool(has_ema),
        "has_ema_joint_slope": bool(math.isfinite(joint) and abs(joint) >= cfg.ema_joint_slope_min_atr),
        "has_ema_band_expansion": bool(
            _finite(row.get("band_change_3_atr"), 0.0) >= cfg.band_expand_min_change_atr
        ),
        "has_ema_band_compression": bool(
            _finite(row.get("band_change_3_atr"), 0.0) <= -cfg.band_expand_min_change_atr
            or _finite(row.get("ema_fast_compression_score"), 0.0) >= cfg.compression_max
        ),
        "has_ema59_context": bool(
            abs(_finite(row.get("close_to_ema_59_atr"), np.inf)) <= cfg.near_ema59_atr
        ),
        "has_ema200_context": bool(
            abs(_finite(row.get("close_to_ema_200_atr"), np.inf)) <= cfg.near_ema200_atr
        ),
        "has_breakout_context": et.startswith("range_breakout")
        or bool(row.get("lifecycle_stage") in {"attempt", "confirmed"}),
        "has_regime_proxy_context": str(row.get("regime_proxy") or "unclear") != "unclear",
    }


def _sequence_family(
    row: Mapping[str, Any],
    *,
    cfg: PatternDiscoveryC33BConfig,
    prior_di_lag: int | None = None,
    next_ema_lag: int | None = None,
) -> str:
    core = _core_sequence_label(
        event_type=str(row.get("event_type") or ""),
        prior_di_lag=prior_di_lag,
        next_ema_lag=next_ema_lag,
    )
    adx_bucket = _adx_bucket(_finite(row.get("adx_14"), np.nan), cfg)
    motion = _adx_motion(row, cfg)
    band = _band_state_from_row(row, cfg)
    return f"{core}__{adx_bucket}__{motion}__{band}"


def _assign_pattern_ids(row: dict[str, Any], cfg: PatternDiscoveryC33BConfig) -> dict[str, Any]:
    """Coarse pattern_id for candidates; fine sequence_family for analysis."""
    core = str(row.get("sequence_core") or _event_role(str(row.get("event_type") or "")))
    direction = str(row.get("direction") or "unknown")
    adx_bucket = str(row.get("adx_bucket") or _adx_bucket(_finite(row.get("adx_14"), np.nan), cfg))
    # Candidate grouping: core + ADX bucket + direction (not full band/motion cartesian)
    row["sequence_core"] = core
    row["pattern_family"] = f"{core}__{adx_bucket}"
    row["pattern_id"] = f"{row['pattern_family']}::{direction}"
    row["sequence_family_detail"] = row.get("sequence_family") or f"{core}__{adx_bucket}"
    return row


def build_di_ema_sequences(
    events: Sequence[Mapping[str, Any]],
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events or frame.empty:
        return []
    idx = frame.reset_index(drop=True).copy()
    idx["bar_index"] = pd.to_numeric(idx["bar_index"], errors="coerce").astype("int64")
    by_bar = idx.set_index("bar_index", drop=False)
    rows = [dict(ev) for ev in events if str(ev.get("event_type")) in {"di_cross", "ema_cross"}]
    if not rows:
        return []

    di_events = [r for r in rows if str(r.get("event_type")) == "di_cross"]
    ema_events = [r for r in rows if str(r.get("event_type")) == "ema_cross"]
    ema_by_dir_bar: dict[tuple[str, int], dict[str, Any]] = {}
    di_by_dir_bar: dict[tuple[str, int], dict[str, Any]] = {}
    for ev in ema_events:
        ema_by_dir_bar[(str(ev.get("direction")), int(ev["bar_index"]))] = ev
    for ev in di_events:
        di_by_dir_bar[(str(ev.get("direction")), int(ev["bar_index"]))] = ev

    out: list[dict[str, Any]] = []
    for ev in di_events:
        bar_i = int(ev["bar_index"])
        direction = str(ev.get("direction") or "")
        future_ema: dict[str, Any] | None = None
        lag: int | None = None
        for step in range(0, cfg.di_follow_window_max + 1):
            candidate = ema_by_dir_bar.get((direction, bar_i + step))
            if candidate is not None:
                future_ema = candidate
                lag = step
                break
        row = dict(ev)
        base = by_bar.loc[bar_i].to_dict() if bar_i in by_bar.index else {}
        row.update(base)
        row["paired_ema_event_id"] = None if future_ema is None else future_ema.get("event_id")
        row["paired_ema_bar_index"] = None if future_ema is None else int(future_ema["bar_index"])
        row["paired_ema_lag_bars"] = lag
        row["paired_ema_lag_bucket"] = _lag_bucket(lag)
        row["has_follow_ema_cross_path"] = lag is not None
        row["sequence_core"] = _core_sequence_label(event_type="di_cross", next_ema_lag=lag)
        row["sequence_family"] = _sequence_family(row, cfg=cfg, next_ema_lag=lag)
        row["di_spread_expanding_after_1_path"] = False
        row["di_spread_expanding_after_3_path"] = False
        row["di_spread_collapsing_after_1_path"] = False
        row["di_spread_collapsing_after_3_path"] = False
        if bar_i + 1 < len(frame):
            cur = abs(_finite(frame.iloc[bar_i].get("di_spread"), 0.0))
            nxt1 = abs(_finite(frame.iloc[bar_i + 1].get("di_spread"), 0.0))
            row["di_spread_expanding_after_1_path"] = bool(nxt1 - cur >= cfg.di_spread_expand_min)
            row["di_spread_collapsing_after_1_path"] = bool(cur - nxt1 >= cfg.di_spread_expand_min)
        if bar_i + 3 < len(frame):
            cur = abs(_finite(frame.iloc[bar_i].get("di_spread"), 0.0))
            nxt3 = abs(_finite(frame.iloc[bar_i + 3].get("di_spread"), 0.0))
            row["di_spread_expanding_after_3_path"] = bool(nxt3 - cur >= cfg.di_spread_expand_min)
            row["di_spread_collapsing_after_3_path"] = bool(cur - nxt3 >= cfg.di_spread_expand_min)
        row["di_spread_expanding_asof"] = bool(
            _finite(row.get("di_spread_abs_change_1"), 0.0) >= cfg.di_spread_expand_min
        )
        # ADX path from DI to following EMA (research-only when lag > 0).
        row["adx_change_di_to_ema_path"] = None
        row["adx_rising_di_to_ema_path"] = False
        row["adx_falling_di_to_ema_path"] = False
        if lag is not None and lag > 0 and bar_i + lag < len(frame):
            adx0 = _finite(frame.iloc[bar_i].get("adx_14"), np.nan)
            adx1 = _finite(frame.iloc[bar_i + lag].get("adx_14"), np.nan)
            if math.isfinite(adx0) and math.isfinite(adx1):
                delta = float(adx1 - adx0)
                row["adx_change_di_to_ema_path"] = delta
                row["adx_rising_di_to_ema_path"] = bool(delta >= cfg.adx_rising_min_delta_1)
                row["adx_falling_di_to_ema_path"] = bool(delta <= -cfg.adx_rising_min_delta_1)
        row.update(_policy_flags(row, cfg))
        # Forward EMA follow / DI-without-follow require look-ahead → research path only.
        # Coincident (lag==0) is as-of on the shared bar.
        row["is_policy_feature"] = bool(lag == 0)
        row["is_retrospective"] = bool(lag != 0)
        row["has_path_features"] = True
        if lag is not None and lag > 0:
            row["sequence_core_retro"] = row["sequence_core"]
        out.append(row)

    for ev in ema_events:
        bar_i = int(ev["bar_index"])
        direction = str(ev.get("direction") or "")
        prior_di: dict[str, Any] | None = None
        lag: int | None = None
        for step in range(0, cfg.di_follow_window_max + 1):
            candidate = di_by_dir_bar.get((direction, bar_i - step))
            if candidate is not None:
                prior_di = candidate
                lag = step
                break
        row = dict(ev)
        base = by_bar.loc[bar_i].to_dict() if bar_i in by_bar.index else {}
        row.update(base)
        row["paired_di_event_id"] = None if prior_di is None else prior_di.get("event_id")
        row["paired_di_bar_index"] = None if prior_di is None else int(prior_di["bar_index"])
        row["paired_di_lag_bars"] = lag
        row["paired_di_lag_bucket"] = _lag_bucket(lag)
        row["has_prior_di_cross_asof"] = lag is not None
        row["has_ema_cross_asof"] = True
        row["sequence_core"] = _core_sequence_label(event_type="ema_cross", prior_di_lag=lag)
        row["sequence_family"] = _sequence_family(row, cfg=cfg, prior_di_lag=lag)
        row["di_spread_expanding_asof"] = bool(
            _finite(row.get("di_spread_abs_change_1"), 0.0) >= cfg.di_spread_expand_min
        )
        row["adx_change_di_to_ema_asof"] = None
        row["adx_rising_di_to_ema_asof"] = False
        row["adx_falling_di_to_ema_asof"] = False
        if lag is not None and lag > 0 and prior_di is not None:
            di_bar = int(prior_di["bar_index"])
            adx0 = _finite(frame.iloc[di_bar].get("adx_14"), np.nan)
            adx1 = _finite(frame.iloc[bar_i].get("adx_14"), np.nan)
            if math.isfinite(adx0) and math.isfinite(adx1):
                delta = float(adx1 - adx0)
                row["adx_change_di_to_ema_asof"] = delta
                row["adx_rising_di_to_ema_asof"] = bool(delta >= cfg.adx_rising_min_delta_1)
                row["adx_falling_di_to_ema_asof"] = bool(delta <= -cfg.adx_rising_min_delta_1)
        row.update(_policy_flags(row, cfg))
        # Prior DI is as-of at EMA bar → policy eligible.
        row["is_policy_feature"] = True
        row["is_retrospective"] = False
        row["has_path_features"] = False
        out.append(row)

    return sorted(out, key=lambda r: (int(r["bar_index"]), str(r.get("event_type")), str(r.get("event_id"))))


def build_adx_asof_relationships(
    events: Sequence[Mapping[str, Any]],
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for ev in events:
        bar_i = int(ev["bar_index"])
        base = frame.iloc[bar_i].to_dict()
        row = dict(ev)
        row.update(base)
        flags = _policy_flags(row, cfg)
        row["adx_bucket"] = _adx_bucket(_finite(row.get("adx_14"), np.nan), cfg)
        row["adx_motion"] = _adx_motion(row, cfg)
        row["adx_level_asof"] = _finite(row.get("adx_14"), np.nan)
        row["adx_delta_1_asof"] = _finite(row.get("adx_delta_1"), np.nan)
        row["adx_delta_2_asof"] = _finite(row.get("adx_delta_2"), np.nan)
        row["adx_delta_3_asof"] = _finite(row.get("adx_delta_3"), np.nan)
        row["adx_delta_5_asof"] = _finite(row.get("adx_delta_5"), np.nan)
        row["adx_slope_lin_3_asof"] = _finite(row.get("adx_slope_lin_3"), np.nan)
        row["adx_slope_lin_5_asof"] = _finite(row.get("adx_slope_lin_5"), np.nan)
        row["adx_accel_asof"] = _finite(row.get("adx_accel"), np.nan)
        row["adx_rising_streak_asof"] = int(_finite(row.get("adx_rising_streak"), 0))
        row["adx_rising_into_ema_expansion"] = bool(
            flags["has_adx_rising"] and flags["has_ema_band_expansion"]
        )
        row["adx_falling_despite_ema_expansion"] = bool(
            flags["has_ema_band_expansion"]
            and _finite(row.get("adx_delta_1"), 0.0) <= -cfg.adx_rising_min_delta_1
        )
        row["adx_rises_parallel_ema_expansion_asof"] = bool(row["adx_rising_into_ema_expansion"])
        row["is_policy_feature"] = True
        row["is_retrospective"] = False

        future_horizon = cfg.delayed_horizon
        future = frame.iloc[bar_i : min(len(frame), bar_i + future_horizon + 1)]
        if len(future) > 1:
            fut_adx = pd.to_numeric(future["adx_14"], errors="coerce").to_numpy(dtype=float)[1:]
            cur_adx = _finite(row.get("adx_14"), np.nan)
            future_delta = fut_adx - cur_adx if math.isfinite(cur_adx) else np.asarray([], dtype=float)
            future_delta = future_delta[np.isfinite(future_delta)]
            row["adx_rises_after_ema_cross_path"] = bool(
                future_delta.size and float(np.max(future_delta)) >= cfg.adx_rising_min_delta_1
            )
            row["adx_falls_after_ema_cross_path"] = bool(
                future_delta.size and float(np.min(future_delta)) <= -cfg.adx_rising_min_delta_1
            )
            row["path_features_policy_eligible"] = False
        else:
            row["adx_rises_after_ema_cross_path"] = False
            row["adx_falls_after_ema_cross_path"] = False
            row["path_features_policy_eligible"] = False
        row["path_horizon"] = future_horizon
        # Keep row policy-eligible for as-of ADX fields; path columns are separate.
        rows.append(row)
    return rows


def build_ema_band_dynamics(
    events: Sequence[Mapping[str, Any]],
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for ev in events:
        bar_i = int(ev["bar_index"])
        base = frame.iloc[bar_i].to_dict()
        row = dict(ev)
        row.update(base)
        et = str(row.get("event_type") or "")
        band_change = _finite(row.get("band_change_3_atr"), 0.0)
        row["band_state"] = _band_state_from_row(row, cfg)
        row["band_abs_spread_atr"] = abs(_finite(row.get("ema_9_20_spread_atr"), 0.0))
        row["band_abs_spread_pct"] = abs(
            _finite(row.get("ema_9_20_spread"), 0.0)
            / max(abs(_finite(row.get("close"), 1.0)), 1e-12)
            * 100.0
        )
        row["joint_slope_3_atr"] = _finite(row.get("ema_joint_slope_3_atr"), 0.0)
        row["joint_rising"] = bool(row["joint_slope_3_atr"] >= cfg.ema_joint_slope_min_atr)
        row["joint_falling"] = bool(row["joint_slope_3_atr"] <= -cfg.ema_joint_slope_min_atr)
        row["joint_flat"] = bool(abs(row["joint_slope_3_atr"]) <= cfg.ema_flat_slope_max_atr)
        row["ema_fast_rising_slow_flat"] = bool(row.get("ema_fast_rising_slow_flat"))
        row["ema_band_expansion_flag"] = bool(band_change >= cfg.band_expand_min_change_atr)
        row["ema_band_compression_flag"] = bool(
            band_change <= -cfg.band_expand_min_change_atr
            or _finite(row.get("ema_fast_compression_score"), 0.0) >= cfg.compression_max
        )
        row["expansion_duration"] = int(_finite(row.get("ema_band_expansion_duration"), 0))
        row["expansion_duration_ge_2"] = bool(row.get("ema_band_expansion_ge_2"))
        row["expansion_duration_ge_3"] = bool(row.get("ema_band_expansion_ge_3"))
        row["expansion_duration_ge_5"] = bool(row.get("ema_band_expansion_ge_5"))
        row["cross_with_growing_band"] = bool(et in {"ema_cross", "di_cross"} and band_change >= cfg.band_expand_min_change_atr)
        row["cross_with_shrinking_band"] = bool(et in {"ema_cross", "di_cross"} and band_change <= -cfg.band_expand_min_change_atr)
        row["cross_without_joint_slope"] = bool(
            et in {"ema_cross", "di_cross"} and abs(row["joint_slope_3_atr"]) < cfg.ema_joint_slope_min_atr
        )
        row["joint_slope_without_cross"] = bool(
            et != "ema_cross" and abs(row["joint_slope_3_atr"]) >= cfg.ema_joint_slope_min_atr
        )
        # Path: DI cross followed by joint slope still without EMA cross (look-ahead).
        row["di_then_joint_slope_no_ema_path"] = False
        if et == "di_cross":
            for step in range(1, min(cfg.di_follow_window_max, len(frame) - bar_i - 1) + 1):
                fut = frame.iloc[bar_i + step]
                joint = _finite(fut.get("ema_joint_slope_3_atr"), 0.0)
                # EMA cross on future bar would be detected elsewhere; here only joint slope path.
                if abs(joint) >= cfg.ema_joint_slope_min_atr:
                    row["di_then_joint_slope_no_ema_path"] = True
                    break
        row["is_policy_feature"] = True
        row["is_retrospective"] = bool(row["di_then_joint_slope_no_ema_path"])
        rows.append(row)
    return rows


def compute_multi_horizon_outcomes_c33b(
    events: Sequence[Mapping[str, Any]],
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events or frame.empty:
        return []
    arrays = build_price_arrays(frame)
    horizons = tuple(sorted(set(cfg.horizons_for_class) | set(cfg.optional_horizons)))
    rows: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        side = _direction_side(str(ev.get("direction")))
        bar_i = int(ev["bar_index"])
        ref_close = _finite(ev.get("close"), np.nan)
        primary = compute_horizon_outcome(
            bar_index=bar_i,
            horizon=cfg.delayed_horizon,
            reference_close=ref_close,
            side=side,
            arrays=arrays,
        )
        for h in horizons:
            out = compute_horizon_outcome(
                bar_index=bar_i,
                horizon=int(h),
                reference_close=ref_close,
                side=side,
                arrays=arrays,
            )
            for key, value in out.items():
                if key == "horizon":
                    continue
                row[f"h{h}_{key}"] = value

        h6 = row.get("h6_evaluable") is True
        h12 = row.get("h12_evaluable") is True
        h24 = row.get("h24_evaluable") is True
        h6_mfe = _finite(row.get("h6_mfe_pct"), np.nan)
        h6_mae = _finite(row.get("h6_mae_pct"), np.nan)
        h12_mae = _finite(row.get("h12_mae_pct"), np.nan)
        h12_mfe = _finite(row.get("h12_mfe_pct"), np.nan)
        h12_raw = _finite(row.get("h12_raw_close_return_pct"), np.nan)
        h24_mfe = _finite(row.get("h24_mfe_pct"), np.nan)
        h6_clean = (
            h6
            and math.isfinite(h6_mfe)
            and h6_mfe >= cfg.clean_mfe_min
            and math.isfinite(h6_mae)
            and h6_mae <= cfg.clean_mae_max
            and bool(row.get("h6_direction_hit"))
        )
        h12_clean = (
            h12
            and math.isfinite(h12_mfe)
            and h12_mfe >= cfg.clean_mfe_min
            and math.isfinite(h12_mae)
            and h12_mae <= cfg.clean_mae_max
            and bool(row.get("h12_direction_hit"))
        )
        h24_recovery = (
            h24
            and math.isfinite(h24_mfe)
            and h24_mfe >= cfg.recovery_mfe_min
            and bool(row.get("h24_direction_hit"))
        )
        # Early adverse: material MAE early, then later recovery — not pure adverse_reversal.
        early_adverse = (
            h12
            and math.isfinite(h12_mae)
            and h12_mae >= cfg.early_adverse_mae
            and h24_recovery
        )
        # Small early MAE before follow-through should not force adverse_reversal.
        mild_early_drawdown = (
            h12
            and math.isfinite(h12_mae)
            and h12_mae > cfg.clean_mae_max
            and h12_mae < cfg.adverse_mae_min
            and math.isfinite(h12_mfe)
            and h12_mfe >= cfg.clean_mfe_min
            and bool(row.get("h12_direction_hit"))
        )
        if early_adverse:
            row["outcome_class"] = "early_adverse_then_recovery"
        elif h6 and not h6_clean and h12_clean:
            row["outcome_class"] = "delayed_success"
        elif h12_clean or mild_early_drawdown:
            row["outcome_class"] = "clean_success"
        elif (
            math.isfinite(h12_mfe)
            and h12_mfe <= cfg.weak_mfe_max
            and abs(h12_raw) <= cfg.weak_mfe_max
            and (not math.isfinite(h12_mae) or h12_mae <= cfg.weak_mfe_max)
        ):
            row["outcome_class"] = "neutral"
        elif math.isfinite(h12_mfe) and 0.0 < h12_mfe <= cfg.weak_mfe_max:
            row["outcome_class"] = "weak_followthrough"
        elif (
            math.isfinite(h12_mae)
            and h12_mae >= cfg.adverse_mae_min
            and not h24_recovery
            and (not math.isfinite(h12_mfe) or h12_mfe < cfg.clean_mfe_min)
        ):
            row["outcome_class"] = "adverse_reversal"
        else:
            row["outcome_class"] = "failed_followthrough"
        row["primary_outcome_horizon"] = cfg.delayed_horizon
        row["is_policy_feature"] = True
        row["is_retrospective"] = False
        rows.append(row)
    return rows


def _bootstrap_ci(
    values: Sequence[float],
    *,
    samples: int,
    seed: int,
    stat: Callable[[np.ndarray], float],
) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    arr = np.asarray(list(values), dtype=float)
    if len(arr) == 1:
        val = float(stat(arr))
        return val, val
    rng = np.random.default_rng(seed)
    stats = np.empty(samples, dtype=float)
    n = len(arr)
    for i in range(samples):
        sample = arr[rng.integers(0, n, size=n)]
        stats[i] = float(stat(sample))
    return float(np.quantile(stats, 0.025)), float(np.quantile(stats, 0.975))


def _event_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in events:
        key = str(ev.get("outcome_class") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _summarize_split(events: Sequence[Mapping[str, Any]], cfg: PatternDiscoveryC33BConfig) -> dict[str, Any]:
    clean = [1.0 if str(ev.get("outcome_class")) == "clean_success" else 0.0 for ev in events]
    dirs = [
        float(ev.get("h12_directional_close_return_pct"))
        for ev in events
        if ev.get("h12_directional_close_return_pct") is not None
    ]
    mfes = [float(ev.get("h12_mfe_pct")) for ev in events if ev.get("h12_mfe_pct") is not None]
    abs_dirs = [abs(v) for v in dirs]
    max_share = (max(abs_dirs) / sum(abs_dirs)) if abs_dirs and sum(abs_dirs) > 0 else None
    rates = {
        "clean_rate": _mean(clean),
        "mean_dir_return": _mean(dirs),
        "mean_mfe": _mean(mfes),
    }
    clean_ci = _bootstrap_ci(
        clean,
        samples=cfg.bootstrap_samples,
        seed=cfg.bootstrap_seed,
        stat=lambda x: float(np.mean(x)),
    )
    dir_ci = _bootstrap_ci(
        dirs,
        samples=cfg.bootstrap_samples,
        seed=cfg.bootstrap_seed + 1,
        stat=lambda x: float(np.mean(x)),
    )
    mfe_ci = _bootstrap_ci(
        mfes,
        samples=cfg.bootstrap_samples,
        seed=cfg.bootstrap_seed + 2,
        stat=lambda x: float(np.mean(x)),
    )
    return {
        "n_events": len(events),
        "counts": _event_counts(events),
        "clean_rate": rates["clean_rate"],
        "mean_dir_return": rates["mean_dir_return"],
        "mean_mfe": rates["mean_mfe"],
        "clean_rate_ci": clean_ci,
        "mean_dir_return_ci": dir_ci,
        "mean_mfe_ci": mfe_ci,
        "max_abs_dir_return_share": max_share,
        "small_sample": len(events) < cfg.min_pattern_events_discovery,
    }


def _candidate_status(
    discovery: Mapping[str, Any],
    validation: Mapping[str, Any],
    cfg: PatternDiscoveryC33BConfig,
) -> str:
    nd = int(discovery.get("n_events") or 0)
    nv = int(validation.get("n_events") or 0)
    if nd < cfg.min_pattern_events_discovery or nv < cfg.min_pattern_events_validation:
        return "small_sample"

    d_dir = discovery.get("mean_dir_return")
    v_dir = validation.get("mean_dir_return")
    d_mfe = discovery.get("mean_mfe")
    v_mfe = validation.get("mean_mfe")
    if _sign(d_dir) != 0 and _sign(v_dir) != 0 and _sign(d_dir) != _sign(v_dir):
        return "unstable"
    if _sign(d_mfe) != 0 and _sign(v_mfe) != 0 and _sign(d_mfe) != _sign(v_mfe):
        return "unstable"
    # Extreme single-event dependence on either split.
    for split in (discovery, validation):
        share = split.get("max_abs_dir_return_share")
        if share is not None and float(share) >= 0.50 and int(split.get("n_events") or 0) >= 3:
            return "unstable"
    return "research_candidate"


def _pattern_row(
    events: Sequence[Mapping[str, Any]],
    *,
    split_name: str,
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        grouped.setdefault(str(ev.get("pattern_id") or "misc"), []).append(dict(ev))
    rows: list[dict[str, Any]] = []
    for pid, items in sorted(grouped.items()):
        summary = _summarize_split(items, cfg)
        row = {
            "split": split_name,
            "pattern_id": pid,
            "pattern_family": str(items[0].get("pattern_family") or "misc"),
            "sequence_family": str(items[0].get("sequence_family") or "misc"),
            "status": "research_candidate" if summary["n_events"] >= cfg.min_pattern_events_discovery else "small_sample",
            **summary,
        }
        rows.append(row)
    return rows


def build_candidate_patterns_c33b(
    discovery_events: Sequence[Mapping[str, Any]],
    validation_events: Sequence[Mapping[str, Any]],
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    # Policy-eligible events only — no retrospective / path sequence cores.
    disc_events = [
        ev
        for ev in discovery_events
        if bool(ev.get("is_policy_feature", True)) and not bool(ev.get("is_retrospective", False))
    ]
    val_events = [
        ev
        for ev in validation_events
        if bool(ev.get("is_policy_feature", True)) and not bool(ev.get("is_retrospective", False))
    ]
    disc_rows = _pattern_row(disc_events, split_name="discovery", cfg=cfg)
    val_rows = _pattern_row(val_events, split_name="validation", cfg=cfg)
    val_map = {str(r["pattern_id"]): r for r in val_rows}
    rows: list[dict[str, Any]] = []
    for disc in disc_rows:
        pid = str(disc["pattern_id"])
        val = val_map.get(
            pid,
            {
                "n_events": 0,
                "counts": {},
                "clean_rate": None,
                "mean_dir_return": None,
                "mean_mfe": None,
                "clean_rate_ci": (None, None),
                "mean_dir_return_ci": (None, None),
                "mean_mfe_ci": (None, None),
                "max_abs_dir_return_share": None,
                "small_sample": True,
            },
        )
        status = _candidate_status(disc, val, cfg)
        d_clean_ci = disc["clean_rate_ci"]
        v_clean_ci = val["clean_rate_ci"]
        d_dir_ci = disc["mean_dir_return_ci"]
        v_dir_ci = val["mean_dir_return_ci"]
        row = {
            "pattern_id": pid,
            "pattern_family": disc["pattern_family"],
            "sequence_family": disc["sequence_family"],
            "sequence_core": next(
                (
                    str(ev.get("sequence_core"))
                    for ev in disc_events
                    if str(ev.get("pattern_id")) == pid and ev.get("sequence_core")
                ),
                str(disc.get("pattern_family") or "misc").split("__")[0],
            ),
            "status": status,
            "small_sample": status == "small_sample",
            "n_discovery": disc["n_events"],
            "n_validation": int(val.get("n_events") or 0),
            "discovery": {
                "n_events": disc["n_events"],
                "clean_rate": disc["clean_rate"],
                "mean_dir_return": disc["mean_dir_return"],
                "mean_mfe": disc["mean_mfe"],
                "clean_rate_ci": list(d_clean_ci),
                "mean_dir_return_ci": list(d_dir_ci),
                "mean_mfe_ci": list(disc["mean_mfe_ci"]),
                "max_abs_dir_return_share": disc.get("max_abs_dir_return_share"),
                "counts": disc.get("counts"),
            },
            "validation": {
                "n_events": int(val.get("n_events") or 0),
                "clean_rate": val.get("clean_rate"),
                "mean_dir_return": val.get("mean_dir_return"),
                "mean_mfe": val.get("mean_mfe"),
                "clean_rate_ci": list(v_clean_ci),
                "mean_dir_return_ci": list(v_dir_ci),
                "mean_mfe_ci": list(val["mean_mfe_ci"]),
                "max_abs_dir_return_share": val.get("max_abs_dir_return_share"),
                "counts": val.get("counts"),
            },
            "discovery_clean_rate": disc["clean_rate"],
            "validation_clean_rate": val.get("clean_rate"),
            "discovery_mean_dir_return": disc["mean_dir_return"],
            "validation_mean_dir_return": val.get("mean_dir_return"),
            "discovery_mean_mfe": disc["mean_mfe"],
            "validation_mean_mfe": val.get("mean_mfe"),
            "discovery_clean_rate_ci_low": d_clean_ci[0],
            "discovery_clean_rate_ci_high": d_clean_ci[1],
            "validation_clean_rate_ci_low": v_clean_ci[0],
            "validation_clean_rate_ci_high": v_clean_ci[1],
            "discovery_mean_dir_return_ci_low": d_dir_ci[0],
            "discovery_mean_dir_return_ci_high": d_dir_ci[1],
            "validation_mean_dir_return_ci_low": v_dir_ci[0],
            "validation_mean_dir_return_ci_high": v_dir_ci[1],
            "discovery_mean_mfe_ci_low": disc["mean_mfe_ci"][0],
            "discovery_mean_mfe_ci_high": disc["mean_mfe_ci"][1],
            "validation_mean_mfe_ci_low": val["mean_mfe_ci"][0],
            "validation_mean_mfe_ci_high": val["mean_mfe_ci"][1],
            "directional_sign_flip": bool(
                _sign(disc["mean_dir_return"]) != 0
                and _sign(val.get("mean_dir_return")) != 0
                and _sign(disc["mean_dir_return"]) != _sign(val.get("mean_dir_return"))
            ),
            "mfe_sign_flip": bool(
                _sign(disc["mean_mfe"]) != 0
                and _sign(val.get("mean_mfe")) != 0
                and _sign(disc["mean_mfe"]) != _sign(val.get("mean_mfe"))
            ),
            "contains_retrospective_features": False,
        }
        rows.append(row)
    return rows


def pattern_component_ablation(
    events: Sequence[Mapping[str, Any]],
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events:
        return []
    flags = [
        "has_di_cross",
        "has_di_expansion",
        "has_adx_level_confirmation",
        "has_adx_rising",
        "has_adx_acceleration",
        "has_ema_cross",
        "has_ema_joint_slope",
        "has_ema_band_expansion",
        "has_ema_band_compression",
        "has_ema59_context",
        "has_ema200_context",
        "has_breakout_context",
        "has_regime_proxy_context",
    ]
    rows: list[dict[str, Any]] = []
    for flag in flags:
        pos = [ev for ev in events if bool(ev.get(flag))]
        neg = [ev for ev in events if not bool(ev.get(flag))]
        def _stats(sub: Sequence[Mapping[str, Any]]) -> tuple[float | None, float | None]:
            if not sub:
                return None, None
            clean = [1.0 if str(ev.get("outcome_class")) == "clean_success" else 0.0 for ev in sub]
            dirs = [
                float(ev.get("h12_directional_close_return_pct"))
                for ev in sub
                if ev.get("h12_directional_close_return_pct") is not None
            ]
            return _mean(clean), _mean(dirs)
        clean_pos, dir_pos = _stats(pos)
        clean_neg, dir_neg = _stats(neg)
        rows.append(
            {
                "component": flag,
                "n_true": len(pos),
                "n_false": len(neg),
                "clean_rate_true": clean_pos,
                "clean_rate_false": clean_neg,
                "mean_dir_return_true": dir_pos,
                "mean_dir_return_false": dir_neg,
                "delta_clean_rate": None
                if clean_pos is None or clean_neg is None
                else clean_pos - clean_neg,
                "delta_mean_dir_return": None
                if dir_pos is None or dir_neg is None
                else dir_pos - dir_neg,
            }
        )
    return rows


def threshold_sensitivity_c33b(
    events: Sequence[Mapping[str, Any]],
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    if not events:
        return []
    rows: list[dict[str, Any]] = []
    primary = [ev for ev in events if ev.get("h12_evaluable") is True]
    clean_primary = [ev for ev in primary if str(ev.get("outcome_class")) == "clean_success"]
    for scale in (0.9, 1.1):
        rows.append(
            {
                "threshold": "clean_mfe_min",
                "scale": scale,
                "scaled_value": cfg.clean_mfe_min * scale,
                "event_count": sum(
                    1
                    for ev in primary
                    if _finite(ev.get("h12_mfe_pct"), -np.inf) >= cfg.clean_mfe_min * scale
                ),
                "clean_rate": len(clean_primary) / len(primary) if primary else None,
            }
        )
        rows.append(
            {
                "threshold": "clean_mae_max",
                "scale": scale,
                "scaled_value": cfg.clean_mae_max * scale,
                "event_count": sum(
                    1
                    for ev in primary
                    if _finite(ev.get("h12_mae_pct"), np.inf) <= cfg.clean_mae_max * scale
                ),
                "clean_rate": len(clean_primary) / len(primary) if primary else None,
            }
        )
        rows.append(
            {
                "threshold": "band_expand_min_change_atr",
                "scale": scale,
                "scaled_value": cfg.band_expand_min_change_atr * scale,
                "event_count": sum(
                    1
                    for ev in events
                    if _finite(ev.get("band_change_3_atr"), 0.0) >= cfg.band_expand_min_change_atr * scale
                ),
                "clean_rate": len(clean_primary) / len(primary) if primary else None,
            }
        )
        rows.append(
            {
                "threshold": "adx_level_confirmation_min",
                "scale": scale,
                "scaled_value": cfg.adx_level_confirmation_min * scale,
                "event_count": sum(
                    1
                    for ev in events
                    if _finite(ev.get("adx_14"), 0.0) >= cfg.adx_level_confirmation_min * scale
                ),
                "clean_rate": len(clean_primary) / len(primary) if primary else None,
            }
        )
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def _sort_event_rows(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [dict(ev) for ev in events],
        key=lambda ev: (int(ev.get("bar_index") or 0), str(ev.get("event_timestamp") or ""), str(ev.get("event_id") or "")),
    )


def _merge_event_row(
    ev: Mapping[str, Any],
    *,
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> dict[str, Any]:
    row = dict(ev)
    bar_i = int(ev["bar_index"])
    frame_row = frame.iloc[bar_i].to_dict()
    row.update(frame_row)
    row["event_role"] = _event_role(str(row.get("event_type") or ""))
    row["band_state"] = _band_state_from_row(row, cfg)
    row["adx_bucket"] = _adx_bucket(_finite(row.get("adx_14"), np.nan), cfg)
    row["adx_motion"] = _adx_motion(row, cfg)
    if not row.get("sequence_core"):
        row["sequence_core"] = row["event_role"]
    if not row.get("sequence_family"):
        row["sequence_family"] = _sequence_family(row, cfg=cfg)
    row.setdefault("is_policy_feature", True)
    row.setdefault("is_retrospective", False)
    row.update(_policy_flags(row, cfg))
    return _assign_pattern_ids(row, cfg)


def _apply_sequences_to_events(
    events: Sequence[Mapping[str, Any]],
    sequences: Sequence[Mapping[str, Any]],
    cfg: PatternDiscoveryC33BConfig,
) -> list[dict[str, Any]]:
    seq_by_id = {str(s.get("event_id")): s for s in sequences}
    out: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        seq = seq_by_id.get(str(ev.get("event_id")))
        if seq is not None:
            for key in (
                "sequence_core",
                "sequence_family",
                "paired_ema_event_id",
                "paired_ema_bar_index",
                "paired_ema_lag_bars",
                "paired_ema_lag_bucket",
                "paired_di_event_id",
                "paired_di_bar_index",
                "paired_di_lag_bars",
                "paired_di_lag_bucket",
                "has_follow_ema_cross_path",
                "has_prior_di_cross_asof",
                "has_ema_cross_asof",
                "di_spread_expanding_asof",
                "di_spread_expanding_after_1_path",
                "di_spread_expanding_after_3_path",
                "di_spread_collapsing_after_1_path",
                "di_spread_collapsing_after_3_path",
                "adx_change_di_to_ema_path",
                "adx_rising_di_to_ema_path",
                "adx_falling_di_to_ema_path",
                "adx_change_di_to_ema_asof",
                "adx_rising_di_to_ema_asof",
                "adx_falling_di_to_ema_asof",
                "sequence_core_retro",
                "is_policy_feature",
                "is_retrospective",
                "has_path_features",
            ):
                if key in seq:
                    row[key] = seq[key]
        else:
            row.setdefault("sequence_core", _event_role(str(row.get("event_type") or "")))
            row.setdefault("is_policy_feature", True)
            row.setdefault("is_retrospective", False)
        row.update(_policy_flags(row, cfg))
        out.append(_assign_pattern_ids(row, cfg))
    return out


def _candidate_rows_for_csv(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for row in rows:
        item = {k: v for k, v in dict(row).items() if k not in {"discovery", "validation"}}
        flat.append(item)
    return flat


def run_c33b_audit(
    *,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    load_start: str = "2026-01-01",
    load_end: str = "2026-05-15",
    analyze_start: str = "2026-02-01",
    analyze_end: str = "2026-04-30",
    discovery_end: str | None = "2026-03-20",
    horizons: tuple[int, ...] | None = None,
    min_pattern_events: int = 20,
    output_dir: Path = DEFAULT_OUT,
    cache_dir: Path | None = None,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = PatternDiscoveryC33BConfig(
        discovery_end=discovery_end,
        min_pattern_events_discovery=min_pattern_events,
        min_pattern_events_validation=max(10, min_pattern_events // 2 if min_pattern_events > 10 else 10),
    )
    horizons = tuple(horizons or (*cfg.horizons, *cfg.optional_horizons))

    t0 = time.perf_counter()
    frame = build_discovery_frame(
        symbol=symbol,
        timeframe=timeframe,
        load_start=load_start,
        load_end=load_end,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        cache_dir=cache_dir or (output_dir / ".cache" / "indicator_features"),
    )
    t_load = time.perf_counter()
    if frame.empty:
        raise RuntimeError("no discovery rows available for requested window")

    frame = enrich_discovery_frame(frame, cfg)

    base_cfg = C33AConfig()
    base_events = [
        *detect_ema_crosses(frame),
        *detect_di_crosses(frame),
        *detect_ema_expansions(frame),
        *detect_range_breakouts(frame, base_cfg),
        *detect_trend_follow(frame, base_cfg),
    ]
    t_detect = time.perf_counter()

    a0 = _coerce_ts(analyze_start)
    a1 = _coerce_ts(analyze_end)
    filtered = [
        ev
        for ev in _sort_event_rows(base_events)
        if a0 <= _coerce_ts(ev.get("event_timestamp") or ev.get("decision_time")) <= a1
    ]

    enriched_events = [_merge_event_row(ev, frame=frame, cfg=cfg) for ev in filtered]
    t_enrich = time.perf_counter()

    di_ema_sequences = build_di_ema_sequences(enriched_events, frame, cfg)
    for seq in di_ema_sequences:
        _assign_pattern_ids(seq, cfg)
    enriched_events = _apply_sequences_to_events(enriched_events, di_ema_sequences, cfg)
    adx_relationships = build_adx_asof_relationships(enriched_events, frame, cfg)
    ema_band = build_ema_band_dynamics(enriched_events, frame, cfg)
    t_relationships = time.perf_counter()

    outcomes = compute_multi_horizon_outcomes_c33b(enriched_events, frame, cfg)
    outcome_map = {str(r["event_id"]): r for r in outcomes}
    event_rows: list[dict[str, Any]] = []
    for ev in enriched_events:
        row = dict(ev)
        out = outcome_map.get(str(ev["event_id"]), {})
        # Preserve as-of / sequence policy flags; overlay horizon metrics and class.
        for key, value in out.items():
            if key.startswith("h") or key in {"outcome_class", "primary_outcome_horizon"}:
                row[key] = value
        row.update(_policy_flags(row, cfg))
        event_rows.append(_assign_pattern_ids(row, cfg))
    event_rows = _sort_event_rows(event_rows)
    t_outcomes = time.perf_counter()

    split = split_discovery_validation(event_rows, discovery_end or cfg.discovery_end)
    for ev in split["discovery"]:
        ev["split"] = "discovery"
    for ev in split["validation"]:
        ev["split"] = "validation"
    combined = [*split["discovery"], *split["validation"]]

    pattern_rows_discovery = _pattern_row(split["discovery"], split_name="discovery", cfg=cfg)
    pattern_rows_validation = _pattern_row(split["validation"], split_name="validation", cfg=cfg)
    candidate_rows = build_candidate_patterns_c33b(split["discovery"], split["validation"], cfg)
    ablation_rows = pattern_component_ablation(combined, cfg)
    sensitivity_rows = threshold_sensitivity_c33b(combined, cfg)
    t_candidates = time.perf_counter()

    _write_csv(output_dir / "events_enriched.csv", event_rows)
    _write_csv(output_dir / "di_ema_sequences.csv", di_ema_sequences)
    _write_csv(output_dir / "adx_asof_relationships.csv", adx_relationships)
    _write_csv(output_dir / "ema_band_dynamics.csv", ema_band)
    _write_csv(output_dir / "multi_horizon_outcomes.csv", outcomes)
    _write_csv(output_dir / "pattern_component_ablation.csv", ablation_rows)
    _write_csv(output_dir / "candidate_patterns_c3_3b.csv", _candidate_rows_for_csv(candidate_rows))
    (output_dir / "candidate_patterns_c3_3b.json").write_text(
        json.dumps(json_safe(candidate_rows), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_csv(output_dir / "threshold_sensitivity_c3_3b.csv", sensitivity_rows)
    _write_csv(output_dir / "discovery_pattern_metrics.csv", pattern_rows_discovery)
    _write_csv(output_dir / "validation_pattern_metrics.csv", pattern_rows_validation)

    pine_meta = export_trend_detector_artifacts(
        frame=frame,
        output_dir=output_dir,
        cfg=cfg,
        symbol=symbol,
        timeframe=timeframe,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        di_ema_sequences=di_ema_sequences,
        outcomes=outcomes,
    )
    t_export_prep = time.perf_counter()

    timings = {
        "load_s": round(t_load - t0, 4),
        "detect_s": round(t_detect - t_load, 4),
        "enrich_s": round(t_enrich - t_detect, 4),
        "relationship_s": round(t_relationships - t_enrich, 4),
        "outcomes_s": round(t_outcomes - t_relationships, 4),
        "candidate_s": round(t_candidates - t_outcomes, 4),
        "pine_export_s": round(t_export_prep - t_candidates, 4),
        "export_s": None,
        "total_s": None,
    }

    candidate_summary = {
        "n_candidates": len(candidate_rows),
        "n_research_candidate": sum(1 for r in candidate_rows if r["status"] == "research_candidate"),
        "n_small_sample": sum(1 for r in candidate_rows if r["status"] == "small_sample"),
        "n_unstable": sum(1 for r in candidate_rows if r["status"] == "unstable"),
        "top_candidates": [
            {
                "pattern_id": r["pattern_id"],
                "status": r["status"],
                "n_discovery": r["n_discovery"],
                "n_validation": r["n_validation"],
            }
            for r in candidate_rows[:10]
        ],
    }

    event_counts = {
        "total": len(event_rows),
        "di_crosses": sum(1 for r in event_rows if r.get("event_type") == "di_cross"),
        "ema_crosses": sum(1 for r in event_rows if r.get("event_type") == "ema_cross"),
        "ema_expansions": sum(1 for r in event_rows if r.get("event_type") == "ema_expansion_start"),
        "range_breakouts": sum(1 for r in event_rows if str(r.get("event_type")).startswith("range_breakout")),
        "trend_follow": sum(1 for r in event_rows if str(r.get("event_type")).startswith("trend_follow")),
        "clean_success": sum(1 for r in event_rows if r.get("outcome_class") == "clean_success"),
        "delayed_success": sum(1 for r in event_rows if r.get("outcome_class") == "delayed_success"),
        "weak_followthrough": sum(1 for r in event_rows if r.get("outcome_class") == "weak_followthrough"),
        "neutral": sum(1 for r in event_rows if r.get("outcome_class") == "neutral"),
        "early_adverse_then_recovery": sum(
            1 for r in event_rows if r.get("outcome_class") == "early_adverse_then_recovery"
        ),
        "failed_followthrough": sum(1 for r in event_rows if r.get("outcome_class") == "failed_followthrough"),
        "adverse_reversal": sum(1 for r in event_rows if r.get("outcome_class") == "adverse_reversal"),
    }
    di_ema_core_counts: dict[str, int] = {}
    di_lag_counts: dict[str, int] = {}
    for seq in di_ema_sequences:
        core = str(seq.get("sequence_core") or "unknown")
        di_ema_core_counts[core] = di_ema_core_counts.get(core, 0) + 1
        if seq.get("event_type") == "di_cross":
            bucket = str(seq.get("paired_ema_lag_bucket") or "none")
            di_lag_counts[bucket] = di_lag_counts.get(bucket, 0) + 1

    summary_core = {
        "phase": "C3_3B_indicator_pattern_discovery",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "discovery_end": discovery_end or cfg.discovery_end,
        "config": cfg.to_dict(),
        "baseline": baseline,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_confirmed": bool(baseline.get("hash_matches")),
        "event_hash": events_content_hash(event_rows),
        "event_counts": event_counts,
        "di_ema_sequence_cores": di_ema_core_counts,
        "di_to_ema_lag_buckets": di_lag_counts,
        "candidate_summary": candidate_summary,
        "trend_detector_pine": pine_meta,
        "policy_feature_columns": [
            "has_di_cross",
            "has_di_expansion",
            "has_adx_level_confirmation",
            "has_adx_rising",
            "has_adx_acceleration",
            "has_ema_cross",
            "has_ema_joint_slope",
            "has_ema_band_expansion",
            "has_ema_band_compression",
            "has_ema59_context",
            "has_ema200_context",
            "has_breakout_context",
            "has_regime_proxy_context",
        ],
        "retrospective_columns": [
            "adx_rises_after_ema_cross_path",
            "adx_falls_after_ema_cross_path",
            "paired_ema_lag_bars",
            "di_spread_expanding_after_1_path",
            "di_spread_expanding_after_3_path",
            "di_spread_collapsing_after_1_path",
            "di_spread_collapsing_after_3_path",
            "adx_change_di_to_ema_path",
            "adx_rising_di_to_ema_path",
            "adx_falling_di_to_ema_path",
            "di_then_joint_slope_no_ema_path",
            "sequence_core_retro",
        ],
        "safety": {
            "research_only": True,
            "no_classifier_changes": True,
            "no_production_config_changes": True,
            "baseline_read_only": True,
        },
        "performance": timings,
        "artifacts": {
            "events_enriched": "events_enriched.csv",
            "di_ema_sequences": "di_ema_sequences.csv",
            "adx_asof_relationships": "adx_asof_relationships.csv",
            "ema_band_dynamics": "ema_band_dynamics.csv",
            "multi_horizon_outcomes": "multi_horizon_outcomes.csv",
            "pattern_component_ablation": "pattern_component_ablation.csv",
            "candidate_patterns_csv": "candidate_patterns_c3_3b.csv",
            "candidate_patterns_json": "candidate_patterns_c3_3b.json",
            "threshold_sensitivity": "threshold_sensitivity_c3_3b.csv",
            "trend_detector_asof_pine": ASOF_PINE_NAME,
            "trend_detector_outcome_pine": OUTCOME_PINE_NAME,
            "trend_detector_state_counts": "trend_detector_state_counts.csv",
            "trend_detector_transitions": "trend_detector_transitions.csv",
            "trend_detector_component_summary": "trend_detector_component_summary.csv",
            "summary": "summary.json",
            "run_summary": "run_summary.json",
            "manifest": "manifest.json",
        },
    }
    summary_blob = json.dumps(json_safe(summary_core), sort_keys=True, separators=(",", ":"))
    deterministic_hash = hashlib.sha256(summary_blob.encode("utf-8")).hexdigest()
    timings["export_s"] = round(time.perf_counter() - t_export_prep, 4)
    timings["total_s"] = round(time.perf_counter() - t0, 4)
    summary = {**summary_core, "deterministic_hash": deterministic_hash, "performance": timings}
    summary_text = json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n"
    (output_dir / "summary.json").write_text(summary_text, encoding="utf-8")
    (output_dir / "run_summary.json").write_text(summary_text, encoding="utf-8")
    manifest = {
        "phase": summary["phase"],
        "summary_file": "summary.json",
        "run_summary_file": "run_summary.json",
        "hash": deterministic_hash,
        "artifacts": summary_core["artifacts"],
        "row_counts": {
            "events_enriched": len(event_rows),
            "di_ema_sequences": len(di_ema_sequences),
            "adx_asof_relationships": len(adx_relationships),
            "ema_band_dynamics": len(ema_band),
            "multi_horizon_outcomes": len(outcomes),
            "candidate_patterns": len(candidate_rows),
            "threshold_sensitivity": len(sensitivity_rows),
            "pattern_component_ablation": len(ablation_rows),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(json_safe(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C3.3B indicator pattern discovery")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-01-01")
    parser.add_argument("--load-end", default="2026-05-15")
    parser.add_argument("--analyze-start", default="2026-02-01")
    parser.add_argument("--analyze-end", default="2026-04-30")
    parser.add_argument("--discovery-end", default="2026-03-20")
    parser.add_argument("--min-pattern-events", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    summary = run_c33b_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        discovery_end=args.discovery_end,
        min_pattern_events=args.min_pattern_events,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "n_events": summary["event_counts"]["total"],
                "n_candidates": summary["candidate_summary"]["n_candidates"],
                "candidate_summary": summary["candidate_summary"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
