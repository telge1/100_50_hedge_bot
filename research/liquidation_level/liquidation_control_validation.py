"""Causal matched-control validation for a frozen liquidation-level winner config.

Compares winner path metrics against time-/volatility-/volume-matched controls.
Estimated LuxAlgo-style levels only — not real exchange liquidations.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_backtest import assign_sample, in_sample_cut
from research.liquidation_level.liquidation_config import config_hash
from research.liquidation_level.liquidation_levels import (
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevelConfig,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
    replay_liquidation_levels,
)
from research.liquidation_level.liquidation_optimizer import build_lite_upper_events
from research.liquidation_level.short_squeeze_path_audit import (
    MINUTES_PER_CANDLE,
    analyze_short_path,
    classify_path_category,
)

WINNER_CONFIG_ID = "2eab613f172d928e"
EXPECTED_FULL = 2696
EXPECTED_IS = 1824
EXPECTED_OOS = 872
DEFAULT_HORIZONS = (1, 3, 6, 12, 24, 48, 96)
PRIMARY_HORIZON = 50  # winner count definition
MIN_DISTANCE_CANDLES = 96
N_BUCKETS = 5


def frozen_winner_config() -> LiquidationLevelConfig:
    return LiquidationLevelConfig(
        reference_price="close",
        volume_threshold=1.3,
        volatility_threshold=20.0,
        leverages=(25, 50, 100),
        cluster_distance_pct=0.15,
        cluster_min_level_count=1,
        cluster_min_total_strength=4,
        volume_sma_period=13,
        max_active_levels=500,
        minimum_move_divisor=333.0,
        sweep_strict_cross=True,
        reclaim_window_candles=3,
        path_horizon_candles=50,
    )


@dataclass
class ControlValidationConfig:
    control_runs: int = 1000
    random_seed: int = 42
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    min_distance_candles: int = MIN_DISTANCE_CANDLES
    n_buckets: int = N_BUCKETS
    hour_tolerance_strict: int = 2
    hour_tolerance_medium: int = 4
    expected_full: int = EXPECTED_FULL
    expected_is: int = EXPECTED_IS
    expected_oos: int = EXPECTED_OOS
    winner_config_id: str = WINNER_CONFIG_ID
    progress_every: int = 25
    seed_sensitivity_seeds: tuple[int, ...] = (42, 43, 44, 45, 46)
    seed_sensitivity_runs: int = 200


@dataclass
class ValidationEvent:
    event_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    entry_index: int
    entry_timestamp: pd.Timestamp
    entry_price: float
    side: str
    direction: str
    leverage: int
    swept_level_count: int
    swept_total_strength: int
    swept_leverages: tuple[int, ...]
    cluster_center_price: float | None
    cluster_distance_pct: float
    sample: str
    month: str
    hour_utc: int
    volatility_bucket: int
    atr_bucket: int
    volume_bucket: int
    atr_pct: float
    volume_ratio: float
    leverage_group_only: str
    leverage_group_flags: dict[str, bool]


def hour_cyclic_distance(h1: int, h2: int) -> int:
    d = abs(int(h1) - int(h2)) % 24
    return min(d, 24 - d)


def compute_atr_pct(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    tr = np.empty(n, dtype=float)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = pd.Series(tr).rolling(period, min_periods=period).mean().to_numpy(float)
    out = np.full(n, np.nan)
    for i in range(n):
        if np.isfinite(atr[i]) and closes[i] > 0:
            out[i] = float(atr[i] / closes[i] * 100.0)
    return out


def compute_volume_ratio(volumes: np.ndarray, period: int = 13) -> np.ndarray:
    sma = pd.Series(volumes).rolling(period, min_periods=period).mean().to_numpy(float)
    out = np.full(len(volumes), np.nan)
    for i in range(len(volumes)):
        if np.isfinite(sma[i]) and sma[i] > 0:
            out[i] = float(volumes[i] / sma[i])
    return out


def assign_quantile_buckets(values: np.ndarray, mask: np.ndarray, n_buckets: int) -> np.ndarray:
    """Assign 0..n_buckets-1 using sample-local quantiles; -1 if invalid."""
    out = np.full(len(values), -1, dtype=int)
    idx = np.where(mask & np.isfinite(values))[0]
    if len(idx) == 0:
        return out
    qs = np.quantile(values[idx], np.linspace(0, 1, n_buckets + 1)[1:-1])
    for i in idx:
        b = int(np.searchsorted(qs, values[i], side="right"))
        out[i] = min(max(b, 0), n_buckets - 1)
    return out


def leverage_labels(swept: set[int]) -> tuple[str, dict[str, bool]]:
    flags = {
        "includes_25x": 25 in swept,
        "includes_50x": 50 in swept,
        "includes_100x": 100 in swept,
        "mixed_leverages": len(swept) >= 2,
        "only_25x": swept == {25},
        "only_50x": swept == {50},
        "only_100x": swept == {100},
    }
    if flags["only_25x"]:
        only = "only_25x"
    elif flags["only_50x"]:
        only = "only_50x"
    elif flags["only_100x"]:
        only = "only_100x"
    elif flags["mixed_leverages"]:
        only = "mixed_leverages"
    else:
        only = "other"
    return only, flags


def path_metrics_for_direction(
    *,
    direction: str,
    entry_index: int,
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: pd.Series,
    horizon: int,
) -> dict[str, Any] | None:
    """Unified event/control metric function. Short uses path audit; long is mirrored OHLC."""
    if direction == "short":
        p = analyze_short_path(
            entry_index=entry_index,
            entry_price=entry_price,
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=timestamps,
            horizon=horizon,
        )
    elif direction == "long":
        # Mirror: treat rising prices as adverse for short logic by swapping high/low signs around entry
        # Equivalent: flip highs/lows relative to entry → use inverted series
        inv_highs = 2.0 * entry_price - lows
        inv_lows = 2.0 * entry_price - highs
        inv_closes = 2.0 * entry_price - closes
        p = analyze_short_path(
            entry_index=entry_index,
            entry_price=entry_price,
            highs=inv_highs,
            lows=inv_lows,
            closes=inv_closes,
            timestamps=timestamps,
            horizon=horizon,
        )
    else:
        raise ValueError(f"unknown direction={direction!r}")
    if p is None or not p["complete_horizon"]:
        return None
    cat = classify_path_category(p)
    return {
        "return_to_close_pct": float(p["close_return_pct"]),
        "MFE_pct": float(p["max_favorable_move_pct"]),
        "MAE_pct": float(p["max_adverse_move_pct"]),
        "peak_drop_pct": float(p["drop_from_peak_pct"]),
        "peak_before_trough": bool(
            p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"]
        ),
        "squeeze_then_drop": cat in {"squeeze_then_drop", "deep_squeeze_then_drop"},
        "breakout": cat == "immediate_breakout",
        "minutes_to_peak": float(p["minutes_to_max_adverse"]),
        "path_category": cat,
    }


def build_winner_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    *,
    cfg: LiquidationLevelConfig,
    n_buckets: int = N_BUCKETS,
    require_horizon: int = PRIMARY_HORIZON,
) -> tuple[list[ValidationEvent], dict[str, Any]]:
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    volumes = data["volume"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)

    atr_pct = compute_atr_pct(highs, lows, closes)
    vol_ratio = compute_volume_ratio(volumes, period=int(cfg.volume_sma_period))
    sample_arr = np.array([assign_sample(i, n) for i in range(n)], dtype=object)

    atr_bucket = np.full(n, -1, dtype=int)
    vol_bucket = np.full(n, -1, dtype=int)
    for sample in ("in_sample", "out_of_sample"):
        mask = sample_arr == sample
        atr_bucket = np.where(mask, assign_quantile_buckets(atr_pct, mask, n_buckets), atr_bucket)
        vol_bucket = np.where(mask, assign_quantile_buckets(vol_ratio, mask, n_buckets), vol_bucket)

    # candle -> swept upper leverages / strength
    by_candle: dict[int, list] = {}
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        if lvl.side != SIDE_UPPER or int(lvl.leverage) not in set(cfg.leverages):
            continue
        by_candle.setdefault(int(lvl.swept_index), []).append(lvl)

    lite = build_lite_upper_events(
        result,
        data,
        allowed_leverages=tuple(cfg.leverages),
        reclaim_window_candles=int(cfg.reclaim_window_candles),
    )

    events: list[ValidationEvent] = []
    for e in lite:
        if e.leverage != 50 or e.exclusive_reclaim_group != "immediate_reclaim":
            continue
        if e.entry_index is None or e.entry_price is None:
            continue
        # require complete primary horizon
        if e.entry_index + require_horizon > n:
            continue
        i = int(e.candle_index)
        lvls = by_candle.get(i, [])
        swept = {int(x.leverage) for x in lvls}
        strengths = [int(x.strength) for x in lvls]
        prices = [float(x.level_price) for x in lvls]
        only, flags = leverage_labels(swept or {50})
        center = float(np.average(prices, weights=strengths)) if prices and sum(strengths) > 0 else (
            float(prices[0]) if prices else None
        )
        events.append(
            ValidationEvent(
                event_id=e.event_id,
                signal_index=i,
                signal_timestamp=pd.Timestamp(e.timestamp),
                entry_index=int(e.entry_index),
                entry_timestamp=pd.Timestamp(ts.iloc[int(e.entry_index)]),
                entry_price=float(e.entry_price),
                side="upper",
                direction="short",
                leverage=50,
                swept_level_count=len(lvls) if lvls else 1,
                swept_total_strength=int(sum(strengths)) if strengths else 1,
                swept_leverages=tuple(sorted(swept)) if swept else (50,),
                cluster_center_price=center,
                cluster_distance_pct=float(cfg.cluster_distance_pct),
                sample=str(e.sample),
                month=str(pd.Timestamp(e.timestamp).strftime("%Y-%m")),
                hour_utc=int(pd.Timestamp(e.timestamp).hour),
                volatility_bucket=int(atr_bucket[i]),
                atr_bucket=int(atr_bucket[i]),
                volume_bucket=int(vol_bucket[i]),
                atr_pct=float(atr_pct[i]) if np.isfinite(atr_pct[i]) else float("nan"),
                volume_ratio=float(vol_ratio[i]) if np.isfinite(vol_ratio[i]) else float("nan"),
                leverage_group_only=only,
                leverage_group_flags=flags,
            )
        )

    counts = {
        "full": len(events),
        "in_sample": sum(1 for x in events if x.sample == "in_sample"),
        "out_of_sample": sum(1 for x in events if x.sample == "out_of_sample"),
    }
    meta = {
        "atr_pct": atr_pct,
        "vol_ratio": vol_ratio,
        "atr_bucket": atr_bucket,
        "vol_bucket": vol_bucket,
        "sample_arr": sample_arr,
        "counts": counts,
        "n_candles": n,
    }
    return events, meta


def validate_event_counts(counts: Mapping[str, int], cfg: ControlValidationConfig) -> None:
    ok = (
        counts.get("full") == cfg.expected_full
        and counts.get("in_sample") == cfg.expected_is
        and counts.get("out_of_sample") == cfg.expected_oos
    )
    if not ok:
        raise RuntimeError(
            "event count mismatch vs winner "
            f"{cfg.winner_config_id}: got full={counts.get('full')} is={counts.get('in_sample')} "
            f"oos={counts.get('out_of_sample')}; expected "
            f"full={cfg.expected_full} is={cfg.expected_is} oos={cfg.expected_oos}. Aborting."
        )


@dataclass
class ControlPool:
    """Indexed candidate control signal indices (not entries)."""

    indices: np.ndarray  # signal candle indices
    sample: np.ndarray
    month: np.ndarray
    hour: np.ndarray
    atr_bucket: np.ndarray
    vol_bucket: np.ndarray
    entry_index: np.ndarray
    entry_price: np.ndarray


def build_control_pool(
    ohlcv: pd.DataFrame,
    *,
    event_signal_indices: set[int],
    atr_bucket: np.ndarray,
    vol_bucket: np.ndarray,
    sample_arr: np.ndarray,
    max_horizon: int,
) -> ControlPool:
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    idxs = []
    for i in range(n):
        if i in event_signal_indices:
            continue
        entry = i + 1
        if entry >= n or entry + max_horizon > n:
            continue
        if atr_bucket[i] < 0 or vol_bucket[i] < 0:
            continue
        idxs.append(i)
    arr = np.asarray(idxs, dtype=int)
    return ControlPool(
        indices=arr,
        sample=np.asarray([sample_arr[i] for i in arr], dtype=object),
        month=np.asarray([str(ts.iloc[i].strftime("%Y-%m")) for i in arr], dtype=object),
        hour=np.asarray([int(ts.iloc[i].hour) for i in arr], dtype=int),
        atr_bucket=np.asarray([int(atr_bucket[i]) for i in arr], dtype=int),
        vol_bucket=np.asarray([int(vol_bucket[i]) for i in arr], dtype=int),
        entry_index=arr + 1,
        entry_price=np.asarray([float(opens[i + 1]) for i in arr], dtype=float),
    )


def match_control_for_event(
    event: ValidationEvent,
    pool: ControlPool,
    rng: np.random.Generator,
    *,
    mode: str = "medium",
    min_distance: int = MIN_DISTANCE_CANDLES,
    hour_tol_strict: int = 2,
    hour_tol_medium: int = 4,
    n_buckets: int = N_BUCKETS,
) -> tuple[int | None, str]:
    """Return (pool_row_index, match_level). Pool row index into ControlPool arrays."""
    base = (
        (pool.sample == event.sample)
        & (pool.month == event.month)
        & (np.abs(pool.indices - event.signal_index) >= int(min_distance))
    )

    def pick(mask: np.ndarray, level: str) -> tuple[int | None, str]:
        cand = np.where(mask)[0]
        if len(cand) == 0:
            return None, level
        return int(rng.choice(cand)), level

    if mode == "loose":
        return pick(base, "loose_month_sample")

    # cyclic hour distance via min(|dh|, 24-|dh|)
    dh = np.abs(pool.hour.astype(int) - int(event.hour_utc)) % 24
    hour_dist = np.minimum(dh, 24 - dh)
    atr_eq = pool.atr_bucket == event.atr_bucket
    vol_eq = pool.vol_bucket == event.volume_bucket

    m0 = base & (hour_dist <= hour_tol_strict) & atr_eq & vol_eq
    hit, lvl = pick(m0, "exact_month_hour_atr_vol")
    if hit is not None:
        return hit, lvl

    if mode == "strict":
        return None, "no_match_strict"

    mA = base & (hour_dist <= hour_tol_medium) & atr_eq & vol_eq
    hit, lvl = pick(mA, "relaxed_hour_pm4")
    if hit is not None:
        return hit, lvl

    atr_nb = np.abs(pool.atr_bucket - event.atr_bucket) <= 1
    mB = base & (hour_dist <= hour_tol_medium) & atr_nb & vol_eq
    hit, lvl = pick(mB, "neighbor_atr")
    if hit is not None:
        return hit, lvl

    vol_nb = np.abs(pool.vol_bucket - event.volume_bucket) <= 1
    mC = base & (hour_dist <= hour_tol_medium) & atr_nb & vol_nb
    hit, lvl = pick(mC, "neighbor_volume")
    if hit is not None:
        return hit, lvl

    return None, "no_match"


def aggregate_metric_dicts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "mean_return": None,
            "median_return": None,
            "mean_mfe": None,
            "mean_mae": None,
            "median_peak_drop": None,
            "peak_before_trough_rate": None,
            "squeeze_then_drop_rate": None,
            "breakout_rate": None,
            "median_minutes_to_peak": None,
        }
    rets = [r["return_to_close_pct"] for r in rows]
    mfe = [r["MFE_pct"] for r in rows]
    mae = [r["MAE_pct"] for r in rows]
    drop = [r["peak_drop_pct"] for r in rows]
    n = len(rows)
    return {
        "n": n,
        "mean_return": float(np.mean(rets)),
        "median_return": float(np.median(rets)),
        "mean_mfe": float(np.mean(mfe)),
        "mean_mae": float(np.mean(mae)),
        "median_peak_drop": float(np.median(drop)),
        "peak_before_trough_rate": 100.0 * sum(1 for r in rows if r["peak_before_trough"]) / n,
        "squeeze_then_drop_rate": 100.0 * sum(1 for r in rows if r["squeeze_then_drop"]) / n,
        "breakout_rate": 100.0 * sum(1 for r in rows if r["breakout"]) / n,
        "median_minutes_to_peak": float(np.median([r["minutes_to_peak"] for r in rows])),
    }


def empirical_two_sided_p(event_stat: float, control_stats: Sequence[float]) -> float:
    if not control_stats:
        return float("nan")
    arr = np.asarray(control_stats, float)
    # two-sided: fraction of |c - mean_c| >= |event - mean_c| using control center
    center = float(np.mean(arr))
    thr = abs(float(event_stat) - center)
    return float(np.mean(np.abs(arr - center) >= thr - 1e-15))


def percentile_of_event(event_stat: float, control_stats: Sequence[float]) -> float:
    if not control_stats:
        return float("nan")
    arr = np.asarray(control_stats, float)
    return float(100.0 * np.mean(arr <= float(event_stat)))


def decide_oos_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Conservative decision from OOS horizon comparisons + sensitivity."""
    reasons: list[str] = []
    oos_n = int(payload.get("oos_n") or 0)
    if oos_n < 200:
        reasons.append("oos_n_too_small")
    horizons = payload.get("oos_horizon_summary") or []
    better = 0
    worse = 0
    for h in horizons:
        d = h.get("delta_median_return")
        if d is None:
            continue
        if d > 0:
            better += 1
        elif d < 0:
            worse += 1
    if better == 0 and worse == 0:
        reasons.append("no_horizon_deltas")
    frac = payload.get("fraction_controls_better_than_event_median_return")
    if frac is not None and float(frac) > 0.55:
        reasons.append("majority_controls_better_than_event")
    if frac is not None and float(frac) < 0.45:
        pass  # favorable
    seed_flip = bool(payload.get("seed_sensitivity_unstable"))
    match_flip = bool(payload.get("matching_sensitivity_unstable"))
    month_dom = bool(payload.get("single_month_dominates"))
    if seed_flip:
        reasons.append("seed_sensitivity_unstable")
    if match_flip:
        reasons.append("matching_sensitivity_unstable")
    if month_dom:
        reasons.append("single_month_dominates")

    if "oos_n_too_small" in reasons or "no_horizon_deltas" in reasons:
        status = "inconclusive"
    elif "majority_controls_better_than_event" in reasons and worse >= better:
        status = "worse_than_control"
    elif better >= max(3, worse + 2) and not seed_flip and not match_flip and not month_dom and (
        frac is not None and float(frac) <= 0.40
    ):
        status = "confirmed_better_than_matched_control"
    elif better > worse and not (seed_flip and match_flip):
        status = "partially_confirmed"
    elif abs(better - worse) <= 1 and (frac is None or 0.4 <= float(frac) <= 0.6):
        status = "indistinguishable_from_control"
    elif worse > better:
        status = "worse_than_control"
    else:
        status = "inconclusive"

    return {
        "status": status,
        "reasons": reasons,
        "oos_n": oos_n,
        "horizons_event_better": better,
        "horizons_control_better": worse,
        "fraction_controls_better_than_event_median_return": frac,
        "note": (
            "Empirical matched-control comparison only. "
            "Not a formal significance claim. No trading-edge claim."
        ),
        "integration_recommended": status == "confirmed_better_than_matched_control",
    }


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def _load_completed_runs(path: Path) -> set[int]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(int(json.loads(line)["run_id"]))
        except Exception:
            continue
    return done


def run_control_validation(
    ohlcv: pd.DataFrame,
    *,
    output_dir: Path,
    cfg: ControlValidationConfig | None = None,
    level_config: LiquidationLevelConfig | None = None,
    skip_seed_sensitivity: bool = False,
    skip_matching_sensitivity: bool = False,
    max_events: int | None = None,
    resume: bool = False,
    matching_mode: str = "medium",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    cfg = cfg or ControlValidationConfig()
    level_cfg = level_config or frozen_winner_config()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if config_hash(level_cfg) != cfg.winner_config_id:
        raise RuntimeError(
            f"frozen config hash {config_hash(level_cfg)} != expected {cfg.winner_config_id}"
        )

    from research.liquidation_level.liquidation_config import config_to_canonical_dict

    _atomic_write_json(
        out / "config.json",
        {
            "winner_config_id": cfg.winner_config_id,
            "level_config": config_to_canonical_dict(level_cfg),
            "control_runs": cfg.control_runs,
            "random_seed": cfg.random_seed,
            "horizons": list(cfg.horizons),
            "matching_mode_default": matching_mode,
            "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations.",
        },
    )

    print("replaying frozen winner levels...", flush=True)
    data = normalize_ohlcv_dataframe(ohlcv)
    replay = replay_liquidation_levels(data, level_cfg)
    events, meta = build_winner_events(replay, data, cfg=level_cfg, n_buckets=cfg.n_buckets)
    validate_event_counts(meta["counts"], cfg)
    _atomic_write_json(out / "event_validation.json", {"ok": True, **meta["counts"], "config_id": cfg.winner_config_id})

    if max_events is not None and max_events > 0:
        rng_s = np.random.default_rng(cfg.random_seed)
        if max_events < len(events):
            pick = sorted(rng_s.choice(len(events), size=int(max_events), replace=False).tolist())
            events = [events[i] for i in pick]

    # export events.csv
    ev_rows = []
    for e in events:
        row = {
            "event_id": e.event_id,
            "signal_index": e.signal_index,
            "signal_timestamp": str(e.signal_timestamp),
            "entry_index": e.entry_index,
            "entry_timestamp": str(e.entry_timestamp),
            "entry_price": e.entry_price,
            "side": e.side,
            "direction": e.direction,
            "swept_level_count": e.swept_level_count,
            "swept_total_strength": e.swept_total_strength,
            "swept_leverages": ",".join(str(x) for x in e.swept_leverages),
            "cluster_center_price": e.cluster_center_price,
            "cluster_distance_pct": e.cluster_distance_pct,
            "sample": e.sample,
            "month": e.month,
            "hour_utc": e.hour_utc,
            "volatility_bucket": e.volatility_bucket,
            "atr_bucket": e.atr_bucket,
            "volume_bucket": e.volume_bucket,
            "leverage_group_only": e.leverage_group_only,
            **{f"flag_{k}": v for k, v in e.leverage_group_flags.items()},
        }
        ev_rows.append(row)
    pd.DataFrame(ev_rows).to_csv(out / "events.csv", index=False)

    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    opens = data["open"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    max_h = max(cfg.horizons)

    # Precompute event metrics by horizon
    print("precomputing event path metrics...", flush=True)
    event_metrics: dict[int, list[dict[str, Any] | None]] = {h: [] for h in cfg.horizons}
    for e in events:
        for h in cfg.horizons:
            m = path_metrics_for_direction(
                direction=e.direction,
                entry_index=e.entry_index,
                entry_price=e.entry_price,
                highs=highs,
                lows=lows,
                closes=closes,
                timestamps=ts,
                horizon=int(h),
            )
            event_metrics[h].append(m)

    pool = build_control_pool(
        data,
        event_signal_indices={e.signal_index for e in events},
        atr_bucket=meta["atr_bucket"],
        vol_bucket=meta["vol_bucket"],
        sample_arr=meta["sample_arr"],
        max_horizon=max_h,
    )
    print(f"control pool size={len(pool.indices)}", flush=True)

    # Cache control metrics by entry_index to avoid recomputation across runs
    ctrl_metric_cache: dict[tuple[int, int], dict[str, Any]] = {}

    def ctrl_metrics(entry_i: int, entry_px: float, horizon: int, direction: str) -> dict[str, Any] | None:
        key = (entry_i, horizon)
        if key in ctrl_metric_cache:
            return ctrl_metric_cache[key]
        m = path_metrics_for_direction(
            direction=direction,
            entry_index=entry_i,
            entry_price=entry_px,
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=ts,
            horizon=horizon,
        )
        if m is not None:
            ctrl_metric_cache[key] = m
            if len(ctrl_metric_cache) > 200_000:
                # bound memory: drop arbitrarily half
                for k in list(ctrl_metric_cache.keys())[:100_000]:
                    del ctrl_metric_cache[k]
        return m

    runs_path = out / "control_runs.jsonl"
    completed_runs = _load_completed_runs(runs_path) if resume else set()
    if not resume and runs_path.exists():
        runs_path.unlink()

    match_rows: list[dict[str, Any]] = []
    # collect per-run aggregate for OOS + full for primary horizon 24 and all horizons
    run_summaries: list[dict[str, Any]] = []

    main_horizon_for_decision = 24 if 24 in cfg.horizons else cfg.horizons[len(cfg.horizons) // 2]

    def run_one(run_id: int, seed: int, mode: str) -> dict[str, Any]:
        rng = np.random.default_rng(int(seed) + int(run_id) * 10007)
        matched = 0
        match_levels: dict[str, int] = {}
        # per sample/horizon control metric lists
        buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
        oos_ctrl_for_main: list[dict[str, Any]] = []
        oos_evt_for_main: list[dict[str, Any]] = []

        local_matches = []
        for ei, e in enumerate(events):
            pool_i, level = match_control_for_event(
                e,
                pool,
                rng,
                mode=mode,
                min_distance=cfg.min_distance_candles,
                hour_tol_strict=cfg.hour_tolerance_strict,
                hour_tol_medium=cfg.hour_tolerance_medium,
                n_buckets=cfg.n_buckets,
            )
            match_levels[level] = match_levels.get(level, 0) + 1
            if pool_i is None:
                continue
            matched += 1
            entry_i = int(pool.entry_index[pool_i])
            entry_px = float(pool.entry_price[pool_i])
            if run_id == 0 and len(local_matches) < 5000:
                local_matches.append(
                    {
                        "run_id": run_id,
                        "event_id": e.event_id,
                        "event_signal_index": e.signal_index,
                        "control_signal_index": int(pool.indices[pool_i]),
                        "control_entry_index": entry_i,
                        "match_level": level,
                        "sample": e.sample,
                        "month": e.month,
                    }
                )
            for h in cfg.horizons:
                cm = ctrl_metrics(entry_i, entry_px, int(h), e.direction)
                em = event_metrics[h][ei]
                if cm is None or em is None:
                    continue
                key = (e.sample, int(h))
                buckets.setdefault(key, []).append(cm)
                if e.sample == "out_of_sample" and int(h) == int(main_horizon_for_decision):
                    oos_ctrl_for_main.append(cm)
                    oos_evt_for_main.append(em)

        # event aggregates once per sample/horizon outside — use fixed event list
        summary = {
            "run_id": run_id,
            "seed": seed,
            "matching_mode": mode,
            "matched_events": matched,
            "match_rate_pct": 100.0 * matched / max(1, len(events)),
            "match_levels": match_levels,
        }
        # store control mean return OOS main horizon
        if oos_ctrl_for_main:
            summary["oos_control_mean_return_h" + str(main_horizon_for_decision)] = float(
                np.mean([x["return_to_close_pct"] for x in oos_ctrl_for_main])
            )
            summary["oos_control_median_peak_drop_h" + str(main_horizon_for_decision)] = float(
                np.median([x["peak_drop_pct"] for x in oos_ctrl_for_main])
            )
            summary["oos_control_peak_before_trough_rate_h" + str(main_horizon_for_decision)] = (
                100.0 * sum(1 for x in oos_ctrl_for_main if x["peak_before_trough"]) / len(oos_ctrl_for_main)
            )
        else:
            summary["oos_control_mean_return_h" + str(main_horizon_for_decision)] = None
        summary["_buckets"] = {f"{s}|{h}": aggregate_metric_dicts(v) for (s, h), v in buckets.items()}
        if run_id == 0:
            summary["_match_examples"] = local_matches
        return summary

    print(f"starting control runs={cfg.control_runs} mode={matching_mode}...", flush=True)
    t_run0 = time.perf_counter()
    for run_id in range(cfg.control_runs):
        if run_id in completed_runs:
            continue
        try:
            s = run_one(run_id, cfg.random_seed, matching_mode)
            if run_id == 0 and s.get("_match_examples"):
                match_rows.extend(s.pop("_match_examples"))
            slim = {k: v for k, v in s.items() if not k.startswith("_")}
            # keep bucket aggregates for later in slim under nested
            slim["buckets"] = s.get("_buckets")
            _append_jsonl(runs_path, slim)
            run_summaries.append(slim)
            completed_runs.add(run_id)
        except Exception as exc:
            _append_jsonl(
                out / "failed_runs.jsonl",
                {"run_id": run_id, "error": str(exc), "traceback": traceback.format_exc()},
            )
        if cfg.progress_every and (run_id + 1) % cfg.progress_every == 0:
            done = len(completed_runs)
            elapsed = time.perf_counter() - t_run0
            rate = done / max(elapsed, 1e-6)
            remaining = cfg.control_runs - done
            eta = remaining / max(rate, 1e-9)
            print(
                f"progress runs={done}/{cfg.control_runs} cache_ctrl={len(ctrl_metric_cache)} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                flush=True,
            )

    # reload all runs if resume
    if resume and runs_path.exists():
        run_summaries = []
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                run_summaries.append(json.loads(line))

    if match_rows:
        pd.DataFrame(match_rows).to_csv(out / "control_matches.csv", index=False)
    else:
        # synthesize few matches from first stored run if needed
        pd.DataFrame([]).to_csv(out / "control_matches.csv", index=False)

    # Event side aggregates
    def event_agg(sample: str | None, horizon: int, predicate=None) -> dict[str, Any]:
        rows = []
        for i, e in enumerate(events):
            if sample is not None and e.sample != sample:
                continue
            if predicate is not None and not predicate(e):
                continue
            m = event_metrics[horizon][i]
            if m is None:
                continue
            rows.append(m)
        return aggregate_metric_dicts(rows)

    def control_dist(sample: str, horizon: int, key: str = "mean_return") -> list[float]:
        vals = []
        bkey = f"{sample}|{horizon}"
        for s in run_summaries:
            b = (s.get("buckets") or {}).get(bkey)
            if not b:
                continue
            v = b.get(key)
            if v is not None:
                vals.append(float(v))
        return vals

    # horizon comparison
    hz_rows = []
    for sample in ("full", "in_sample", "out_of_sample"):
        samp = None if sample == "full" else sample
        for h in cfg.horizons:
            ea = event_agg(samp, int(h))
            # For full, merge by concatenating IS/OOS control means? Use both samples average of run means
            if sample == "full":
                cmeans = control_dist("in_sample", int(h)) + control_dist("out_of_sample", int(h))
                # better: recompute from run buckets weighted — use average of IS+OOS mean returns per run
                cmeans = []
                for s in run_summaries:
                    parts = []
                    for sm in ("in_sample", "out_of_sample"):
                        b = (s.get("buckets") or {}).get(f"{sm}|{h}")
                        if b and b.get("mean_return") is not None and b.get("n"):
                            parts.append((b["mean_return"], b["n"]))
                    if parts:
                        num = sum(m * n for m, n in parts)
                        den = sum(n for _, n in parts)
                        cmeans.append(num / den)
                cmed_drop = []
                cpbt = []
                for s in run_summaries:
                    parts_d, parts_p, ns = [], [], []
                    for sm in ("in_sample", "out_of_sample"):
                        b = (s.get("buckets") or {}).get(f"{sm}|{h}")
                        if b and b.get("n"):
                            parts_d.append((b.get("median_peak_drop"), b["n"]))
                            parts_p.append((b.get("peak_before_trough_rate"), b["n"]))
                            ns.append(b["n"])
                    if parts_d and all(x[0] is not None for x in parts_d):
                        cmed_drop.append(sum(m * n for m, n in parts_d) / sum(ns))
                    if parts_p and all(x[0] is not None for x in parts_p):
                        cpbt.append(sum(m * n for m, n in parts_p) / sum(ns))
            else:
                cmeans = control_dist(sample, int(h), "mean_return")
                cmed_drop = control_dist(sample, int(h), "median_peak_drop")
                cpbt = control_dist(sample, int(h), "peak_before_trough_rate")

            c_mean = float(np.mean(cmeans)) if cmeans else None
            c_std = float(np.std(cmeans)) if cmeans else None
            row = {
                "sample": sample,
                "horizon": int(h),
                "event_n": ea["n"],
                "control_n_mean": float(np.mean([
                    ((s.get("buckets") or {}).get(f"{sample if sample!='full' else 'in_sample'}|{h}") or {}).get("n") or 0
                    for s in run_summaries
                ])) if run_summaries else None,
                "event_mean_return": ea["mean_return"],
                "control_mean_return": c_mean,
                "event_median_return": ea["median_return"],
                "control_median_return": float(np.median(control_dist(sample if sample != "full" else "out_of_sample", int(h), "median_return"))) if sample != "full" else ea["median_return"],
                "delta_mean_return": None if ea["mean_return"] is None or c_mean is None else ea["mean_return"] - c_mean,
                "delta_median_return": None,
                "event_mean_mfe": ea["mean_mfe"],
                "control_mean_mfe": float(np.mean(control_dist(sample if sample != "full" else "out_of_sample", int(h), "mean_mfe"))) if True else None,
                "delta_mean_mfe": None,
                "event_mean_mae": ea["mean_mae"],
                "control_mean_mae": float(np.mean(control_dist(sample if sample != "full" else "out_of_sample", int(h), "mean_mae"))) if True else None,
                "delta_mean_mae": None,
                "event_median_peak_drop": ea["median_peak_drop"],
                "control_median_peak_drop": float(np.mean(cmed_drop)) if cmed_drop else None,
                "delta_peak_drop": None,
                "event_peak_before_trough_rate": ea["peak_before_trough_rate"],
                "control_peak_before_trough_rate": float(np.mean(cpbt)) if cpbt else None,
                "delta_peak_before_trough_rate": None,
                "event_squeeze_then_drop_rate": ea["squeeze_then_drop_rate"],
                "control_squeeze_then_drop_rate": float(np.mean(control_dist(sample if sample != "full" else "out_of_sample", int(h), "squeeze_then_drop_rate"))) if True else None,
                "delta_squeeze_then_drop_rate": None,
                "event_breakout_rate": ea["breakout_rate"],
                "control_breakout_rate": float(np.mean(control_dist(sample if sample != "full" else "out_of_sample", int(h), "breakout_rate"))) if True else None,
                "delta_breakout_rate": None,
                "control_mean_distribution_mean": c_mean,
                "control_mean_distribution_std": c_std,
                "percentile_of_event_result": percentile_of_event(ea["mean_return"], cmeans) if ea["mean_return"] is not None else None,
                "fraction_controls_better_than_event": (
                    None
                    if ea["mean_return"] is None or not cmeans
                    else float(np.mean(np.asarray(cmeans) > ea["mean_return"]))
                ),
                "empirical_two_sided_p_value": (
                    empirical_two_sided_p(ea["mean_return"], cmeans) if ea["mean_return"] is not None else None
                ),
            }
            # fill deltas
            for a, b, d in (
                ("event_median_return", "control_median_return", "delta_median_return"),
                ("event_mean_mfe", "control_mean_mfe", "delta_mean_mfe"),
                ("event_mean_mae", "control_mean_mae", "delta_mean_mae"),
                ("event_median_peak_drop", "control_median_peak_drop", "delta_peak_drop"),
                ("event_peak_before_trough_rate", "control_peak_before_trough_rate", "delta_peak_before_trough_rate"),
                ("event_squeeze_then_drop_rate", "control_squeeze_then_drop_rate", "delta_squeeze_then_drop_rate"),
                ("event_breakout_rate", "control_breakout_rate", "delta_breakout_rate"),
            ):
                if row[a] is not None and row[b] is not None:
                    row[d] = float(row[a]) - float(row[b])
            hz_rows.append(row)

    pd.DataFrame(hz_rows).to_csv(out / "horizon_comparison.csv", index=False)
    pd.DataFrame([{k: v for k, v in s.items() if k != "buckets"} for s in run_summaries]).to_csv(
        out / "control_run_summary.csv", index=False
    )

    # leverage comparison on OOS main horizon using event flags vs same controls? 
    # report event-only path stats by leverage group vs overall control OOS
    lev_rows = []
    h = int(main_horizon_for_decision)
    ctrl_oos_mean = control_dist("out_of_sample", h, "mean_return")
    ctrl_oos_drop = control_dist("out_of_sample", h, "median_peak_drop")
    for flag in (
        "only_25x",
        "only_50x",
        "only_100x",
        "includes_25x",
        "includes_50x",
        "includes_100x",
        "mixed_leverages",
    ):
        for sample in ("full", "in_sample", "out_of_sample"):
            samp = None if sample == "full" else sample

            def pred(e, f=flag):
                return bool(e.leverage_group_flags.get(f))

            ea = event_agg(samp, h, pred)
            cmeans = ctrl_oos_mean if sample != "in_sample" else control_dist("in_sample", h, "mean_return")
            cmean = float(np.mean(cmeans)) if cmeans else None
            lev_rows.append(
                {
                    "leverage_group": flag,
                    "sample": sample,
                    "horizon": h,
                    "event_n": ea["n"],
                    "event_mean_return": ea["mean_return"],
                    "control_mean_return": cmean,
                    "delta_mean_return": None if ea["mean_return"] is None or cmean is None else ea["mean_return"] - cmean,
                    "event_median_peak_drop": ea["median_peak_drop"],
                    "control_median_peak_drop": float(np.mean(ctrl_oos_drop)) if ctrl_oos_drop else None,
                    "event_peak_before_trough_rate": ea["peak_before_trough_rate"],
                    "event_squeeze_then_drop_rate": ea["squeeze_then_drop_rate"],
                    "note": "Primary winner events are upper_50x immediate_reclaim; includes_* reflect co-swept levers on same candle.",
                }
            )
    pd.DataFrame(lev_rows).to_csv(out / "leverage_comparison.csv", index=False)

    # side comparison: primary is upper/short only — still export clear row
    side_rows = []
    for sample in ("full", "in_sample", "out_of_sample"):
        samp = None if sample == "full" else sample
        ea = event_agg(samp, h)
        side_rows.append(
            {
                "side": "upper",
                "direction": "short",
                "sample": sample,
                "horizon": h,
                "event_n": ea["n"],
                "event_mean_return": ea["mean_return"],
                "event_median_peak_drop": ea["median_peak_drop"],
                "event_peak_before_trough_rate": ea["peak_before_trough_rate"],
                "note": "Winner event universe is upper/short only (matches optimizer primary metric).",
            }
        )
    pd.DataFrame(side_rows).to_csv(out / "side_comparison.csv", index=False)

    # monthly OOS
    monthly_rows = []
    months = sorted({e.month for e in events if e.sample == "out_of_sample"})
    for month in months:
        ea = event_agg("out_of_sample", h, lambda e, m=month: e.month == m)
        monthly_rows.append(
            {
                "month": month,
                "sample": "out_of_sample",
                "horizon": h,
                "event_n": ea["n"],
                "event_mean_return": ea["mean_return"],
                "event_median_peak_drop": ea["median_peak_drop"],
                "event_peak_before_trough_rate": ea["peak_before_trough_rate"],
            }
        )
    pd.DataFrame(monthly_rows).to_csv(out / "monthly_comparison.csv", index=False)

    # seed sensitivity (OOS subset for server safety; still multi-seed)
    seed_rows = []
    seed_unstable = False
    if not skip_seed_sensitivity:
        oos_idx = [i for i, e in enumerate(events) if e.sample == "out_of_sample"]
        sens_events = [events[i] for i in oos_idx[: min(500, len(oos_idx))]]
        base_frac = None
        for seed in cfg.seed_sensitivity_seeds:
            ctrl_means = []
            for run_id in range(cfg.seed_sensitivity_runs):
                rng = np.random.default_rng(int(seed) + run_id * 10007)
                rows = []
                for e in sens_events:
                    pool_i, _level = match_control_for_event(
                        e, pool, rng, mode=matching_mode, min_distance=cfg.min_distance_candles
                    )
                    if pool_i is None:
                        continue
                    cm = ctrl_metrics(
                        int(pool.entry_index[pool_i]),
                        float(pool.entry_price[pool_i]),
                        h,
                        e.direction,
                    )
                    if cm:
                        rows.append(cm)
                if rows:
                    ctrl_means.append(float(np.mean([x["return_to_close_pct"] for x in rows])))
            ea = event_agg("out_of_sample", h)
            frac = (
                None
                if ea["mean_return"] is None or not ctrl_means
                else float(np.mean(np.asarray(ctrl_means) > ea["mean_return"]))
            )
            if base_frac is None:
                base_frac = frac
            elif frac is not None and base_frac is not None and abs(frac - base_frac) > 0.15:
                seed_unstable = True
            seed_rows.append(
                {
                    "seed": seed,
                    "runs": cfg.seed_sensitivity_runs,
                    "oos_events_used": len(sens_events),
                    "oos_event_mean_return": ea["mean_return"],
                    "control_mean_return_mean": float(np.mean(ctrl_means)) if ctrl_means else None,
                    "fraction_controls_better_than_event": frac,
                    "matching_mode": matching_mode,
                }
            )
    pd.DataFrame(seed_rows).to_csv(out / "seed_sensitivity.csv", index=False)

    # matching sensitivity
    match_rows_s = []
    match_unstable = False
    if not skip_matching_sensitivity:
        oos_idx = [i for i, e in enumerate(events) if e.sample == "out_of_sample"]
        sens_events = [events[i] for i in oos_idx[: min(500, len(oos_idx))]]
        fracs = []
        for mode in ("strict", "medium", "loose"):
            ctrl_means = []
            rates = []
            for run_id in range(min(100, cfg.seed_sensitivity_runs)):
                rng = np.random.default_rng(cfg.random_seed + run_id * 10007)
                rows = []
                matched = 0
                for e in sens_events:
                    pool_i, _level = match_control_for_event(
                        e, pool, rng, mode=mode, min_distance=cfg.min_distance_candles
                    )
                    if pool_i is None:
                        continue
                    matched += 1
                    cm = ctrl_metrics(
                        int(pool.entry_index[pool_i]),
                        float(pool.entry_price[pool_i]),
                        h,
                        e.direction,
                    )
                    if cm:
                        rows.append(cm)
                rates.append(100.0 * matched / max(1, len(sens_events)))
                if rows:
                    ctrl_means.append(float(np.mean([x["return_to_close_pct"] for x in rows])))
            ea = event_agg("out_of_sample", h)
            frac = (
                None
                if ea["mean_return"] is None or not ctrl_means
                else float(np.mean(np.asarray(ctrl_means) > ea["mean_return"]))
            )
            fracs.append(frac)
            match_rows_s.append(
                {
                    "matching_mode": mode,
                    "runs": min(100, cfg.seed_sensitivity_runs),
                    "oos_events_used": len(sens_events),
                    "mean_match_rate_pct": float(np.mean(rates)) if rates else None,
                    "control_mean_return_mean": float(np.mean(ctrl_means)) if ctrl_means else None,
                    "fraction_controls_better_than_event": frac,
                    "oos_event_mean_return": ea["mean_return"],
                }
            )
        valid = [f for f in fracs if f is not None]
        if len(valid) >= 2 and (max(valid) - min(valid)) > 0.20:
            match_unstable = True
    pd.DataFrame(match_rows_s).to_csv(out / "matching_sensitivity.csv", index=False)

    # sample size sensitivity
    ss_rows = []
    for n_take in (100, 500, len(events)):
        rng = np.random.default_rng(cfg.random_seed)
        if n_take >= len(events):
            subset = list(range(len(events)))
        else:
            subset = sorted(rng.choice(len(events), size=n_take, replace=False).tolist())
        # event mean OOS in subset
        rows = []
        for i in subset:
            e = events[i]
            if e.sample != "out_of_sample":
                continue
            m = event_metrics[h][i]
            if m:
                rows.append(m)
        ea = aggregate_metric_dicts(rows)
        # 50 control runs on subset — approximate by using full-run control means as reference
        cmeans = control_dist("out_of_sample", h, "mean_return")[:50]
        frac = (
            None
            if ea["mean_return"] is None or not cmeans
            else float(np.mean(np.asarray(cmeans) > ea["mean_return"]))
        )
        ss_rows.append(
            {
                "event_sample_size_requested": n_take,
                "oos_events_in_sample": ea["n"],
                "oos_event_mean_return": ea["mean_return"],
                "fraction_controls_better_than_event": frac,
                "note": "100-sample mirrors earlier weak top10 control comparison risk",
            }
        )
    pd.DataFrame(ss_rows).to_csv(out / "sample_size_sensitivity.csv", index=False)

    # OOS decision
    oos_hz = [r for r in hz_rows if r["sample"] == "out_of_sample"]
    oos_main = next((r for r in oos_hz if r["horizon"] == h), oos_hz[0] if oos_hz else {})
    month_ns = [r["event_n"] for r in monthly_rows]
    single_month = bool(month_ns) and (max(month_ns) / max(1, sum(month_ns)) > 0.6)
    decision = decide_oos_status(
        {
            "oos_n": meta["counts"]["out_of_sample"] if max_events is None else sum(1 for e in events if e.sample == "out_of_sample"),
            "oos_horizon_summary": oos_hz,
            "fraction_controls_better_than_event_median_return": oos_main.get("fraction_controls_better_than_event"),
            "seed_sensitivity_unstable": seed_unstable,
            "matching_sensitivity_unstable": match_unstable,
            "single_month_dominates": single_month,
        }
    )
    _atomic_write_json(out / "oos_decision.json", decision)

    match_rate = float(np.mean([s.get("match_rate_pct") or 0 for s in run_summaries])) if run_summaries else None
    summary = {
        "winner_config_id": cfg.winner_config_id,
        "event_counts": meta["counts"],
        "control_runs_completed": len(run_summaries),
        "mean_match_rate_pct": match_rate,
        "oos_decision": decision,
        "oos_main_horizon": h,
        "oos_main_row": oos_main,
        "seed_sensitivity_unstable": seed_unstable,
        "matching_sensitivity_unstable": match_unstable,
        "elapsed_s": time.perf_counter() - t0,
        "disclaimer": "Estimated LuxAlgo levels; empirical controls only; no trading-edge claim.",
    }
    _atomic_write_json(out / "summary.json", summary)
    write_control_readme(out, summary, decision, meta["counts"], match_rate)
    return summary


def write_control_readme(
    out: Path,
    summary: Mapping[str, Any],
    decision: Mapping[str, Any],
    counts: Mapping[str, int],
    match_rate: float | None,
) -> None:
    text = f"""# Control Validation — Winner `{summary.get('winner_config_id')}`

Estimated LuxAlgo-style levels — **not** real exchange liquidations.

## Why this test?

The optimizer’s top10 control comparison only used 100 events vs 100 controls and looked weak.
This audit re-tests the **frozen** winner with many matched control runs on the **full** event set.

## How controls were matched

Same sample (IS/OOS), same calendar month, same short direction, similar UTC hour
(cyclic), same ATR%% and volume-ratio quantile buckets, ≥96 candles away from the event,
and enough forward candles. If needed, matching loosens: ±4h → neighbor ATR → neighbor volume.
Matching never uses future returns.

## Event reproduction

- Full / IS / OOS: **{counts.get('full')} / {counts.get('in_sample')} / {counts.get('out_of_sample')}**
- Expected: 2696 / 1824 / 872
- Mean match rate: **{match_rate}**

## OOS vs matched controls

See `horizon_comparison.csv` and `oos_decision.json`.

Decision status: **{decision.get('status')}**  
Reasons: {decision.get('reasons')}  
Integration recommended: **{decision.get('integration_recommended')}**

## Leverage / side

Primary events are **upper / short / 50x immediate reclaim** (optimizer primary metric).
`leverage_comparison.csv` tags co-swept 25x/100x on the same candle (`includes_*`, `mixed_*`).
Lower/long sweeps are not part of this winner universe.

## Seed / matching sensitivity

See `seed_sensitivity.csv` and `matching_sensitivity.csv`.
Unstable flags feed the conservative decision rules.

## Important

No trading-edge claim if results are unstable, weak, or worse than controls.
No scanner/bot integration from this audit.
"""
    (out / "README_results.md").write_text(text, encoding="utf-8")
