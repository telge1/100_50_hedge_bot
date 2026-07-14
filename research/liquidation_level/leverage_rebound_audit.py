"""Leverage-specific rebound audit after causal LuxAlgo liquidation-level sweeps.

Estimated levels only — not real exchange liquidations.
Measurement starts at the open of the candle after the sweep (no same-bar trade).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_backtest import assign_sample, in_sample_cut
from research.liquidation_level.liquidation_features import candle_geometry, sweep_depth_pct
from research.liquidation_level.liquidation_levels import (
    SIDE_LOWER,
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevel,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
)

EPS = 1e-12
DEFAULT_HORIZONS = (1, 2, 3, 6, 12, 24)
DEFAULT_THRESHOLDS = (0.10, 0.20, 0.25, 0.30, 0.50, 0.75, 1.00)
DEFAULT_CASCADE_WINDOWS = (1, 3, 6, 12)
LEVERAGES = (100, 50, 25)
ROUNDTRIP_COST_PCT = 0.12


@dataclass(frozen=True)
class ReboundAuditConfig:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    rebound_thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS
    cascade_windows: tuple[int, ...] = DEFAULT_CASCADE_WINDOWS
    bootstrap_resamples: int = 1000
    seed: int = 42
    in_sample_fraction: float = 0.70
    roundtrip_cost_pct: float = ROUNDTRIP_COST_PCT


@dataclass
class ReboundLevelEvent:
    event_id: str
    timestamp: pd.Timestamp
    candle_index: int
    side: str
    expected_rebound_direction: str
    leverage: int
    level_id: int
    level_price: float
    level_strength: int
    level_age: int
    source_reference_price: float
    event_open: float
    event_high: float
    event_low: float
    event_close: float
    event_volume: float
    swept_distance_pct: float
    close_relative_to_level_pct: float
    candle_return_pct: float
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    entry_index: int | None
    entry_price: float | None
    sample: str
    rejection: bool
    breakthrough: bool
    rejection_wick_gt_body: bool
    close_in_favorable_third: bool
    age_bucket: str
    reclaim_class: str


@dataclass
class LeverageCombinationEvent:
    event_id: str
    timestamp: pd.Timestamp
    candle_index: int
    side: str
    expected_rebound_direction: str
    leverage_combination: str
    deepest_swept_leverage: int
    swept_level_count: int
    swept_total_strength: int
    swept_100x_count: int
    swept_50x_count: int
    swept_25x_count: int
    entry_index: int | None
    entry_price: float | None
    sample: str
    level_ids: tuple[int, ...]
    event_open: float
    event_high: float
    event_low: float
    event_close: float
    rejection: bool
    breakthrough: bool
    reclaim_class: str


@dataclass
class CascadeEvent:
    event_id: str
    side: str
    cascade_type: str
    window: int
    start_index: int
    end_index: int
    step_indices: tuple[int, ...]
    entry_index: int | None
    entry_price: float | None
    sample: str
    expected_rebound_direction: str


def age_bucket(age: int) -> str:
    if age <= 6:
        return "0_6"
    if age <= 24:
        return "7_24"
    if age <= 96:
        return "25_96"
    return "gt_96"


def leverage_combo_label(levers: set[int]) -> str:
    parts = []
    for lev in (100, 50, 25):
        if lev in levers:
            parts.append(f"{lev}x")
    if not parts:
        return "none"
    if len(parts) == 1:
        return f"{parts[0]}_only"
    return "_".join(parts)


def deepest_leverage(levers: Iterable[int]) -> int | None:
    """25x is deepest, then 50x, then 100x."""
    s = set(int(x) for x in levers)
    for lev in (25, 50, 100):
        if lev in s:
            return lev
    return None


def close_relative_to_level_pct(side: str, close: float, level: float) -> float:
    if level == 0:
        return 0.0
    if side == SIDE_LOWER:
        return (float(close) - float(level)) / float(level) * 100.0
    return (float(level) - float(close)) / float(level) * 100.0


def classify_reclaim(
    *,
    side: str,
    level_price: float,
    event_close: float,
    closes: np.ndarray,
    entry_index: int | None,
) -> str:
    """Classify reclaim of the swept level after the sweep candle."""
    lvl = float(level_price)
    if side == SIDE_LOWER:
        imm = float(event_close) > lvl
        if entry_index is None or entry_index >= len(closes):
            return "immediate_reclaim" if imm else "no_reclaim"
        next_ok = float(closes[entry_index]) > lvl
        within3 = any(float(closes[i]) > lvl for i in range(entry_index, min(entry_index + 3, len(closes))))
        within6 = any(float(closes[i]) > lvl for i in range(entry_index, min(entry_index + 6, len(closes))))
    else:
        imm = float(event_close) < lvl
        if entry_index is None or entry_index >= len(closes):
            return "immediate_reclaim" if imm else "no_reclaim"
        next_ok = float(closes[entry_index]) < lvl
        within3 = any(float(closes[i]) < lvl for i in range(entry_index, min(entry_index + 3, len(closes))))
        within6 = any(float(closes[i]) < lvl for i in range(entry_index, min(entry_index + 6, len(closes))))

    if imm:
        return "immediate_reclaim"
    if next_ok:
        return "next_candle_reclaim"
    if within3:
        return "reclaim_within_3"
    if within6:
        return "reclaim_within_6"
    return "no_reclaim"


def measure_path_metrics(
    *,
    side: str,
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    horizon: int,
) -> dict[str, Any]:
    """MFE/MAE and first-touch times from next-open reference."""
    if entry_price <= 0 or horizon < 1 or len(highs) < horizon:
        return {}
    h = highs[:horizon]
    l = lows[:horizon]
    if side == SIDE_LOWER:
        # long rebound
        fav = (h / entry_price - 1.0) * 100.0
        adv = (1.0 - l / entry_price) * 100.0
    else:
        fav = (1.0 - l / entry_price) * 100.0
        adv = (h / entry_price - 1.0) * 100.0
    mfe = float(np.max(fav))
    mae = float(np.max(adv))
    return {
        "mfe_pct": mfe,
        "mae_pct": mae,
        "fav_path": fav,
        "adv_path": adv,
    }


def first_touch_bar(path: np.ndarray, threshold: float) -> int | None:
    hits = np.where(path >= float(threshold))[0]
    if len(hits) == 0:
        return None
    return int(hits[0]) + 1  # 1-based bars held


def rebound_before_adverse(
    fav_path: np.ndarray,
    adv_path: np.ndarray,
    threshold: float,
    adverse_thr: float | None = None,
) -> bool:
    """True if rebound threshold is touched before adverse of equal (or given) size."""
    adv_need = float(threshold if adverse_thr is None else adverse_thr)
    t_fav = first_touch_bar(fav_path, threshold)
    t_adv = first_touch_bar(adv_path, adv_need)
    if t_fav is None:
        return False
    if t_adv is None:
        return True
    return t_fav <= t_adv


def build_rebound_level_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    config: ReboundAuditConfig | None = None,
) -> list[ReboundLevelEvent]:
    cfg = config or ReboundAuditConfig()
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    volumes = data["volume"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)

    events: list[ReboundLevelEvent] = []
    seq = 0
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        if int(lvl.leverage) not in LEVERAGES:
            continue
        i = int(lvl.swept_index)
        if i < 0 or i >= n:
            continue
        entry_index = i + 1
        if entry_index >= n:
            # end of data — exclude (no next open)
            continue
        geo = candle_geometry(opens[i], highs[i], lows[i], closes[i])
        side = str(lvl.side)
        direction = "long" if side == SIDE_LOWER else "short"
        rejection = (side == SIDE_LOWER and closes[i] > lvl.level_price) or (
            side == SIDE_UPPER and closes[i] < lvl.level_price
        )
        breakthrough = (side == SIDE_LOWER and closes[i] < lvl.level_price) or (
            side == SIDE_UPPER and closes[i] > lvl.level_price
        )
        wick_gt = (
            (side == SIDE_LOWER and geo["lower_wick_pct"] > geo["sweep_body_pct"])
            or (side == SIDE_UPPER and geo["upper_wick_pct"] > geo["sweep_body_pct"])
        )
        clv = geo["close_location_value"]
        fav_third = (side == SIDE_LOWER and clv >= 2.0 / 3.0) or (side == SIDE_UPPER and clv <= 1.0 / 3.0)
        age = int(lvl.age_at_sweep if lvl.age_at_sweep is not None else i - int(lvl.created_index))
        reclaim = classify_reclaim(
            side=side,
            level_price=float(lvl.level_price),
            event_close=float(closes[i]),
            closes=closes,
            entry_index=entry_index,
        )
        seq += 1
        events.append(
            ReboundLevelEvent(
                event_id=f"RB_{seq:06d}",
                timestamp=ts.iloc[i],
                candle_index=i,
                side=side,
                expected_rebound_direction=direction,
                leverage=int(lvl.leverage),
                level_id=int(lvl.level_id),
                level_price=float(lvl.level_price),
                level_strength=int(lvl.strength),
                level_age=age,
                source_reference_price=float(lvl.reference_price),
                event_open=float(opens[i]),
                event_high=float(highs[i]),
                event_low=float(lows[i]),
                event_close=float(closes[i]),
                event_volume=float(volumes[i]),
                swept_distance_pct=sweep_depth_pct(side, float(lvl.level_price), float(highs[i]), float(lows[i])),
                close_relative_to_level_pct=close_relative_to_level_pct(side, float(closes[i]), float(lvl.level_price)),
                candle_return_pct=(float(closes[i]) / float(opens[i]) - 1.0) * 100.0 if opens[i] else 0.0,
                body_pct=geo["sweep_body_pct"],
                upper_wick_pct=geo["upper_wick_pct"],
                lower_wick_pct=geo["lower_wick_pct"],
                entry_index=entry_index,
                entry_price=float(opens[entry_index]),
                sample=assign_sample(i, n, cfg.in_sample_fraction),
                rejection=bool(rejection),
                breakthrough=bool(breakthrough),
                rejection_wick_gt_body=bool(wick_gt and rejection),
                close_in_favorable_third=bool(fav_third),
                age_bucket=age_bucket(age),
                reclaim_class=reclaim,
            )
        )
    return events


def build_leverage_combinations(
    level_events: Sequence[ReboundLevelEvent],
    ohlcv: pd.DataFrame,
    config: ReboundAuditConfig | None = None,
) -> list[LeverageCombinationEvent]:
    cfg = config or ReboundAuditConfig()
    data = normalize_ohlcv_dataframe(ohlcv) if "timestamp" not in ohlcv.columns else ohlcv
    n = len(data)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)

    # group by (candle, side)
    groups: dict[tuple[int, str], list[ReboundLevelEvent]] = {}
    for e in level_events:
        groups.setdefault((e.candle_index, e.side), []).append(e)

    out: list[LeverageCombinationEvent] = []
    seq = 0
    for (idx, side), evs in sorted(groups.items()):
        levers = {int(e.leverage) for e in evs}
        combo = leverage_combo_label(levers)
        deep = deepest_leverage(levers)
        if deep is None:
            continue
        entry_index = idx + 1
        if entry_index >= n:
            continue
        # reclaim using deepest level price among deepest leverage
        deep_evs = [e for e in evs if e.leverage == deep]
        # use farthest level in rebound-adverse direction as reference
        if side == SIDE_LOWER:
            ref_lvl = min(e.level_price for e in deep_evs)
        else:
            ref_lvl = max(e.level_price for e in deep_evs)
        reclaim = classify_reclaim(
            side=side,
            level_price=ref_lvl,
            event_close=float(closes[idx]),
            closes=closes,
            entry_index=entry_index,
        )
        rejection = (side == SIDE_LOWER and closes[idx] > ref_lvl) or (
            side == SIDE_UPPER and closes[idx] < ref_lvl
        )
        breakthrough = (side == SIDE_LOWER and closes[idx] < ref_lvl) or (
            side == SIDE_UPPER and closes[idx] > ref_lvl
        )
        seq += 1
        out.append(
            LeverageCombinationEvent(
                event_id=f"LC_{seq:06d}",
                timestamp=ts.iloc[idx],
                candle_index=idx,
                side=side,
                expected_rebound_direction="long" if side == SIDE_LOWER else "short",
                leverage_combination=combo,
                deepest_swept_leverage=int(deep),
                swept_level_count=len(evs),
                swept_total_strength=int(sum(e.level_strength for e in evs)),
                swept_100x_count=sum(1 for e in evs if e.leverage == 100),
                swept_50x_count=sum(1 for e in evs if e.leverage == 50),
                swept_25x_count=sum(1 for e in evs if e.leverage == 25),
                entry_index=entry_index,
                entry_price=float(opens[entry_index]),
                sample=assign_sample(idx, n, cfg.in_sample_fraction),
                level_ids=tuple(sorted(e.level_id for e in evs)),
                event_open=float(opens[idx]),
                event_high=float(highs[idx]),
                event_low=float(lows[idx]),
                event_close=float(closes[idx]),
                rejection=bool(rejection),
                breakthrough=bool(breakthrough),
                reclaim_class=reclaim,
            )
        )
    return out


def _side_leverage_presence(
    level_events: Sequence[ReboundLevelEvent],
) -> dict[tuple[str, int], set[int]]:
    """Map (side, leverage) -> set of candle indices with that sweep."""
    out: dict[tuple[str, int], set[int]] = {(s, lev): set() for s in (SIDE_LOWER, SIDE_UPPER) for lev in LEVERAGES}
    for e in level_events:
        out[(e.side, e.leverage)].add(e.candle_index)
    return out


def build_cascade_events(
    level_events: Sequence[ReboundLevelEvent],
    ohlcv: pd.DataFrame,
    config: ReboundAuditConfig | None = None,
) -> list[CascadeEvent]:
    """Detect causal leverage cascades; measure after last step only."""
    cfg = config or ReboundAuditConfig()
    data = ohlcv if "timestamp" in ohlcv.columns else normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    presence = _side_leverage_presence(level_events)

    patterns = {
        "100x->50x": (100, 50),
        "50x->25x": (50, 25),
        "100x->25x": (100, 25),
        "100x->50x->25x": (100, 50, 25),
    }
    out: list[CascadeEvent] = []
    seq = 0

    for side in (SIDE_LOWER, SIDE_UPPER):
        for window in cfg.cascade_windows:
            for ctype, steps in patterns.items():
                starts = sorted(presence[(side, steps[0])])
                for t0 in starts:
                    idxs = [t0]
                    ok = True
                    cur = t0
                    for lev in steps[1:]:
                        # find first occurrence of lev on same side in (cur, cur+window]
                        candidates = [
                            t
                            for t in presence[(side, lev)]
                            if cur < t <= cur + int(window)
                        ]
                        if not candidates:
                            ok = False
                            break
                        nxt = min(candidates)
                        idxs.append(nxt)
                        cur = nxt
                    if not ok:
                        continue
                    # for 100->50->25 ensure ordered chain already enforced
                    end = idxs[-1]
                    entry_index = end + 1
                    if entry_index >= n:
                        continue
                    seq += 1
                    out.append(
                        CascadeEvent(
                            event_id=f"CAS_{seq:06d}",
                            side=side,
                            cascade_type=ctype,
                            window=int(window),
                            start_index=int(t0),
                            end_index=int(end),
                            step_indices=tuple(int(x) for x in idxs),
                            entry_index=entry_index,
                            entry_price=float(opens[entry_index]),
                            sample=assign_sample(end, n, cfg.in_sample_fraction),
                            expected_rebound_direction="long" if side == SIDE_LOWER else "short",
                        )
                    )
    return out


def _percentile(arr: Sequence[float], q: float) -> float | None:
    if not arr:
        return None
    return float(np.percentile(np.asarray(arr, dtype=float), q))


def summarize_rebound_paths(
    *,
    group_name: str,
    side: str,
    sample: str,
    entry_indices: Sequence[int],
    entry_prices: Sequence[float],
    highs: np.ndarray,
    lows: np.ndarray,
    config: ReboundAuditConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    n = len(highs)
    for horizon in config.horizons:
        mfes: list[float] = []
        maes: list[float] = []
        paths: list[tuple[np.ndarray, np.ndarray]] = []
        for ei, ep in zip(entry_indices, entry_prices):
            if ei is None or ep is None:
                continue
            ei = int(ei)
            if ei < 0 or ei + horizon - 1 >= n:
                continue
            metrics = measure_path_metrics(
                side=side,
                entry_price=float(ep),
                highs=highs[ei : ei + horizon],
                lows=lows[ei : ei + horizon],
                horizon=int(horizon),
            )
            if not metrics:
                continue
            mfes.append(metrics["mfe_pct"])
            maes.append(metrics["mae_pct"])
            paths.append((metrics["fav_path"], metrics["adv_path"]))

        if not mfes:
            for thr in config.rebound_thresholds:
                rows.append(
                    {
                        "group": group_name,
                        "side": side,
                        "sample": sample,
                        "horizon": int(horizon),
                        "threshold_pct": float(thr),
                        "event_count": 0,
                        "hit_count": 0,
                        "hit_rate_pct": None,
                        "median_mfe_pct": None,
                        "mean_mfe_pct": None,
                        "p25_mfe_pct": None,
                        "p50_mfe_pct": None,
                        "p75_mfe_pct": None,
                        "p90_mfe_pct": None,
                        "median_mae_pct": None,
                        "mean_mae_pct": None,
                        "median_bars_to_threshold": None,
                        "rebound_before_equal_adverse_pct": None,
                        "rebound_before_adverse_0_25_pct": None,
                        "rebound_before_adverse_0_50_pct": None,
                    }
                )
            continue

        for thr in config.rebound_thresholds:
            hits = 0
            times: list[float] = []
            before_eq = before_25 = before_50 = 0
            for fav, adv in paths:
                t = first_touch_bar(fav, float(thr))
                if t is not None:
                    hits += 1
                    times.append(float(t))
                if rebound_before_adverse(fav, adv, float(thr), None):
                    before_eq += 1
                if rebound_before_adverse(fav, adv, float(thr), 0.25):
                    before_25 += 1
                if rebound_before_adverse(fav, adv, float(thr), 0.50):
                    before_50 += 1
            cnt = len(paths)
            rows.append(
                {
                    "group": group_name,
                    "side": side,
                    "sample": sample,
                    "horizon": int(horizon),
                    "threshold_pct": float(thr),
                    "event_count": cnt,
                    "hit_count": hits,
                    "hit_rate_pct": 100.0 * hits / cnt,
                    "median_mfe_pct": float(np.median(mfes)),
                    "mean_mfe_pct": float(np.mean(mfes)),
                    "p25_mfe_pct": _percentile(mfes, 25),
                    "p50_mfe_pct": _percentile(mfes, 50),
                    "p75_mfe_pct": _percentile(mfes, 75),
                    "p90_mfe_pct": _percentile(mfes, 90),
                    "median_mae_pct": float(np.median(maes)),
                    "mean_mae_pct": float(np.mean(maes)),
                    "median_bars_to_threshold": float(np.median(times)) if times else None,
                    "rebound_before_equal_adverse_pct": 100.0 * before_eq / cnt,
                    "rebound_before_adverse_0_25_pct": 100.0 * before_25 / cnt,
                    "rebound_before_adverse_0_50_pct": 100.0 * before_50 / cnt,
                }
            )
    return rows


def _filter_sample(events: Sequence[Any], sample: str) -> list[Any]:
    if sample == "full":
        return list(events)
    return [e for e in events if getattr(e, "sample") == sample]


def atr_proxy(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, i: int, win: int = 14) -> float:
    lo = max(0, i - win + 1)
    seg_h = highs[lo : i + 1]
    seg_l = lows[lo : i + 1]
    return float(np.mean(seg_h - seg_l)) if len(seg_h) else float(highs[i] - lows[i])


def build_control_pool(
    ohlcv: pd.DataFrame,
    sweep_indices: set[int],
) -> pd.DataFrame:
    data = ohlcv if "timestamp" in ohlcv.columns else normalize_ohlcv_dataframe(ohlcv)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    rows = []
    for i in range(len(data) - 1):  # need next open
        if i in sweep_indices:
            continue
        rng = float(highs[i] - lows[i])
        rows.append(
            {
                "candle_index": i,
                "month": str(ts.iloc[i].strftime("%Y-%m")),
                "hour": int(ts.iloc[i].hour),
                "range": rng,
                "atr": atr_proxy(highs, lows, closes, i),
                "entry_index": i + 1,
                "entry_price": float(data.iloc[i + 1]["open"]),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["range_q"] = pd.qcut(df["range"].rank(method="first"), q=5, labels=False, duplicates="drop")
    return df


def match_controls(
    events: Sequence[ReboundLevelEvent],
    control_pool: pd.DataFrame,
    *,
    seed: int,
    n_per_event: int = 1,
) -> list[dict[str, Any]]:
    """Deterministic matched controls: month + hour + range quantile."""
    if control_pool.empty or not events:
        return []
    rng = np.random.default_rng(seed)
    # index pool by keys
    pooled = control_pool.copy()
    out = []
    for e in events:
        month = str(pd.Timestamp(e.timestamp).strftime("%Y-%m"))
        hour = int(pd.Timestamp(e.timestamp).hour)
        # approximate range quantile of event candle
        ev_range = float(e.event_high - e.event_low)
        cand = pooled[(pooled["month"] == month) & (pooled["hour"] == hour)]
        if cand.empty:
            cand = pooled[pooled["month"] == month]
        if cand.empty:
            continue
        # nearest range
        diffs = (cand["range"] - ev_range).abs()
        order = np.argsort(diffs.to_numpy())
        take_n = min(max(n_per_event * 3, 3), len(order))
        top = cand.iloc[order[:take_n]]
        pick_i = int(rng.integers(0, len(top)))
        row = top.iloc[pick_i]
        out.append(
            {
                "source_event_id": e.event_id,
                "side": e.side,
                "leverage": e.leverage,
                "sample": e.sample,
                "control_candle_index": int(row["candle_index"]),
                "entry_index": int(row["entry_index"]),
                "entry_price": float(row["entry_price"]),
                "expected_rebound_direction": e.expected_rebound_direction,
            }
        )
    return out


def bootstrap_diff_ci(
    event_values: Sequence[float],
    control_values: Sequence[float],
    *,
    resamples: int,
    seed: int,
) -> dict[str, float | None]:
    if not event_values or not control_values:
        return {"diff_mean": None, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    ev = np.asarray(event_values, dtype=float)
    ct = np.asarray(control_values, dtype=float)
    diffs = np.empty(resamples, dtype=float)
    for i in range(resamples):
        eb = rng.choice(ev, size=len(ev), replace=True)
        cb = rng.choice(ct, size=len(ct), replace=True)
        diffs[i] = float(np.mean(eb) - np.mean(cb))
    return {
        "diff_mean": float(np.mean(ev) - np.mean(ct)),
        "ci_low": float(np.percentile(diffs, 2.5)),
        "ci_high": float(np.percentile(diffs, 97.5)),
    }


def _records(objs: Iterable[Any]) -> list[dict[str, Any]]:
    rows = []
    for obj in objs:
        row = asdict(obj)
        for k, v in list(row.items()):
            if isinstance(v, pd.Timestamp):
                row[k] = str(v)
            elif isinstance(v, tuple):
                row[k] = ",".join(str(x) for x in v)
        rows.append(row)
    return rows


@dataclass
class ReboundAuditBundle:
    config: ReboundAuditConfig
    level_events: list[ReboundLevelEvent]
    combination_events: list[LeverageCombinationEvent]
    cascade_events: list[CascadeEvent]
    threshold_summary: list[dict[str, Any]]
    reclaim_summary: list[dict[str, Any]]
    rejection_summary: list[dict[str, Any]]
    control_comparison: list[dict[str, Any]]
    monthly_summary: list[dict[str, Any]]
    summary_full: dict[str, Any]
    summary_in_sample: dict[str, Any]
    summary_out_of_sample: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


def run_leverage_rebound_audit(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    config: ReboundAuditConfig | None = None,
) -> ReboundAuditBundle:
    cfg = config or ReboundAuditConfig()
    data = normalize_ohlcv_dataframe(ohlcv)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    n = len(data)

    print("building rebound level events...", flush=True)
    level_events = build_rebound_level_events(result, data, cfg)
    print(f"level_events={len(level_events)}", flush=True)
    combos = build_leverage_combinations(level_events, data, cfg)
    print(f"leverage_combinations={len(combos)}", flush=True)
    cascades = build_cascade_events(level_events, data, cfg)
    print(f"cascade_events={len(cascades)}", flush=True)

    threshold_rows: list[dict[str, Any]] = []
    reclaim_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []

    for sample in ("full", "in_sample", "out_of_sample"):
        evs = _filter_sample(level_events, sample)
        # per leverage × side
        for side in (SIDE_LOWER, SIDE_UPPER):
            for lev in LEVERAGES:
                sub = [e for e in evs if e.side == side and e.leverage == lev]
                threshold_rows.extend(
                    summarize_rebound_paths(
                        group_name=f"{side}_{lev}x",
                        side=side,
                        sample=sample,
                        entry_indices=[e.entry_index for e in sub if e.entry_index is not None],
                        entry_prices=[e.entry_price for e in sub if e.entry_price is not None],
                        highs=highs,
                        lows=lows,
                        config=cfg,
                    )
                )
                # reclaim breakdown
                for rc in (
                    "immediate_reclaim",
                    "next_candle_reclaim",
                    "reclaim_within_3",
                    "reclaim_within_6",
                    "no_reclaim",
                ):
                    rsub = [e for e in sub if e.reclaim_class == rc]
                    path_rows = summarize_rebound_paths(
                        group_name=f"{side}_{lev}x__{rc}",
                        side=side,
                        sample=sample,
                        entry_indices=[e.entry_index for e in rsub if e.entry_index is not None],
                        entry_prices=[e.entry_price for e in rsub if e.entry_price is not None],
                        highs=highs,
                        lows=lows,
                        config=cfg,
                    )
                    # store compact reclaim counts + h=3 thr=0.25 snapshot
                    snap = next(
                        (
                            r
                            for r in path_rows
                            if r["horizon"] == 3 and abs(r["threshold_pct"] - 0.25) < 1e-12
                        ),
                        None,
                    )
                    reclaim_rows.append(
                        {
                            "group": f"{side}_{lev}x",
                            "sample": sample,
                            "reclaim_class": rc,
                            "event_count": len(rsub),
                            "share_pct": (100.0 * len(rsub) / len(sub)) if sub else None,
                            "horizon3_thr0_25_hit_rate_pct": None if not snap else snap["hit_rate_pct"],
                            "horizon3_mean_mfe_pct": None if not snap else snap["mean_mfe_pct"],
                            "horizon3_mean_mae_pct": None if not snap else snap["mean_mae_pct"],
                        }
                    )

                # rejection vs breakthrough
                for label, pred in (
                    ("rejection", lambda e: e.rejection),
                    ("breakthrough", lambda e: e.breakthrough),
                    ("rejection_wick_gt_body", lambda e: e.rejection_wick_gt_body),
                    ("close_favorable_third", lambda e: e.close_in_favorable_third),
                ):
                    ssub = [e for e in sub if pred(e)]
                    path_rows = summarize_rebound_paths(
                        group_name=f"{side}_{lev}x__{label}",
                        side=side,
                        sample=sample,
                        entry_indices=[e.entry_index for e in ssub if e.entry_index is not None],
                        entry_prices=[e.entry_price for e in ssub if e.entry_price is not None],
                        highs=highs,
                        lows=lows,
                        config=cfg,
                    )
                    snap = next(
                        (
                            r
                            for r in path_rows
                            if r["horizon"] == 3 and abs(r["threshold_pct"] - 0.25) < 1e-12
                        ),
                        None,
                    )
                    rejection_rows.append(
                        {
                            "group": f"{side}_{lev}x",
                            "sample": sample,
                            "style": label,
                            "event_count": len(ssub),
                            "share_pct": (100.0 * len(ssub) / len(sub)) if sub else None,
                            "horizon3_thr0_25_hit_rate_pct": None if not snap else snap["hit_rate_pct"],
                            "horizon3_mean_mfe_pct": None if not snap else snap["mean_mfe_pct"],
                            "horizon3_mean_mae_pct": None if not snap else snap["mean_mae_pct"],
                        }
                    )

                # strength / age buckets
                for strength in (1, 2, 3):
                    ssub = [e for e in sub if e.level_strength == strength]
                    path_rows = summarize_rebound_paths(
                        group_name=f"{side}_{lev}x__strength{strength}",
                        side=side,
                        sample=sample,
                        entry_indices=[e.entry_index for e in ssub if e.entry_index is not None],
                        entry_prices=[e.entry_price for e in ssub if e.entry_price is not None],
                        highs=highs,
                        lows=lows,
                        config=cfg,
                    )
                    threshold_rows.extend(path_rows)
                for ab in ("0_6", "7_24", "25_96", "gt_96"):
                    ssub = [e for e in sub if e.age_bucket == ab]
                    threshold_rows.extend(
                        summarize_rebound_paths(
                            group_name=f"{side}_{lev}x__age_{ab}",
                            side=side,
                            sample=sample,
                            entry_indices=[e.entry_index for e in ssub if e.entry_index is not None],
                            entry_prices=[e.entry_price for e in ssub if e.entry_price is not None],
                            highs=highs,
                            lows=lows,
                            config=cfg,
                        )
                    )

        # combination groups
        com_s = _filter_sample(combos, sample)
        for combo_name in (
            "100x_only",
            "50x_only",
            "25x_only",
            "100x_50x",
            "50x_25x",
            "100x_25x",
            "100x_50x_25x",
        ):
            for side in (SIDE_LOWER, SIDE_UPPER):
                sub = [e for e in com_s if e.side == side and e.leverage_combination == combo_name]
                threshold_rows.extend(
                    summarize_rebound_paths(
                        group_name=f"combo_{side}_{combo_name}",
                        side=side,
                        sample=sample,
                        entry_indices=[e.entry_index for e in sub if e.entry_index is not None],
                        entry_prices=[e.entry_price for e in sub if e.entry_price is not None],
                        highs=highs,
                        lows=lows,
                        config=cfg,
                    )
                )

        # cascades
        cas_s = _filter_sample(cascades, sample)
        for ctype in ("100x->50x", "50x->25x", "100x->25x", "100x->50x->25x"):
            for window in cfg.cascade_windows:
                for side in (SIDE_LOWER, SIDE_UPPER):
                    sub = [
                        e
                        for e in cas_s
                        if e.side == side and e.cascade_type == ctype and e.window == window
                    ]
                    threshold_rows.extend(
                        summarize_rebound_paths(
                            group_name=f"cascade_{side}_{ctype}_w{window}",
                            side=side,
                            sample=sample,
                            entry_indices=[e.entry_index for e in sub if e.entry_index is not None],
                            entry_prices=[e.entry_price for e in sub if e.entry_price is not None],
                            highs=highs,
                            lows=lows,
                            config=cfg,
                        )
                    )

    # controls
    print("building matched controls...", flush=True)
    sweep_idx = {e.candle_index for e in level_events}
    pool = build_control_pool(data, sweep_idx)
    controls = match_controls(level_events, pool, seed=cfg.seed, n_per_event=1)
    control_rows: list[dict[str, Any]] = []
    for sample in ("full", "in_sample", "out_of_sample"):
        for side in (SIDE_LOWER, SIDE_UPPER):
            for lev in LEVERAGES:
                evs = [
                    e
                    for e in _filter_sample(level_events, sample)
                    if e.side == side and e.leverage == lev
                ]
                ctr = [
                    c
                    for c in controls
                    if c["side"] == side
                    and c["leverage"] == lev
                    and (sample == "full" or c["sample"] == sample)
                ]
                # event / control hit rates at h=3, thr=0.25 and MFE
                def path_stats(entries: list[tuple[int, float]], s: str) -> dict[str, Any]:
                    mfes = []
                    hits_010 = hits_025 = hits_050 = 0
                    before = 0
                    usable = 0
                    for ei, ep in entries:
                        if ei + 3 - 1 >= n:
                            continue
                        m = measure_path_metrics(
                            side=s,
                            entry_price=ep,
                            highs=highs[ei : ei + 3],
                            lows=lows[ei : ei + 3],
                            horizon=3,
                        )
                        if not m:
                            continue
                        usable += 1
                        mfes.append(m["mfe_pct"])
                        if first_touch_bar(m["fav_path"], 0.10) is not None:
                            hits_010 += 1
                        if first_touch_bar(m["fav_path"], 0.25) is not None:
                            hits_025 += 1
                        if first_touch_bar(m["fav_path"], 0.50) is not None:
                            hits_050 += 1
                        if rebound_before_adverse(m["fav_path"], m["adv_path"], 0.25, 0.25):
                            before += 1
                    if usable == 0:
                        return {
                            "n": 0,
                            "hit_0_10": None,
                            "hit_0_25": None,
                            "hit_0_50": None,
                            "mean_mfe": None,
                            "before_adv": None,
                            "mfes": [],
                        }
                    return {
                        "n": usable,
                        "hit_0_10": 100.0 * hits_010 / usable,
                        "hit_0_25": 100.0 * hits_025 / usable,
                        "hit_0_50": 100.0 * hits_050 / usable,
                        "mean_mfe": float(np.mean(mfes)),
                        "before_adv": 100.0 * before / usable,
                        "mfes": mfes,
                    }

                est = path_stats(
                    [(int(e.entry_index), float(e.entry_price)) for e in evs if e.entry_index is not None],
                    side,
                )
                cst = path_stats(
                    [(int(c["entry_index"]), float(c["entry_price"])) for c in ctr],
                    side,
                )
                ci = bootstrap_diff_ci(
                    est["mfes"],
                    cst["mfes"],
                    resamples=min(cfg.bootstrap_resamples, 1000),
                    seed=cfg.seed + lev + (0 if side == SIDE_LOWER else 17),
                )
                control_rows.append(
                    {
                        "group": f"{side}_{lev}x",
                        "sample": sample,
                        "event_n": est["n"],
                        "control_n": cst["n"],
                        "event_hit_0_10_h3": est["hit_0_10"],
                        "control_hit_0_10_h3": cst["hit_0_10"],
                        "event_hit_0_25_h3": est["hit_0_25"],
                        "control_hit_0_25_h3": cst["hit_0_25"],
                        "event_hit_0_50_h3": est["hit_0_50"],
                        "control_hit_0_50_h3": cst["hit_0_50"],
                        "event_mean_mfe_h3": est["mean_mfe"],
                        "control_mean_mfe_h3": cst["mean_mfe"],
                        "event_minus_control_mfe": (
                            None
                            if est["mean_mfe"] is None or cst["mean_mfe"] is None
                            else est["mean_mfe"] - cst["mean_mfe"]
                        ),
                        "event_rebound_before_adverse_0_25": est["before_adv"],
                        "control_rebound_before_adverse_0_25": cst["before_adv"],
                        "bootstrap_mfe_diff_mean": ci["diff_mean"],
                        "bootstrap_mfe_diff_ci95_low": ci["ci_low"],
                        "bootstrap_mfe_diff_ci95_high": ci["ci_high"],
                        "note": "empirical comparison only; not a formal significance claim",
                    }
                )

    # monthly summary for base groups h=3 thr=0.25
    ts = pd.to_datetime(data["timestamp"], utc=True)
    for e in level_events:
        month = str(pd.Timestamp(e.timestamp).strftime("%Y-%m"))
        if e.entry_index is None or e.entry_index + 2 >= n:
            continue
        m = measure_path_metrics(
            side=e.side,
            entry_price=float(e.entry_price),
            highs=highs[e.entry_index : e.entry_index + 3],
            lows=lows[e.entry_index : e.entry_index + 3],
            horizon=3,
        )
        if not m:
            continue
        monthly_rows.append(
            {
                "month": month,
                "side": e.side,
                "leverage": e.leverage,
                "sample": e.sample,
                "mfe_pct": m["mfe_pct"],
                "mae_pct": m["mae_pct"],
                "hit_0_25": first_touch_bar(m["fav_path"], 0.25) is not None,
            }
        )
    monthly_summary: list[dict[str, Any]] = []
    if monthly_rows:
        mdf = pd.DataFrame(monthly_rows)
        for (month, side, lev), g in mdf.groupby(["month", "side", "leverage"]):
            monthly_summary.append(
                {
                    "month": month,
                    "side": side,
                    "leverage": int(lev),
                    "event_count": int(len(g)),
                    "mean_mfe_pct": float(g["mfe_pct"].mean()),
                    "median_mfe_pct": float(g["mfe_pct"].median()),
                    "mean_mae_pct": float(g["mae_pct"].mean()),
                    "hit_rate_0_25_pct": 100.0 * float(g["hit_0_25"].mean()),
                }
            )

    def build_summary(sample: str) -> dict[str, Any]:
        evs = _filter_sample(level_events, sample)
        counts = {
            f"{side}_{lev}x": sum(1 for e in evs if e.side == side and e.leverage == lev)
            for side in (SIDE_LOWER, SIDE_UPPER)
            for lev in LEVERAGES
        }
        # key hit rates from threshold_rows
        key = {}
        for thr in (0.10, 0.25, 0.50):
            for h in (1, 3, 6, 12):
                for side in (SIDE_LOWER, SIDE_UPPER):
                    for lev in LEVERAGES:
                        g = f"{side}_{lev}x"
                        row = next(
                            (
                                r
                                for r in threshold_rows
                                if r["group"] == g
                                and r["sample"] == sample
                                and r["horizon"] == h
                                and abs(r["threshold_pct"] - thr) < 1e-12
                            ),
                            None,
                        )
                        key[f"{g}_h{h}_thr{thr}"] = None if not row else {
                            "hit_rate_pct": row["hit_rate_pct"],
                            "mean_mfe_pct": row["mean_mfe_pct"],
                            "mean_mae_pct": row["mean_mae_pct"],
                            "event_count": row["event_count"],
                        }
        ctrl = [r for r in control_rows if r["sample"] == sample]
        # tradeability vs 0.12%
        tradeable_notes = {}
        for side in (SIDE_LOWER, SIDE_UPPER):
            for lev in LEVERAGES:
                g = f"{side}_{lev}x"
                row = next(
                    (
                        r
                        for r in threshold_rows
                        if r["group"] == g
                        and r["sample"] == sample
                        and r["horizon"] == 3
                        and abs(r["threshold_pct"] - 0.25) < 1e-12
                    ),
                    None,
                )
                if not row or row["mean_mfe_pct"] is None:
                    tradeable_notes[g] = "insufficient_data"
                else:
                    # rough: mean MFE vs cost; not a strategy PnL
                    tradeable_notes[g] = {
                        "mean_mfe_h3": row["mean_mfe_pct"],
                        "mean_mae_h3": row["mean_mae_pct"],
                        "cost_pct": cfg.roundtrip_cost_pct,
                        "mean_mfe_exceeds_cost": bool(row["mean_mfe_pct"] > cfg.roundtrip_cost_pct),
                        "note": "MFE is peak favorable excursion, not realized net after costs/slippage",
                    }
        return {
            "sample": sample,
            "event_counts": counts,
            "combination_counts": {
                name: sum(
                    1
                    for e in _filter_sample(combos, sample)
                    if e.leverage_combination == name
                )
                for name in (
                    "100x_only",
                    "50x_only",
                    "25x_only",
                    "100x_50x",
                    "50x_25x",
                    "100x_25x",
                    "100x_50x_25x",
                )
            },
            "cascade_counts": {
                f"{ctype}_w{w}": sum(
                    1
                    for e in _filter_sample(cascades, sample)
                    if e.cascade_type == ctype and e.window == w
                )
                for ctype in ("100x->50x", "50x->25x", "100x->25x", "100x->50x->25x")
                for w in cfg.cascade_windows
            },
            "key_hit_rates": key,
            "control_comparison": ctrl,
            "cost_vs_mfe": tradeable_notes,
            "disclaimer": (
                "Levels are estimated LuxAlgo-style liquidation levels, "
                "not real exchange liquidation feeds."
            ),
        }

    meta = {
        "n_candles": n,
        "in_sample_cut": in_sample_cut(n, cfg.in_sample_fraction),
        "start_timestamp": str(data.iloc[0]["timestamp"]),
        "end_timestamp": str(data.iloc[-1]["timestamp"]),
        "n_level_events": len(level_events),
        "n_combination_events": len(combos),
        "n_cascade_events": len(cascades),
    }
    return ReboundAuditBundle(
        config=cfg,
        level_events=level_events,
        combination_events=combos,
        cascade_events=cascades,
        threshold_summary=threshold_rows,
        reclaim_summary=reclaim_rows,
        rejection_summary=rejection_rows,
        control_comparison=control_rows,
        monthly_summary=monthly_summary,
        summary_full=build_summary("full"),
        summary_in_sample=build_summary("in_sample"),
        summary_out_of_sample=build_summary("out_of_sample"),
        meta=meta,
    )


def events_to_dataframe(events: Sequence[Any]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))
