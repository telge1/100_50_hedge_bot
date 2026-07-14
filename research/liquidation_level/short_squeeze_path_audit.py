"""Path / excursion audit after upper 50x/25x LuxAlgo-style level sweeps.

Focus: further upside after short entry, subsequent downside, timing order.
Estimated levels only — not real exchange liquidations. No classic SL scoring.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_backtest import in_sample_cut
from research.liquidation_level.liquidation_levels import (
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
)
from research.liquidation_level.short_squeeze_continuation_audit import (
    MARCH_END,
    MARCH_START,
    ShortSqueezeConfig,
    ShortSqueezeEvent,
    _filter_sample,
    _group_entry_mode,
    aggregate_closed_htf_local,
    build_upper_squeeze_events,
    enrich_htf_indicators,
    match_controls,
    select_group_events_combo_aware,
)

DEFAULT_PATH_HORIZONS = (1, 3, 5, 10, 12, 15, 20, 25, 30, 40, 50)
ADVERSE_THRESHOLDS = (0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 5.00)
FAVORABLE_THRESHOLDS = ADVERSE_THRESHOLDS
PROFILE_MAX = 50
MINUTES_PER_CANDLE = 5.0

PATH_GROUPS = [
    "upper_50x__sweep_only",
    "upper_50x__immediate_reclaim",
    "upper_50x__delayed_reclaim_1_to_3",
    "upper_50x__reclaim_within_3",
    "upper_50x__reclaim_within_3__T1",
    "upper_50x__reclaim_within_3__T3",
    "upper_25x__sweep_only",
    "upper_25x__immediate_reclaim",
    "upper_25x__delayed_reclaim_1_to_3",
    "upper_25x__reclaim_within_3",
    "upper_25x__reclaim_within_3__T1",
    "upper_25x__reclaim_within_3__T3",
    "combo_50x_25x__reclaim_within_3",
    "combo_100x_50x_25x__reclaim_within_3",
    "upper_50x__no_reclaim_within_3",
    "upper_25x__no_reclaim_within_3",
    "upper_50x__reclaim_within_3__no_T3",
    "upper_25x__reclaim_within_3__no_T3",
]


@dataclass(frozen=True)
class PathAuditConfig:
    horizons: tuple[int, ...] = DEFAULT_PATH_HORIZONS
    seed: int = 42
    in_sample_fraction: float = 0.70
    skip_controls: bool = False
    profile_max: int = PROFILE_MAX


def _percentile(arr: Sequence[float], q: float) -> float | None:
    if not arr:
        return None
    return float(np.percentile(np.asarray(arr, dtype=float), q))


def analyze_short_path(
    *,
    entry_index: int,
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: pd.Series,
    horizon: int,
) -> dict[str, Any] | None:
    """Causal path metrics for a short entry over the next ``horizon`` bars."""
    n = len(highs)
    if entry_price <= 0 or entry_index < 0 or entry_index >= n or horizon < 1:
        return None
    end = min(entry_index + horizon, n)
    if end <= entry_index:
        return None
    h = highs[entry_index:end]
    l = lows[entry_index:end]
    c = closes[entry_index:end]
    bars = int(end - entry_index)

    adverse = (h / entry_price - 1.0) * 100.0
    favorable = (1.0 - l / entry_price) * 100.0

    adv_i = int(np.argmax(adverse))
    fav_i = int(np.argmax(favorable))
    max_adv = float(adverse[adv_i])
    max_fav = float(favorable[fav_i])
    peak_price = float(h[adv_i])
    trough_price = float(l[fav_i])

    if adv_i < fav_i:
        order = "adverse_peak_before_favorable_trough"
    elif fav_i < adv_i:
        order = "favorable_trough_before_adverse_peak"
    else:
        order = "same_candle_peak_and_trough"

    # A: drop from peak to subsequent trough (only lows after peak, including peak bar)
    subsequent_low = float(np.min(l[adv_i:]))
    drop_from_peak = (1.0 - subsequent_low / peak_price) * 100.0 if peak_price > 0 else 0.0
    trough_after_peak_i = adv_i + int(np.argmin(l[adv_i:]))

    # B: rise from trough to subsequent peak
    subsequent_high = float(np.max(h[fav_i:]))
    rise_from_trough = (subsequent_high / trough_price - 1.0) * 100.0 if trough_price > 0 else 0.0
    peak_after_trough_i = fav_i + int(np.argmax(h[fav_i:]))

    def _ts_at(local_i: int) -> str:
        abs_i = entry_index + local_i
        return str(timestamps.iloc[abs_i])

    return {
        "horizon": int(horizon),
        "bars_available": bars,
        "complete_horizon": bars >= horizon,
        "max_adverse_move_pct": max_adv,
        "candle_of_max_adverse": adv_i + 1,  # 1-based bars after entry start
        "minutes_to_max_adverse": (adv_i + 1) * MINUTES_PER_CANDLE,
        "timestamp_of_max_adverse": _ts_at(adv_i),
        "price_at_max_adverse": peak_price,
        "max_favorable_move_pct": max_fav,
        "candle_of_max_favorable": fav_i + 1,
        "minutes_to_max_favorable": (fav_i + 1) * MINUTES_PER_CANDLE,
        "timestamp_of_max_favorable": _ts_at(fav_i),
        "price_at_max_favorable": trough_price,
        "order": order,
        "adverse_peak_before_favorable_trough": order == "adverse_peak_before_favorable_trough",
        "favorable_trough_before_adverse_peak": order == "favorable_trough_before_adverse_peak",
        "same_candle_peak_and_trough": order == "same_candle_peak_and_trough",
        "candles_between_adverse_peak_and_favorable_trough": int(abs(fav_i - adv_i)),
        "minutes_between_adverse_peak_and_favorable_trough": abs(fav_i - adv_i) * MINUTES_PER_CANDLE,
        "highest_high_before_drop_price": peak_price,
        "highest_high_before_drop_pct_from_entry": max_adv,
        "subsequent_lowest_low_price": subsequent_low,
        "drop_from_peak_pct": drop_from_peak,
        "candles_from_peak_to_trough": int(trough_after_peak_i - adv_i),
        "minutes_from_peak_to_trough": int(trough_after_peak_i - adv_i) * MINUTES_PER_CANDLE,
        "trough_to_subsequent_peak_pct": rise_from_trough,
        "candles_from_trough_to_peak": int(peak_after_trough_i - fav_i),
        "drop_over_adverse_ratio": (
            None if max_adv <= 1e-12 else float(drop_from_peak / max_adv)
        ),
        "close_return_pct": (1.0 - float(c[-1]) / entry_price) * 100.0,
        "adverse_path": adverse,
        "favorable_path": favorable,
        "close_path": (1.0 - c / entry_price) * 100.0,
    }


def classify_path_category(path50: dict[str, Any]) -> str:
    """Classify path within available bars (prefer complete 50)."""
    adv = float(path50["max_adverse_move_pct"])
    fav = float(path50["max_favorable_move_pct"])
    drop = float(path50["drop_from_peak_pct"])
    peak_first = bool(path50["adverse_peak_before_favorable_trough"] or path50["same_candle_peak_and_trough"])

    if adv < 0.25 and fav >= 0.50:
        return "immediate_drop"
    if adv >= 1.00 and drop >= 1.00 and peak_first:
        return "deep_squeeze_then_drop"
    if adv >= 0.25 and drop >= 0.50 and peak_first:
        return "squeeze_then_drop"
    if adv >= 1.00 and drop < 0.50:
        return "immediate_breakout"
    if adv >= 0.50 and drop < 0.50:
        return "squeeze_without_drop"
    if adv < 0.50 and fav < 0.50:
        return "sideways_noise"
    # residual: fell without fitting immediate_drop exactly
    if fav >= 0.50 and adv < 0.25:
        return "immediate_drop"
    if peak_first and drop >= 0.50:
        return "squeeze_then_drop"
    return "sideways_noise"


def event_entry(e: ShortSqueezeEvent, mode: str) -> tuple[int, float] | None:
    if mode == "reclaim":
        if e.entry_index is None or e.entry_price is None:
            return None
        return int(e.entry_index), float(e.entry_price)
    ei = e.__dict__.get("_sweep_only_entry_index")
    ep = e.__dict__.get("_sweep_only_entry_price")
    if ei is None or ep is None:
        return None
    return int(ei), float(ep)


def select_path_group(events: Sequence[ShortSqueezeEvent], group: str) -> list[ShortSqueezeEvent]:
    if group.endswith("__no_T3"):
        base = group.replace("__no_T3", "")
        xs = select_group_events_combo_aware(events, base)
        return [e for e in xs if not e.trend_t3]
    if group.endswith("__reclaim_within_3") and group.startswith("combo_"):
        return select_group_events_combo_aware(events, group)
    return select_group_events_combo_aware(events, group)


def dist_summary(values: Sequence[float], prefix: str) -> dict[str, Any]:
    if not values:
        return {
            f"{prefix}_mean": None,
            f"{prefix}_median": None,
            f"{prefix}_p25": None,
            f"{prefix}_p50": None,
            f"{prefix}_p75": None,
            f"{prefix}_p80": None,
            f"{prefix}_p90": None,
            f"{prefix}_p95": None,
            f"{prefix}_max": None,
            f"{prefix}_n": 0,
        }
    arr = np.asarray(values, dtype=float)
    return {
        f"{prefix}_mean": float(np.mean(arr)),
        f"{prefix}_median": float(np.median(arr)),
        f"{prefix}_p25": float(np.percentile(arr, 25)),
        f"{prefix}_p50": float(np.percentile(arr, 50)),
        f"{prefix}_p75": float(np.percentile(arr, 75)),
        f"{prefix}_p80": float(np.percentile(arr, 80)),
        f"{prefix}_p90": float(np.percentile(arr, 90)),
        f"{prefix}_p95": float(np.percentile(arr, 95)),
        f"{prefix}_max": float(np.max(arr)),
        f"{prefix}_n": int(len(arr)),
    }


def threshold_shares(values: Sequence[float], thresholds: Sequence[float], prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    n = len(values)
    for thr in thresholds:
        key = f"{prefix}_ge_{str(thr).replace('.', '_')}_pct"
        out[key] = None if n == 0 else 100.0 * sum(1 for v in values if v >= thr) / n
    return out


@dataclass
class PathAuditBundle:
    config: PathAuditConfig
    events: list[ShortSqueezeEvent]
    path_events: list[dict[str, Any]]
    path_horizon_metrics: list[dict[str, Any]]
    peak_then_trough_events: list[dict[str, Any]]
    path_category_events: list[dict[str, Any]]
    path_category_summary: list[dict[str, Any]]
    path_profile_mean: list[dict[str, Any]]
    path_profile_quantiles: list[dict[str, Any]]
    leverage_comparison: list[dict[str, Any]]
    trend_comparison: list[dict[str, Any]]
    control_comparison: list[dict[str, Any]]
    summary_full: dict[str, Any]
    summary_in_sample: dict[str, Any]
    summary_out_of_sample: dict[str, Any]
    summary_march: dict[str, Any]
    summary_march_06: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


def _build_horizon_metrics(
    group: str,
    sample: str,
    paths: list[dict[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    sub = [p for p in paths if int(p["horizon"]) == int(horizon) and p.get("complete_horizon")]
    # allow incomplete only if bars_available == horizon requirement failed — exclude
    adv = [float(p["max_adverse_move_pct"]) for p in sub]
    fav = [float(p["max_favorable_move_pct"]) for p in sub]
    drop = [float(p["drop_from_peak_pct"]) for p in sub]
    t_adv = [float(p["minutes_to_max_adverse"]) for p in sub]
    t_fav = [float(p["minutes_to_max_favorable"]) for p in sub]
    t_peak_to_trough = [float(p["minutes_from_peak_to_trough"]) for p in sub]
    ratios = [float(p["drop_over_adverse_ratio"]) for p in sub if p.get("drop_over_adverse_ratio") is not None]
    peak_first = sum(1 for p in sub if p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"])
    n = len(sub)
    row = {
        "group": group,
        "sample": sample,
        "horizon": int(horizon),
        "n": n,
        "share_peak_then_trough_pct": None if n == 0 else 100.0 * peak_first / n,
        "share_trough_then_peak_pct": (
            None
            if n == 0
            else 100.0 * sum(1 for p in sub if p["favorable_trough_before_adverse_peak"]) / n
        ),
        "share_same_candle_pct": (
            None if n == 0 else 100.0 * sum(1 for p in sub if p["same_candle_peak_and_trough"]) / n
        ),
        "median_minutes_to_peak": float(np.median(t_adv)) if t_adv else None,
        "mean_minutes_to_peak": float(np.mean(t_adv)) if t_adv else None,
        "median_minutes_to_trough": float(np.median(t_fav)) if t_fav else None,
        "mean_minutes_to_trough": float(np.mean(t_fav)) if t_fav else None,
        "median_minutes_peak_to_trough": float(np.median(t_peak_to_trough)) if t_peak_to_trough else None,
        "median_drop_over_adverse_ratio": float(np.median(ratios)) if ratios else None,
        **dist_summary(adv, "adverse"),
        **dist_summary(fav, "favorable"),
        **dist_summary(drop, "drop_from_peak"),
        **threshold_shares(adv, ADVERSE_THRESHOLDS, "adverse"),
        **threshold_shares(fav, FAVORABLE_THRESHOLDS, "favorable"),
        **threshold_shares(drop, FAVORABLE_THRESHOLDS, "drop_from_peak"),
        "share_immediate_no_025_adv_then_050_fav_pct": (
            None
            if n == 0
            else 100.0 * sum(1 for p in sub if p["max_adverse_move_pct"] < 0.25 and p["max_favorable_move_pct"] >= 0.50) / n
        ),
    }
    return row


def run_short_squeeze_path_audit(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    config: PathAuditConfig | None = None,
) -> PathAuditBundle:
    cfg = config or PathAuditConfig()
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    end_wall = pd.to_datetime(data["timestamp"].iloc[-1], utc=True) + pd.Timedelta(minutes=5)

    print("aggregating HTF + building squeeze events...", flush=True)
    htf15 = enrich_htf_indicators(aggregate_closed_htf_local(data, 15, end_wall))
    htf30 = enrich_htf_indicators(aggregate_closed_htf_local(data, 30, end_wall))
    sq_cfg = ShortSqueezeConfig(skip_tp_sl=True, skip_bootstrap=True, seed=cfg.seed)
    events = build_upper_squeeze_events(result, data, htf15, htf30, sq_cfg)
    print(f"events={len(events)}", flush=True)

    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)

    path_events: list[dict[str, Any]] = []
    peak_then_trough_events: list[dict[str, Any]] = []
    path_category_events: list[dict[str, Any]] = []
    # cache full path dicts (incl. arrays) by (event_id, entry_mode, horizon)
    path_cache: dict[tuple[str, str, int], dict[str, Any]] = {}

    print("computing paths...", flush=True)
    for e in events:
        for mode in ("reclaim", "sweep_only"):
            ent = event_entry(e, mode)
            if ent is None:
                continue
            ei, ep = ent
            if ei >= n:
                continue
            for h in cfg.horizons:
                p = analyze_short_path(
                    entry_index=ei,
                    entry_price=ep,
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    timestamps=ts,
                    horizon=int(h),
                )
                if p is None:
                    continue
                path_cache[(e.event_id, mode, int(h))] = p
                row = {
                    "event_id": e.event_id,
                    "group_hint_leverage": e.leverage,
                    "sample": e.sample,
                    "entry_mode": mode,
                    "candle_index": e.candle_index,
                    "entry_index": ei,
                    "entry_price": ep,
                    "entry_timestamp": str(ts.iloc[ei]),
                    "leverage": e.leverage,
                    "exclusive_reclaim_group": e.exclusive_reclaim_group,
                    "trend_t1": e.trend_t1,
                    "trend_t3": e.trend_t3,
                    "is_march_window": e.is_march_window,
                    "is_march_06": e.is_march_06,
                    "leverage_combination": e.leverage_combination,
                    **{k: v for k, v in p.items() if k not in {"adverse_path", "favorable_path", "close_path"}},
                }
                path_events.append(row)
                if int(h) == int(cfg.profile_max) and p["complete_horizon"]:
                    cat = classify_path_category(p)
                    path_category_events.append(
                        {
                            "event_id": e.event_id,
                            "entry_mode": mode,
                            "sample": e.sample,
                            "leverage": e.leverage,
                            "exclusive_reclaim_group": e.exclusive_reclaim_group,
                            "trend_t1": e.trend_t1,
                            "trend_t3": e.trend_t3,
                            "is_march_window": e.is_march_window,
                            "is_march_06": e.is_march_06,
                            "path_category": cat,
                            "max_adverse_move_pct": p["max_adverse_move_pct"],
                            "max_favorable_move_pct": p["max_favorable_move_pct"],
                            "drop_from_peak_pct": p["drop_from_peak_pct"],
                            "order": p["order"],
                            "minutes_to_max_adverse": p["minutes_to_max_adverse"],
                            "minutes_to_max_favorable": p["minutes_to_max_favorable"],
                            "minutes_from_peak_to_trough": p["minutes_from_peak_to_trough"],
                        }
                    )
                    if p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"]:
                        peak_then_trough_events.append(
                            {
                                "event_id": e.event_id,
                                "entry_mode": mode,
                                "sample": e.sample,
                                "leverage": e.leverage,
                                "trend_t3": e.trend_t3,
                                "highest_high_before_drop_price": p["highest_high_before_drop_price"],
                                "highest_high_before_drop_pct_from_entry": p[
                                    "highest_high_before_drop_pct_from_entry"
                                ],
                                "subsequent_lowest_low_price": p["subsequent_lowest_low_price"],
                                "drop_from_peak_pct": p["drop_from_peak_pct"],
                                "candles_from_peak_to_trough": p["candles_from_peak_to_trough"],
                                "minutes_from_peak_to_trough": p["minutes_from_peak_to_trough"],
                                "max_adverse_move_pct": p["max_adverse_move_pct"],
                                "max_favorable_move_pct": p["max_favorable_move_pct"],
                                "drop_over_adverse_ratio": p["drop_over_adverse_ratio"],
                                "is_march_06": e.is_march_06,
                            }
                        )

    path_horizon_metrics: list[dict[str, Any]] = []
    path_category_summary: list[dict[str, Any]] = []
    path_profile_mean: list[dict[str, Any]] = []
    path_profile_quantiles: list[dict[str, Any]] = []

    print("summarizing groups...", flush=True)
    for sample in ("full", "in_sample", "out_of_sample"):
        base = _filter_sample(events, sample)
        for group in PATH_GROUPS:
            xs = select_path_group(base, group)
            mode = _group_entry_mode(group)
            group_paths: list[dict[str, Any]] = []
            close_profiles = []
            run_adv_profiles = []
            run_fav_profiles = []
            cats = []
            for e in xs:
                for h in cfg.horizons:
                    p = path_cache.get((e.event_id, mode, int(h)))
                    if p is None or not p["complete_horizon"]:
                        continue
                    group_paths.append(p)
                p50 = path_cache.get((e.event_id, mode, int(cfg.profile_max)))
                if p50 is None or not p50["complete_horizon"]:
                    continue
                cats.append(classify_path_category(p50))
                cp = p50["close_path"]
                ap = p50["adverse_path"]
                fp = p50["favorable_path"]
                if len(cp) < cfg.profile_max:
                    continue
                close_profiles.append(cp[: cfg.profile_max])
                run_adv_profiles.append(np.maximum.accumulate(ap[: cfg.profile_max]))
                run_fav_profiles.append(np.maximum.accumulate(fp[: cfg.profile_max]))

            for h in cfg.horizons:
                path_horizon_metrics.append(_build_horizon_metrics(group, sample, group_paths, int(h)))

            ncat = len(cats)
            for cat in (
                "immediate_drop",
                "squeeze_then_drop",
                "deep_squeeze_then_drop",
                "squeeze_without_drop",
                "sideways_noise",
                "immediate_breakout",
            ):
                cnt = sum(1 for c in cats if c == cat)
                path_category_summary.append(
                    {
                        "group": group,
                        "sample": sample,
                        "path_category": cat,
                        "n": cnt,
                        "share_pct": None if ncat == 0 else 100.0 * cnt / ncat,
                        "group_n": ncat,
                    }
                )

            if close_profiles:
                C = np.vstack(close_profiles)
                A = np.vstack(run_adv_profiles)
                F = np.vstack(run_fav_profiles)
                for offset in range(cfg.profile_max):
                    path_profile_mean.append(
                        {
                            "group": group,
                            "sample": sample,
                            "offset_candle": offset + 1,
                            "offset_minutes": (offset + 1) * MINUTES_PER_CANDLE,
                            "mean_close_return_pct": float(np.mean(C[:, offset])),
                            "mean_running_max_adverse_pct": float(np.mean(A[:, offset])),
                            "mean_running_max_favorable_pct": float(np.mean(F[:, offset])),
                            "n": int(C.shape[0]),
                        }
                    )
                    path_profile_quantiles.append(
                        {
                            "group": group,
                            "sample": sample,
                            "offset_candle": offset + 1,
                            "offset_minutes": (offset + 1) * MINUTES_PER_CANDLE,
                            "p10_close_return_pct": float(np.percentile(C[:, offset], 10)),
                            "p25_close_return_pct": float(np.percentile(C[:, offset], 25)),
                            "p50_close_return_pct": float(np.percentile(C[:, offset], 50)),
                            "p75_close_return_pct": float(np.percentile(C[:, offset], 75)),
                            "p90_close_return_pct": float(np.percentile(C[:, offset], 90)),
                            "n": int(C.shape[0]),
                        }
                    )

    leverage_comparison = []
    for sample in ("full", "in_sample", "out_of_sample"):
        for lev, g_reclaim, g_sweep in (
            (50, "upper_50x__reclaim_within_3__T3", "upper_50x__sweep_only"),
            (25, "upper_25x__reclaim_within_3__T3", "upper_25x__sweep_only"),
        ):
            for g in (
                g_sweep,
                f"upper_{lev}x__immediate_reclaim",
                f"upper_{lev}x__reclaim_within_3",
                f"upper_{lev}x__reclaim_within_3__T1",
                g_reclaim,
            ):
                row = next(
                    (
                        r
                        for r in path_horizon_metrics
                        if r["group"] == g and r["sample"] == sample and r["horizon"] == 50
                    ),
                    None,
                )
                if row:
                    leverage_comparison.append({"leverage": lev, **row})

    trend_comparison = []
    for sample in ("full", "in_sample", "out_of_sample"):
        for lev in (50, 25):
            for g in (
                f"upper_{lev}x__reclaim_within_3",
                f"upper_{lev}x__reclaim_within_3__T1",
                f"upper_{lev}x__reclaim_within_3__T3",
                f"upper_{lev}x__reclaim_within_3__no_T3",
                f"upper_{lev}x__immediate_reclaim",
            ):
                row = next(
                    (
                        r
                        for r in path_horizon_metrics
                        if r["group"] == g and r["sample"] == sample and r["horizon"] == 50
                    ),
                    None,
                )
                if row:
                    trend_comparison.append(dict(row))

    # controls
    control_comparison: list[dict[str, Any]] = []
    if not cfg.skip_controls:
        print("matched controls path compare...", flush=True)
        sweep_idx = {e.candle_index for e in events}
        ctrl_src = [
            e
            for e in events
            if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
            and e.trend_t1
            and e.leverage in (50, 25)
        ]
        matched = match_controls(ctrl_src, data, sweep_idx, seed=cfg.seed)
        for sample in ("full", "in_sample", "out_of_sample"):
            for lev in (50, 25):
                evs = [
                    e
                    for e in _filter_sample(ctrl_src, sample)
                    if e.leverage == lev and e.entry_index is not None
                ]
                ctr = [
                    c
                    for c in matched
                    if c["leverage"] == lev and (sample == "full" or c["sample"] == sample)
                ]

                def pack(entries: list[tuple[int, float]]) -> dict[str, Any]:
                    advs, favs, drops, peak_first, t_peak, t_trough = [], [], [], 0, [], []
                    cats_c = {k: 0 for k in (
                        "immediate_drop",
                        "squeeze_then_drop",
                        "deep_squeeze_then_drop",
                        "squeeze_without_drop",
                        "sideways_noise",
                        "immediate_breakout",
                    )}
                    for ei, ep in entries:
                        p = analyze_short_path(
                            entry_index=ei,
                            entry_price=ep,
                            highs=highs,
                            lows=lows,
                            closes=closes,
                            timestamps=ts,
                            horizon=50,
                        )
                        if p is None or not p["complete_horizon"]:
                            continue
                        advs.append(p["max_adverse_move_pct"])
                        favs.append(p["max_favorable_move_pct"])
                        drops.append(p["drop_from_peak_pct"])
                        t_peak.append(p["minutes_to_max_adverse"])
                        t_trough.append(p["minutes_to_max_favorable"])
                        if p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"]:
                            peak_first += 1
                        cats_c[classify_path_category(p)] += 1
                    nloc = len(advs)
                    return {
                        "n": nloc,
                        "median_adverse": float(np.median(advs)) if advs else None,
                        "median_favorable": float(np.median(favs)) if favs else None,
                        "median_drop_from_peak": float(np.median(drops)) if drops else None,
                        "median_minutes_to_peak": float(np.median(t_peak)) if t_peak else None,
                        "median_minutes_to_trough": float(np.median(t_trough)) if t_trough else None,
                        "share_peak_then_trough_pct": None if nloc == 0 else 100.0 * peak_first / nloc,
                        "categories": cats_c,
                    }

                est = pack([(int(e.entry_index), float(e.entry_price)) for e in evs])
                cst = pack([(int(c["entry_index"]), float(c["entry_price"])) for c in ctr])
                control_comparison.append(
                    {
                        "group": f"upper_{lev}x_reclaim_T1",
                        "sample": sample,
                        "event_n": est["n"],
                        "control_n": cst["n"],
                        "event_median_adverse_h50": est["median_adverse"],
                        "control_median_adverse_h50": cst["median_adverse"],
                        "event_median_favorable_h50": est["median_favorable"],
                        "control_median_favorable_h50": cst["median_favorable"],
                        "event_median_drop_from_peak_h50": est["median_drop_from_peak"],
                        "control_median_drop_from_peak_h50": cst["median_drop_from_peak"],
                        "event_median_minutes_to_peak": est["median_minutes_to_peak"],
                        "control_median_minutes_to_peak": cst["median_minutes_to_peak"],
                        "event_median_minutes_to_trough": est["median_minutes_to_trough"],
                        "control_median_minutes_to_trough": cst["median_minutes_to_trough"],
                        "event_share_peak_then_trough_pct": est["share_peak_then_trough_pct"],
                        "control_share_peak_then_trough_pct": cst["share_peak_then_trough_pct"],
                        "event_categories": str(est["categories"]),
                        "control_categories": str(cst["categories"]),
                        "note": "empirical comparison only; not a significance claim",
                    }
                )

    def answers_for(sample: str, group: str) -> dict[str, Any]:
        row = next(
            (
                r
                for r in path_horizon_metrics
                if r["group"] == group and r["sample"] == sample and r["horizon"] == 50
            ),
            None,
        )
        return row or {}

    def build_summary(sample: str) -> dict[str, Any]:
        return {
            "sample": sample,
            "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations.",
            "upper_50x_immediate_reclaim_h50": answers_for(sample, "upper_50x__immediate_reclaim"),
            "upper_50x_reclaim_T3_h50": answers_for(sample, "upper_50x__reclaim_within_3__T3"),
            "upper_25x_immediate_reclaim_h50": answers_for(sample, "upper_25x__immediate_reclaim"),
            "upper_25x_reclaim_T3_h50": answers_for(sample, "upper_25x__reclaim_within_3__T3"),
            "upper_50x_sweep_only_h50": answers_for(sample, "upper_50x__sweep_only"),
            "upper_25x_sweep_only_h50": answers_for(sample, "upper_25x__sweep_only"),
            "hedge_bot_note": (
                "No classical stop-loss scoring. Useful only as path-context: "
                "typical further squeeze size/timing and subsequent drop-from-peak."
            ),
        }

    def march_summary(flag_06: bool | None) -> dict[str, Any]:
        rows = []
        for e in events:
            if not e.is_march_window:
                continue
            if flag_06 is True and not e.is_march_06:
                continue
            if e.leverage not in (50, 25):
                continue
            if e.exclusive_reclaim_group not in {"immediate_reclaim", "delayed_reclaim_1_to_3"}:
                continue
            ent = event_entry(e, "reclaim")
            if not ent:
                continue
            p = analyze_short_path(
                entry_index=ent[0],
                entry_price=ent[1],
                highs=highs,
                lows=lows,
                closes=closes,
                timestamps=ts,
                horizon=50,
            )
            if p is None or not p["complete_horizon"]:
                continue
            rows.append(
                {
                    "event_id": e.event_id,
                    "leverage": e.leverage,
                    "trend_t3": e.trend_t3,
                    "is_march_06": e.is_march_06,
                    "category": classify_path_category(p),
                    "max_adverse_move_pct": p["max_adverse_move_pct"],
                    "max_favorable_move_pct": p["max_favorable_move_pct"],
                    "drop_from_peak_pct": p["drop_from_peak_pct"],
                    "order": p["order"],
                    "minutes_to_max_adverse": p["minutes_to_max_adverse"],
                    "minutes_from_peak_to_trough": p["minutes_from_peak_to_trough"],
                }
            )
        if not rows:
            return {"n": 0, "window": f"{MARCH_START}..{MARCH_END}", "flag_06": flag_06}
        adv = [r["max_adverse_move_pct"] for r in rows]
        fav = [r["max_favorable_move_pct"] for r in rows]
        drop = [r["drop_from_peak_pct"] for r in rows]
        return {
            "n": len(rows),
            "window": f"{MARCH_START}..{MARCH_END}",
            "flag_06": flag_06,
            "n_march_06": sum(1 for r in rows if r["is_march_06"]),
            "median_adverse_h50": float(np.median(adv)),
            "p75_adverse_h50": float(np.percentile(adv, 75)),
            "p90_adverse_h50": float(np.percentile(adv, 90)),
            "median_favorable_h50": float(np.median(fav)),
            "median_drop_from_peak_h50": float(np.median(drop)),
            "share_peak_then_trough_pct": 100.0
            * sum(1 for r in rows if r["order"] != "favorable_trough_before_adverse_peak")
            / len(rows),
            "categories": {
                k: sum(1 for r in rows if r["category"] == k)
                for k in (
                    "immediate_drop",
                    "squeeze_then_drop",
                    "deep_squeeze_then_drop",
                    "squeeze_without_drop",
                    "sideways_noise",
                    "immediate_breakout",
                )
            },
            "disclaimer": "Estimated LuxAlgo levels; not real exchange liquidations.",
        }

    meta = {
        "n_candles": n,
        "in_sample_cut": in_sample_cut(n, cfg.in_sample_fraction),
        "start_timestamp": str(data.iloc[0]["timestamp"]),
        "end_timestamp": str(data.iloc[-1]["timestamp"]),
        "n_events": len(events),
        "n_path_event_rows": len(path_events),
        "event_counts": {
            "upper_50x": sum(1 for e in events if e.leverage == 50),
            "upper_25x": sum(1 for e in events if e.leverage == 25),
        },
    }
    return PathAuditBundle(
        config=cfg,
        events=events,
        path_events=path_events,
        path_horizon_metrics=path_horizon_metrics,
        peak_then_trough_events=peak_then_trough_events,
        path_category_events=path_category_events,
        path_category_summary=path_category_summary,
        path_profile_mean=path_profile_mean,
        path_profile_quantiles=path_profile_quantiles,
        leverage_comparison=leverage_comparison,
        trend_comparison=trend_comparison,
        control_comparison=control_comparison,
        summary_full=build_summary("full"),
        summary_in_sample=build_summary("in_sample"),
        summary_out_of_sample=build_summary("out_of_sample"),
        summary_march=march_summary(None),
        summary_march_06=march_summary(True),
        meta=meta,
    )
