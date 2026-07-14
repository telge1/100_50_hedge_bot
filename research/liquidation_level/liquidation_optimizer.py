"""Causal liquidation-level config grid optimizer (path-context focus).

Does not search for a classical trading edge. Ranks configurations by robust
post-reclaim squeeze→drop path behaviour on estimated LuxAlgo-style levels.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_backtest import assign_sample, in_sample_cut
from research.liquidation_level.liquidation_config import (
    CODE_SCHEMA_VERSION,
    BASELINE_CONFIG_NAME,
    baseline_config,
    build_refinement_grid,
    cluster_evaluation_key,
    config_hash,
    config_to_canonical_dict,
    expand_grid_configurations,
    grid_axis_counts,
    level_generation_key,
    liquidation_config_from_mapping,
    load_optimizer_grid,
    save_liquidation_config,
    select_screening_configurations,
)
from research.liquidation_level.liquidation_levels import (
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevelConfig,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
    replay_liquidation_levels,
)
from research.liquidation_level.short_squeeze_continuation_audit import (
    _find_bearish_reclaim,
)
from research.liquidation_level.short_squeeze_path_audit import (
    analyze_short_path,
    classify_path_category,
)

SCHEMA = CODE_SCHEMA_VERSION


@dataclass
class OptimizerEvaluationSpec:
    target_leverages: tuple[int, ...] = (25, 50, 100)
    reclaim_groups: tuple[str, ...] = ("immediate_reclaim", "reclaim_within_3")
    horizons: tuple[int, ...] = (10, 20, 30, 40, 50)
    minimum_full_events: int = 200
    minimum_oos_events: int = 50
    minimum_events_per_month: int = 10
    minimum_months_with_events: int = 3
    seed: int = 42
    is_oos_peak_drop_max_abs_diff: float = 1.0
    oos_peak_before_trough_min_ratio: float = 0.75
    top_n_controls: int = 10
    path_horizon: int = 50


def evaluation_from_grid(grid_cfg: Mapping[str, Any]) -> OptimizerEvaluationSpec:
    ev = dict(grid_cfg.get("evaluation") or {})
    return OptimizerEvaluationSpec(
        target_leverages=tuple(int(x) for x in ev.get("target_leverages", (25, 50, 100))),
        reclaim_groups=tuple(ev.get("reclaim_groups", ("immediate_reclaim", "reclaim_within_3"))),
        horizons=tuple(int(x) for x in ev.get("horizons", (10, 20, 30, 40, 50))),
        minimum_full_events=int(ev.get("minimum_full_events", 200)),
        minimum_oos_events=int(ev.get("minimum_oos_events", 50)),
        minimum_events_per_month=int(ev.get("minimum_events_per_month", 10)),
        minimum_months_with_events=int(ev.get("minimum_months_with_events", 3)),
        seed=int(ev.get("seed", 42)),
        is_oos_peak_drop_max_abs_diff=float(ev.get("is_oos_peak_drop_max_abs_diff", 1.0)),
        oos_peak_before_trough_min_ratio=float(ev.get("oos_peak_before_trough_min_ratio", 0.75)),
        top_n_controls=int(ev.get("top_n_controls", 10)),
        path_horizon=int((grid_cfg.get("fixed") or {}).get("path_horizon_candles", 50)),
    )


def data_fingerprint(ohlcv: pd.DataFrame) -> dict[str, Any]:
    data = normalize_ohlcv_dataframe(ohlcv)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    closes = data["close"].to_numpy(float)
    # Stable content fingerprint (not path-dependent)
    h = hashlib.sha256()
    h.update(str(len(data)).encode())
    h.update(str(ts.iloc[0]).encode())
    h.update(str(ts.iloc[-1]).encode())
    h.update(np.asarray(closes[:50], dtype=float).tobytes())
    h.update(np.asarray(closes[-50:], dtype=float).tobytes())
    h.update(np.asarray(closes[:: max(1, len(closes) // 20)], dtype=float).tobytes())
    return {
        "n_candles": int(len(data)),
        "start_timestamp": str(ts.iloc[0]),
        "end_timestamp": str(ts.iloc[-1]),
        "content_sha256_16": h.hexdigest()[:16],
        "in_sample_cut": int(in_sample_cut(len(data))),
        "code_schema_version": SCHEMA,
    }


def _pct_stats(vals: Sequence[float], prefix: str) -> dict[str, Any]:
    if not vals:
        return {
            f"{prefix}_mean": None,
            f"{prefix}_median": None,
            f"{prefix}_p75": None,
            f"{prefix}_p90": None,
            f"{prefix}_p95": None,
            f"{prefix}_n": 0,
        }
    arr = np.asarray(vals, float)
    return {
        f"{prefix}_mean": float(np.mean(arr)),
        f"{prefix}_median": float(np.median(arr)),
        f"{prefix}_p75": float(np.percentile(arr, 75)),
        f"{prefix}_p90": float(np.percentile(arr, 90)),
        f"{prefix}_p95": float(np.percentile(arr, 95)),
        f"{prefix}_n": int(len(arr)),
    }


@dataclass
class _LiteEvent:
    event_id: str
    candle_index: int
    leverage: int
    exclusive_reclaim_group: str
    entry_index: int | None
    entry_price: float | None
    sample: str
    timestamp: pd.Timestamp
    event_high: float
    event_low: float


def build_lite_upper_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    *,
    allowed_leverages: Sequence[int],
    reclaim_window_candles: int,
) -> list[_LiteEvent]:
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    lev_ok = set(int(x) for x in allowed_leverages)

    by_candle: dict[int, list] = {}
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        if lvl.side != SIDE_UPPER or int(lvl.leverage) not in lev_ok:
            continue
        by_candle.setdefault(int(lvl.swept_index), []).append(lvl)

    events: list[_LiteEvent] = []
    seq = 0
    for i, lvls in sorted(by_candle.items()):
        for lvl in lvls:
            _, exclusive, reclaim_i, _delay = _find_bearish_reclaim(
                level_price=float(lvl.level_price),
                event_close=float(closes[i]),
                closes=closes,
                sweep_index=i,
                reclaim_window_candles=int(reclaim_window_candles),
            )
            entry_index = None
            entry_price = None
            if exclusive == "immediate_reclaim":
                entry_index = i + 1
            elif exclusive == "delayed_reclaim_1_to_3" and reclaim_i is not None:
                entry_index = reclaim_i + 1
            if entry_index is not None and entry_index < n:
                entry_price = float(opens[entry_index])
            else:
                entry_index = None
                entry_price = None
            seq += 1
            events.append(
                _LiteEvent(
                    event_id=f"OPT_{seq:06d}",
                    candle_index=i,
                    leverage=int(lvl.leverage),
                    exclusive_reclaim_group=exclusive,
                    entry_index=entry_index,
                    entry_price=entry_price,
                    sample=assign_sample(i, n),
                    timestamp=ts.iloc[i],
                    event_high=float(highs[i]),
                    event_low=float(lows[i]),
                )
            )
    return events


def _filter_group(events: Sequence[_LiteEvent], leverage: int, reclaim_group: str) -> list[_LiteEvent]:
    xs = [e for e in events if e.leverage == leverage]
    if reclaim_group == "immediate_reclaim":
        return [e for e in xs if e.exclusive_reclaim_group == "immediate_reclaim"]
    if reclaim_group == "reclaim_within_3":
        return [
            e
            for e in xs
            if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
        ]
    return xs


def summarize_paths_for_events(
    events: Sequence[_LiteEvent],
    *,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: pd.Series,
    horizon: int,
    sample: str,
) -> dict[str, Any]:
    if sample == "full":
        xs = list(events)
    else:
        xs = [e for e in events if e.sample == sample]
    advs, favs, drops, t_peak, t_pt = [], [], [], [], []
    cats = {
        "immediate_drop": 0,
        "squeeze_then_drop": 0,
        "deep_squeeze_then_drop": 0,
        "squeeze_without_drop": 0,
        "sideways_noise": 0,
        "immediate_breakout": 0,
    }
    peak_first = 0
    months: dict[str, int] = {}
    for e in xs:
        if e.entry_index is None or e.entry_price is None:
            continue
        p = analyze_short_path(
            entry_index=int(e.entry_index),
            entry_price=float(e.entry_price),
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=timestamps,
            horizon=int(horizon),
        )
        if p is None or not p["complete_horizon"]:
            continue
        advs.append(float(p["max_adverse_move_pct"]))
        favs.append(float(p["max_favorable_move_pct"]))
        drops.append(float(p["drop_from_peak_pct"]))
        t_peak.append(float(p["minutes_to_max_adverse"]))
        t_pt.append(float(p["minutes_from_peak_to_trough"]))
        if p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"]:
            peak_first += 1
        cats[classify_path_category(p)] += 1
        months[str(pd.Timestamp(e.timestamp).strftime("%Y-%m"))] = (
            months.get(str(pd.Timestamp(e.timestamp).strftime("%Y-%m")), 0) + 1
        )
    n = len(advs)
    month_counts = list(months.values())
    out = {
        "n": n,
        "sweep_or_reclaim_candidates": len(xs),
        "peak_before_trough_rate": None if n == 0 else 100.0 * peak_first / n,
        "immediate_drop_rate": None if n == 0 else 100.0 * cats["immediate_drop"] / n,
        "squeeze_then_drop_rate": None if n == 0 else 100.0 * cats["squeeze_then_drop"] / n,
        "deep_squeeze_then_drop_rate": None if n == 0 else 100.0 * cats["deep_squeeze_then_drop"] / n,
        "squeeze_without_drop_rate": None if n == 0 else 100.0 * cats["squeeze_without_drop"] / n,
        "breakout_rate": None if n == 0 else 100.0 * cats["immediate_breakout"] / n,
        "sideways_rate": None if n == 0 else 100.0 * cats["sideways_noise"] / n,
        "median_minutes_to_peak": float(np.median(t_peak)) if t_peak else None,
        "median_minutes_peak_to_trough": float(np.median(t_pt)) if t_pt else None,
        "months_with_events": len(months),
        "min_events_per_month": int(min(month_counts)) if month_counts else 0,
        "median_events_per_month": float(np.median(month_counts)) if month_counts else 0.0,
        "month_counts": months,
        **_pct_stats(advs, "adverse"),
        **_pct_stats(favs, "favorable"),
        **_pct_stats(drops, "peak_drop"),
    }
    # aliases requested in spec
    for src, dst in (
        ("adverse", "adverse"),
        ("favorable", "favorable"),
        ("peak_drop", "peak_drop"),
    ):
        pass
    return out


def evaluate_configuration(
    config: LiquidationLevelConfig,
    ohlcv: pd.DataFrame,
    *,
    spec: OptimizerEvaluationSpec,
    replay: LiquidationReplayResult | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    data = normalize_ohlcv_dataframe(ohlcv)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)

    if replay is None:
        replay = replay_liquidation_levels(data, config)

    target_levs = [lev for lev in spec.target_leverages if lev in set(config.leverages)]
    # still count sweeps for all config leverages overlapping targets
    events = build_lite_upper_events(
        replay,
        data,
        allowed_leverages=tuple(sorted(set(config.leverages) | set(spec.target_leverages))),
        reclaim_window_candles=int(config.reclaim_window_candles),
    )

    row: dict[str, Any] = {
        "config_id": config_hash(config),
        "level_generation_key": level_generation_key(config),
        "cluster_evaluation_key": cluster_evaluation_key(config),
        "config": config_to_canonical_dict(config),
        "elapsed_s": None,
        "metrics": {},
        "rejection_reasons": [],
        "eligible_for_ranking": False,
        "robustness_flags": {},
    }

    # event counts overall
    for lev in spec.target_leverages:
        sweeps = [e for e in events if e.leverage == lev]
        imm = [e for e in sweeps if e.exclusive_reclaim_group == "immediate_reclaim"]
        within = [
            e
            for e in sweeps
            if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
        ]
        row["metrics"][f"upper_{lev}x_sweep_events"] = len(sweeps)
        row["metrics"][f"upper_{lev}x_immediate_reclaim_events"] = len(imm)
        row["metrics"][f"upper_{lev}x_reclaim_within_3_events"] = len(within)

    primary_horizon = int(spec.path_horizon)
    for lev in spec.target_leverages:
        for rg in spec.reclaim_groups:
            group_events = _filter_group(events, lev, rg)
            for sample in ("full", "in_sample", "out_of_sample"):
                for h in spec.horizons:
                    key = f"upper_{lev}x__{rg}__{sample}__h{h}"
                    row["metrics"][key] = summarize_paths_for_events(
                        group_events,
                        highs=highs,
                        lows=lows,
                        closes=closes,
                        timestamps=ts,
                        horizon=int(h),
                        sample=sample,
                    )

    # Robustness using primary group: upper_50x immediate reclaim if present else first target
    primary_lev = 50 if 50 in spec.target_leverages else spec.target_leverages[0]
    primary_rg = "immediate_reclaim" if "immediate_reclaim" in spec.reclaim_groups else spec.reclaim_groups[0]
    full_m = row["metrics"].get(f"upper_{primary_lev}x__{primary_rg}__full__h{primary_horizon}", {})
    is_m = row["metrics"].get(f"upper_{primary_lev}x__{primary_rg}__in_sample__h{primary_horizon}", {})
    oos_m = row["metrics"].get(f"upper_{primary_lev}x__{primary_rg}__out_of_sample__h{primary_horizon}", {})

    reasons: list[str] = []
    if int(full_m.get("n") or 0) < spec.minimum_full_events:
        reasons.append(f"full_events<{spec.minimum_full_events}")
    if int(oos_m.get("n") or 0) < spec.minimum_oos_events:
        reasons.append(f"oos_events<{spec.minimum_oos_events}")
    months_ok = 0
    for m, c in (full_m.get("month_counts") or {}).items():
        if int(c) >= spec.minimum_events_per_month:
            months_ok += 1
    if months_ok < spec.minimum_months_with_events:
        reasons.append(f"months_with_enough_events<{spec.minimum_months_with_events}")

    is_drop = is_m.get("peak_drop_median")
    oos_drop = oos_m.get("peak_drop_median")
    if is_drop is not None and oos_drop is not None:
        if abs(float(is_drop) - float(oos_drop)) > spec.is_oos_peak_drop_max_abs_diff:
            reasons.append("is_oos_peak_drop_median_divergence")
    is_pbt = is_m.get("peak_before_trough_rate")
    oos_pbt = oos_m.get("peak_before_trough_rate")
    if is_pbt and oos_pbt is not None and float(is_pbt) > 0:
        if float(oos_pbt) < float(is_pbt) * spec.oos_peak_before_trough_min_ratio:
            reasons.append("oos_peak_before_trough_collapse")

    # thin secondary leverage
    for lev in (25, 50):
        if lev in spec.target_leverages:
            n25 = row["metrics"].get(f"upper_{lev}x__{primary_rg}__full__h{primary_horizon}", {}).get("n") or 0
            if int(n25) < 30:
                reasons.append(f"upper_{lev}x_too_few_events")

    row["rejection_reasons"] = reasons
    row["eligible_for_ranking"] = len(reasons) == 0
    row["robustness_flags"] = {
        "primary_leverage": primary_lev,
        "primary_reclaim_group": primary_rg,
        "full_n": full_m.get("n"),
        "is_n": is_m.get("n"),
        "oos_n": oos_m.get("n"),
        "months_ok": months_ok,
    }
    row["elapsed_s"] = time.perf_counter() - t0

    # flat helper fields for CSV ranking
    row["is_peak_drop_median"] = is_m.get("peak_drop_median")
    row["oos_peak_drop_median"] = oos_m.get("peak_drop_median")
    row["full_peak_drop_median"] = full_m.get("peak_drop_median")
    row["is_adverse_median"] = is_m.get("adverse_median")
    row["oos_adverse_median"] = oos_m.get("adverse_median")
    row["is_peak_before_trough_rate"] = is_m.get("peak_before_trough_rate")
    row["oos_peak_before_trough_rate"] = oos_m.get("peak_before_trough_rate")
    row["is_squeeze_then_drop_rate"] = is_m.get("squeeze_then_drop_rate")
    row["oos_squeeze_then_drop_rate"] = oos_m.get("squeeze_then_drop_rate")
    row["is_breakout_rate"] = is_m.get("breakout_rate")
    row["oos_breakout_rate"] = oos_m.get("breakout_rate")
    row["full_n"] = full_m.get("n")
    row["oos_n"] = oos_m.get("n")
    row["is_n"] = is_m.get("n")
    row["is_median_minutes_to_peak"] = is_m.get("median_minutes_to_peak")
    return row


def oos_confirmation_status(row: dict[str, Any], spec: OptimizerEvaluationSpec) -> str:
    if int(row.get("oos_n") or 0) < spec.minimum_oos_events:
        return "insufficient_sample"
    is_drop = row.get("is_peak_drop_median")
    oos_drop = row.get("oos_peak_drop_median")
    is_pbt = row.get("is_peak_before_trough_rate")
    oos_pbt = row.get("oos_peak_before_trough_rate")
    if None in (is_drop, oos_drop, is_pbt, oos_pbt):
        return "insufficient_sample"
    drop_ok = abs(float(is_drop) - float(oos_drop)) <= spec.is_oos_peak_drop_max_abs_diff
    pbt_ok = float(oos_pbt) >= float(is_pbt) * spec.oos_peak_before_trough_min_ratio
    squeeze_is = float(row.get("is_squeeze_then_drop_rate") or 0)
    squeeze_oos = float(row.get("oos_squeeze_then_drop_rate") or 0)
    squeeze_dir = squeeze_oos >= squeeze_is * 0.7
    if drop_ok and pbt_ok and squeeze_dir:
        return "confirmed"
    if drop_ok or pbt_ok:
        return "directionally_confirmed"
    return "not_confirmed"


def _score_robust_path(row: dict[str, Any]) -> float:
    # Higher is better; IS-only inputs for ranking
    if not row.get("eligible_for_ranking"):
        return -1e18
    return (
        float(row.get("is_peak_before_trough_rate") or 0)
        + float(row.get("is_squeeze_then_drop_rate") or 0)
        + 0.5 * float(row.get("is_peak_drop_median") or 0)
        - 1.5 * float(row.get("is_breakout_rate") or 0)
        + 0.01 * float(row.get("is_n") or 0)
    )


def _score_sensitivity(row: dict[str, Any], baseline_n: float) -> float:
    if not row.get("eligible_for_ranking"):
        return -1e18
    n_gain = float(row.get("full_n") or 0) - baseline_n
    return (
        0.02 * n_gain
        + float(row.get("is_peak_before_trough_rate") or 0)
        + 0.3 * float(row.get("is_peak_drop_median") or 0)
        - 0.2 * abs(float(row.get("is_adverse_median") or 0) - float(row.get("oos_adverse_median") or 0))
    )


def _score_hedge_context(row: dict[str, Any]) -> float:
    if not row.get("eligible_for_ranking"):
        return -1e18
    adv = float(row.get("is_adverse_median") or 0)
    drop = float(row.get("is_peak_drop_median") or 0)
    tpeak = float(row.get("is_median_minutes_to_peak") or 0)
    # prefer measurable squeeze + larger subsequent drop + stable mid timing
    timing_pen = abs(tpeak - 90.0) * 0.02
    return adv + drop + 0.3 * float(row.get("is_peak_before_trough_rate") or 0) - timing_pen


class LevelReplayCache:
    """Bounded in-memory replay cache (server-safe).

    Full LiquidationReplayResult objects are large. Keep at most ``max_mem_entries``
    (default 1) so RAM cannot grow with hundreds of configs. Disk stores metadata only.
    """

    def __init__(
        self,
        cache_dir: Path,
        fingerprint: Mapping[str, Any],
        *,
        max_mem_entries: int = 1,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fingerprint = dict(fingerprint)
        self.hits = 0
        self.misses = 0
        self.max_mem_entries = max(1, int(max_mem_entries))
        self._mem: dict[str, LiquidationReplayResult] = {}
        self._order: list[str] = []

    def _meta_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.meta.json"

    def clear_memory(self) -> None:
        self._mem.clear()
        self._order.clear()

    def get_by_key(self, key: str) -> LiquidationReplayResult | None:
        if key in self._mem:
            self.hits += 1
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            return self._mem[key]
        self.misses += 1
        return None

    def get(self, config: LiquidationLevelConfig) -> LiquidationReplayResult | None:
        return self.get_by_key(level_generation_key(config))

    def put(self, config: LiquidationLevelConfig, replay: LiquidationReplayResult) -> None:
        import gc

        key = level_generation_key(config)
        if key in self._mem:
            self._mem[key] = replay
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
        else:
            while len(self._order) >= self.max_mem_entries:
                old = self._order.pop(0)
                self._mem.pop(old, None)
            self._mem[key] = replay
            self._order.append(key)
            gc.collect()
        meta = {
            "level_generation_key": key,
            "config_level_fields": {k: config_to_canonical_dict(config)[k] for k in (
                "reference_price",
                "volume_sma_period",
                "volume_threshold",
                "volatility_threshold",
                "leverages",
                "max_active_levels",
                "minimum_move_divisor",
                "sweep_strict_cross",
            )},
            "data_fingerprint": self.fingerprint,
            "code_schema_version": SCHEMA,
            "n_levels": len(replay.all_levels),
            "n_candles": self.fingerprint.get("n_candles"),
        }
        tmp = self._meta_path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._meta_path(key))


def estimate_dry_run(
    grid_cfg: Mapping[str, Any],
    *,
    output_dir: Path,
    max_configs: int | None,
    feather: Path,
    ohlcv: pd.DataFrame | None = None,
) -> dict[str, Any]:
    axes = grid_axis_counts(grid_cfg)
    all_cfgs = expand_grid_configurations(grid_cfg)
    mode = str(grid_cfg.get("search_mode") or "screening")
    screen_max = int((grid_cfg.get("screening") or {}).get("max_configs", 200))
    if max_configs is not None:
        screen_max = int(max_configs)
    if mode == "grid":
        planned = all_cfgs
    else:
        planned = select_screening_configurations(
            all_cfgs,
            max_configs=screen_max,
            seed=int((grid_cfg.get("evaluation") or {}).get("seed", 42)),
            always_include_baseline=bool(
                (grid_cfg.get("screening") or {}).get("always_include_baseline", True)
            ),
        )
    # unique level generation keys among planned
    gen_keys = {level_generation_key(c) for c in planned}
    probe_s = None
    if ohlcv is not None and len(ohlcv) > 0:
        t0 = time.perf_counter()
        replay_liquidation_levels(ohlcv, baseline_config())
        probe_s = time.perf_counter() - t0
    est_runtime = None if probe_s is None else probe_s * len(gen_keys) * 1.15
    resume_exists = (Path(output_dir) / "completed_configurations.jsonl").exists()
    return {
        "axis_counts": axes,
        "full_grid_combinations": len(all_cfgs),
        "search_mode": mode,
        "planned_configurations": len(planned),
        "unique_level_replays": len(gen_keys),
        "estimated_level_replays": len(gen_keys),
        "baseline_probe_seconds": probe_s,
        "estimated_runtime_seconds": est_runtime,
        "estimated_runtime_hours": None if est_runtime is None else est_runtime / 3600.0,
        "estimated_memory_note": (
            "In-memory replay cache keyed by level_generation_key; "
            "peak RAM scales with unique leverage/volume configs, not cluster axes."
        ),
        "output_dir": str(output_dir),
        "resume_data_present": resume_exists,
        "feather_file": str(feather),
    }


def _append_jsonl(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def _load_completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ids.add(json.loads(line)["config_id"])
        except Exception:
            continue
    return ids


def _flatten_row_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    cfg = row.get("config") or {}
    out = {
        "config_id": row.get("config_id"),
        "eligible_for_ranking": row.get("eligible_for_ranking"),
        "rejection_reasons": "|".join(row.get("rejection_reasons") or []),
        "elapsed_s": row.get("elapsed_s"),
        "level_generation_key": row.get("level_generation_key"),
        "cluster_evaluation_key": row.get("cluster_evaluation_key"),
        "is_n": row.get("is_n"),
        "oos_n": row.get("oos_n"),
        "full_n": row.get("full_n"),
        "is_adverse_median": row.get("is_adverse_median"),
        "oos_adverse_median": row.get("oos_adverse_median"),
        "is_peak_drop_median": row.get("is_peak_drop_median"),
        "oos_peak_drop_median": row.get("oos_peak_drop_median"),
        "full_peak_drop_median": row.get("full_peak_drop_median"),
        "is_peak_before_trough_rate": row.get("is_peak_before_trough_rate"),
        "oos_peak_before_trough_rate": row.get("oos_peak_before_trough_rate"),
        "is_squeeze_then_drop_rate": row.get("is_squeeze_then_drop_rate"),
        "oos_squeeze_then_drop_rate": row.get("oos_squeeze_then_drop_rate"),
        "is_breakout_rate": row.get("is_breakout_rate"),
        "oos_breakout_rate": row.get("oos_breakout_rate"),
        "is_median_minutes_to_peak": row.get("is_median_minutes_to_peak"),
        "oos_confirmation_status": row.get("oos_confirmation_status"),
        "is_rank_score": row.get("is_rank_score"),
        **{f"cfg_{k}": cfg.get(k) for k in (
            "reference_price",
            "volume_threshold",
            "volatility_threshold",
            "leverages",
            "cluster_distance_pct",
            "cluster_min_level_count",
            "cluster_min_total_strength",
        )},
    }
    return out


def run_optimizer(
    *,
    grid_cfg: Mapping[str, Any],
    ohlcv: pd.DataFrame,
    output_dir: Path,
    max_configs: int | None = None,
    start_config_index: int = 0,
    end_config_index: int | None = None,
    resume: bool = False,
    retry_failed: bool = False,
    workers: int = 1,
    seed: int = 42,
    baseline_only: bool = False,
    progress_every: int = 1,
    batch_size: int | None = None,
    max_mem_cache: int = 1,
    skip_controls: bool = False,
) -> dict[str, Any]:
    import gc

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = out / "cache" / "level_replays"
    spec = evaluation_from_grid(grid_cfg)
    if seed:
        spec.seed = int(seed)

    data = normalize_ohlcv_dataframe(ohlcv)
    fp = data_fingerprint(data)
    (out / "data_fingerprint.json").write_text(json.dumps(fp, indent=2) + "\n", encoding="utf-8")
    (out / "grid_config_used.json").write_text(
        json.dumps(grid_cfg, indent=2) + "\n", encoding="utf-8"
    )

    all_cfgs = expand_grid_configurations(grid_cfg)
    mode = str(grid_cfg.get("search_mode") or "screening")
    if baseline_only:
        planned = [baseline_config()]
    elif mode == "grid":
        planned = list(all_cfgs)
    else:
        screen_max = int((grid_cfg.get("screening") or {}).get("max_configs", 200))
        if max_configs is not None:
            screen_max = int(max_configs)
        planned = select_screening_configurations(
            all_cfgs,
            max_configs=screen_max,
            seed=spec.seed,
            always_include_baseline=True,
        )

    planned = planned[start_config_index: end_config_index if end_config_index is not None else None]

    completed_path = out / "completed_configurations.jsonl"
    failed_path = out / "failed_configurations.jsonl"
    progress_path = out / "optimizer_progress.json"
    completed_ids = _load_completed_ids(completed_path) if resume else set()
    failed_ids = _load_completed_ids(failed_path) if resume and not retry_failed else set()

    # validate resume fingerprint
    if resume and (out / "data_fingerprint.json").exists():
        old = json.loads((out / "data_fingerprint.json").read_text(encoding="utf-8"))
        if old.get("content_sha256_16") != fp.get("content_sha256_16"):
            raise RuntimeError("resume refused: data fingerprint mismatch")

    cache = LevelReplayCache(cache_dir, fp, max_mem_entries=max_mem_cache)
    results: list[dict[str, Any]] = []
    # reload completed rows for ranking if resume
    if resume and completed_path.exists():
        for line in completed_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    results.append(json.loads(line))
                except Exception:
                    pass

    manifest = {
        "experiment_name": grid_cfg.get("experiment_name"),
        "search_mode": mode,
        "planned": len(planned),
        "full_grid": len(all_cfgs),
        "workers": workers,
        "seed": spec.seed,
        "code_schema_version": SCHEMA,
        "baseline_config_id": config_hash(baseline_config()),
        "batch_size": batch_size,
        "max_mem_cache": max_mem_cache,
        "skip_controls": skip_controls,
    }
    (out / "optimizer_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Build todo list; group by level key so tiny mem-cache still hits
    todo: list[LiquidationLevelConfig] = []
    for cfg in planned:
        cid = config_hash(cfg)
        if cid in completed_ids:
            continue
        if cid in failed_ids and not retry_failed:
            continue
        todo.append(cfg)
    todo.sort(key=lambda c: (level_generation_key(c), config_hash(c)))
    if batch_size is not None and int(batch_size) > 0:
        todo = todo[: int(batch_size)]

    print(
        f"optimizer planned={len(planned)} todo={len(todo)} "
        f"resume_completed={len(completed_ids)} max_mem_cache={max_mem_cache}",
        flush=True,
    )

    def _run_one(cfg: LiquidationLevelConfig) -> dict[str, Any]:
        lgk = level_generation_key(cfg)
        replay = cache.get_by_key(lgk)
        cache_hit = replay is not None
        if replay is None:
            replay = replay_liquidation_levels(data, cfg)
            cache.put(cfg, replay)
        row = evaluate_configuration(cfg, data, spec=spec, replay=replay)
        row["cache_hit"] = cache_hit
        row["oos_confirmation_status"] = oos_confirmation_status(row, spec)
        return row

    if workers <= 1:
        for i, cfg in enumerate(todo):
            try:
                row = _run_one(cfg)
                results.append(row)
                slim = {k: v for k, v in row.items() if k != "metrics"}
                slim["metrics_summary"] = {
                    k: row["metrics"][k]
                    for k in row["metrics"]
                    if k.endswith(f"__h{spec.path_horizon}") or k.endswith("_events")
                }
                _append_jsonl(completed_path, slim)
                completed_ids.add(row["config_id"])
            except Exception as exc:
                fail = {
                    "config_id": config_hash(cfg),
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "config": config_to_canonical_dict(cfg),
                }
                _append_jsonl(failed_path, fail)
            # Drop replay ASAP after each config unless next shares the same key
            next_same = False
            if i + 1 < len(todo):
                next_same = level_generation_key(todo[i + 1]) == level_generation_key(cfg)
            if not next_same:
                cache.clear_memory()
                gc.collect()
            progress = {
                "done": len(completed_ids),
                "failed": sum(1 for _ in failed_path.open()) if failed_path.exists() else 0,
                "todo_remaining_batch": len(todo) - (i + 1),
                "cache_hits": cache.hits,
                "cache_misses": cache.misses,
                "last_config_id": config_hash(cfg),
            }
            progress_path.write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")
            if progress_every and (i + 1) % progress_every == 0:
                print(f"progress {i+1}/{len(todo)} done_total={len(completed_ids)} cache_hits={cache.hits}", flush=True)
    else:
        # Process pool: each worker recomputes (no shared mem cache across processes).
        # Deterministic order of submission; collect then sort by config_id.
        with ProcessPoolExecutor(max_workers=int(workers)) as ex:
            futs = {
                ex.submit(evaluate_configuration, cfg, data, spec=spec, replay=None): cfg for cfg in todo
            }
            for fut in as_completed(futs):
                cfg = futs[fut]
                try:
                    row = fut.result()
                    row["oos_confirmation_status"] = oos_confirmation_status(row, spec)
                    row["cache_hit"] = False
                    results.append(row)
                    slim = {k: v for k, v in row.items() if k != "metrics"}
                    slim["metrics_summary"] = {
                        k: row["metrics"][k]
                        for k in row["metrics"]
                        if k.endswith(f"__h{spec.path_horizon}") or k.endswith("_events")
                    }
                    _append_jsonl(completed_path, slim)
                except Exception as exc:
                    _append_jsonl(
                        failed_path,
                        {
                            "config_id": config_hash(cfg),
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                            "config": config_to_canonical_dict(cfg),
                        },
                    )

    # Deduplicate results by config_id (last wins)
    by_id = {r["config_id"]: r for r in results}
    results = sorted(by_id.values(), key=lambda r: r["config_id"])

    baseline_id = config_hash(baseline_config())
    baseline_row = by_id.get(baseline_id)
    if baseline_row is None:
        # evaluate baseline for comparison if missing
        baseline_row = evaluate_configuration(baseline_config(), data, spec=spec)
        baseline_row["oos_confirmation_status"] = oos_confirmation_status(baseline_row, spec)
        by_id[baseline_id] = baseline_row
        results = sorted(by_id.values(), key=lambda r: r["config_id"])

    baseline_n = float(baseline_row.get("full_n") or 0)

    for r in results:
        r["is_rank_score"] = _score_robust_path(r)
        r["sensitivity_score"] = _score_sensitivity(r, baseline_n)
        r["hedge_context_score"] = _score_hedge_context(r)
        r["oos_confirmation_status"] = oos_confirmation_status(r, spec)

    # Rankings: IS primary — sort eligible by IS score; OOS only as confirmation column
    eligible = [r for r in results if r.get("eligible_for_ranking")]
    is_ranked = sorted(eligible, key=lambda r: float(r.get("is_rank_score") or -1e18), reverse=True)
    sens_ranked = sorted(eligible, key=lambda r: float(r.get("sensitivity_score") or -1e18), reverse=True)
    hedge_ranked = sorted(eligible, key=lambda r: float(r.get("hedge_context_score") or -1e18), reverse=True)
    full_desc = sorted(
        results,
        key=lambda r: (float(r.get("full_peak_drop_median") or -1), float(r.get("full_n") or 0)),
        reverse=True,
    )

    def _rank_csv(rows: list[dict[str, Any]], path: Path) -> None:
        flat = []
        for i, r in enumerate(rows, start=1):
            fr = _flatten_row_for_csv(r)
            fr["rank"] = i
            flat.append(fr)
        pd.DataFrame(flat).to_csv(path, index=False)

    all_flat = [_flatten_row_for_csv(r) for r in results]
    pd.DataFrame(all_flat).to_csv(out / "all_configurations.csv", index=False)
    pd.DataFrame(all_flat).to_csv(out / "screening_results.csv", index=False)
    _rank_csv(is_ranked, out / "ranking_robust_path.csv")
    _rank_csv(sens_ranked, out / "ranking_sensitivity.csv")
    _rank_csv(hedge_ranked, out / "ranking_hedge_context.csv")
    _rank_csv(is_ranked, out / "is_ranking.csv")
    _rank_csv(full_desc, out / "full_descriptive_rank.csv")

    # OOS confirmation table (does not re-order by OOS)
    oos_rows = []
    for i, r in enumerate(is_ranked, start=1):
        fr = _flatten_row_for_csv(r)
        fr["is_rank"] = i
        fr["oos_confirmation_status"] = r.get("oos_confirmation_status")
        oos_rows.append(fr)
    pd.DataFrame(oos_rows).to_csv(out / "oos_confirmation.csv", index=False)

    # baseline comparison
    base_flat = _flatten_row_for_csv(baseline_row)
    cmp_rows = []
    for r in results:
        fr = _flatten_row_for_csv(r)
        cmp_rows.append(
            {
                **fr,
                "delta_full_n": (fr.get("full_n") or 0) - (base_flat.get("full_n") or 0),
                "delta_is_adverse_median": _sub(fr.get("is_adverse_median"), base_flat.get("is_adverse_median")),
                "delta_is_peak_drop_median": _sub(fr.get("is_peak_drop_median"), base_flat.get("is_peak_drop_median")),
                "delta_is_peak_before_trough_rate": _sub(
                    fr.get("is_peak_before_trough_rate"), base_flat.get("is_peak_before_trough_rate")
                ),
            }
        )
    pd.DataFrame(cmp_rows).to_csv(out / "baseline_comparison.csv", index=False)

    # monthly stability from baseline + top configs
    monthly_rows = []
    for r in [baseline_row] + is_ranked[:10]:
        ms = (
            (r.get("metrics") or {}).get("upper_50x__immediate_reclaim__full__h50")
            or (r.get("metrics_summary") or {}).get("upper_50x__immediate_reclaim__full__h50")
            or {}
        )
        for month, cnt in (ms.get("month_counts") or {}).items():
            monthly_rows.append(
                {
                    "config_id": r.get("config_id"),
                    "month": month,
                    "events": cnt,
                    "meets_minimum": int(cnt) >= spec.minimum_events_per_month,
                }
            )
    pd.DataFrame(monthly_rows).to_csv(out / "monthly_stability.csv", index=False)

    # top10 detailed
    top10 = is_ranked[:10]
    top10_detail = []
    for r in top10:
        fr = _flatten_row_for_csv(r)
        # enrich with p75/p90/p95 from metrics if present
        m = (r.get("metrics") or {}).get("upper_50x__immediate_reclaim__in_sample__h50") or {}
        for k in (
            "adverse_p75",
            "adverse_p90",
            "adverse_p95",
            "peak_drop_p75",
            "peak_drop_p90",
            "peak_drop_p95",
            "favorable_median",
        ):
            fr[k] = m.get(k)
        top10_detail.append(fr)
    pd.DataFrame(top10_detail).to_csv(out / "top10_detailed_metrics.csv", index=False)

    # controls for top10 only (optional — expensive)
    control_rows = []
    if top10 and not skip_controls:
        highs = data["high"].to_numpy(float)
        lows = data["low"].to_numpy(float)
        closes = data["close"].to_numpy(float)
        ts = pd.to_datetime(data["timestamp"], utc=True)
        for r in top10[: spec.top_n_controls]:
            cfg = liquidation_config_from_mapping(r.get("config") or {})
            replay = cache.get_by_key(level_generation_key(cfg)) or replay_liquidation_levels(data, cfg)
            cache.put(cfg, replay)
            events = build_lite_upper_events(
                replay,
                data,
                allowed_leverages=cfg.leverages,
                reclaim_window_candles=cfg.reclaim_window_candles,
            )
            sweep_idx = {e.candle_index for e in events}
            ctrl_pool = [i for i in range(len(data) - 1) if i not in sweep_idx]
            rng = np.random.default_rng(spec.seed)
            reclaim_evs = [
                e
                for e in events
                if e.leverage == 50
                and e.exclusive_reclaim_group == "immediate_reclaim"
                and e.entry_index is not None
            ]
            n_match = min(100, len(reclaim_evs), len(ctrl_pool))
            if n_match == 0:
                cache.clear_memory()
                gc.collect()
                continue
            picks = rng.choice(ctrl_pool, size=n_match, replace=False)

            def pack(entries: list[tuple[int, float]]) -> dict[str, float | None]:
                advs, drops, pbt = [], [], 0
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
                    drops.append(p["drop_from_peak_pct"])
                    if p["adverse_peak_before_favorable_trough"] or p["same_candle_peak_and_trough"]:
                        pbt += 1
                nloc = len(advs)
                return {
                    "n": nloc,
                    "median_adverse": float(np.median(advs)) if advs else None,
                    "median_peak_drop": float(np.median(drops)) if drops else None,
                    "peak_before_trough_rate": None if nloc == 0 else 100.0 * pbt / nloc,
                }

            est = pack([(int(e.entry_index), float(e.entry_price)) for e in reclaim_evs[:n_match]])
            cst = pack([(int(i + 1), float(data.iloc[i + 1]["open"])) for i in picks])
            control_rows.append(
                {
                    "config_id": r["config_id"],
                    "event_n": est["n"],
                    "control_n": cst["n"],
                    "event_median_adverse": est["median_adverse"],
                    "control_median_adverse": cst["median_adverse"],
                    "event_median_peak_drop": est["median_peak_drop"],
                    "control_median_peak_drop": cst["median_peak_drop"],
                    "event_peak_before_trough_rate": est["peak_before_trough_rate"],
                    "control_peak_before_trough_rate": cst["peak_before_trough_rate"],
                    "note": "empirical only; not a significance claim",
                }
            )
            cache.clear_memory()
            gc.collect()
    pd.DataFrame(control_rows).to_csv(out / "top10_control_comparison.csv", index=False)

    # recommended configs
    rec_dir = out / "recommended_configs"
    rec_dir.mkdir(parents=True, exist_ok=True)
    save_liquidation_config(rec_dir / "baseline.json", baseline_config(), name=BASELINE_CONFIG_NAME)

    def _pick_confirmed(ranked: list[dict[str, Any]]) -> dict[str, Any] | None:
        for r in ranked:
            if r.get("oos_confirmation_status") in {"confirmed", "directionally_confirmed"}:
                return r
        return ranked[0] if ranked else None

    best_robust = _pick_confirmed(is_ranked)
    best_sens = _pick_confirmed(sens_ranked)
    best_hedge = _pick_confirmed(hedge_ranked)
    for name, br in (
        ("best_robust_path.json", best_robust),
        ("best_sensitive_but_stable.json", best_sens),
        ("best_hedge_context.json", best_hedge),
    ):
        if br and br.get("config"):
            save_liquidation_config(
                rec_dir / name,
                liquidation_config_from_mapping(br["config"]),
                name=name.replace(".json", ""),
            )

    # refinement grid export (not auto-run)
    top_cfgs = []
    for r in is_ranked[:5]:
        if r.get("config"):
            top_cfgs.append(liquidation_config_from_mapping(r["config"]))
    if not top_cfgs:
        top_cfgs = [baseline_config()]
    ref = build_refinement_grid(top_cfgs, grid_cfg)
    (out / "refinement_grid.json").write_text(json.dumps(ref, indent=2) + "\n", encoding="utf-8")

    failed_count = 0
    if failed_path.exists():
        failed_count = sum(1 for line in failed_path.read_text(encoding="utf-8").splitlines() if line.strip())

    summary = {
        "n_planned": len(planned),
        "n_completed": len(results),
        "n_failed": failed_count,
        "n_eligible": len(eligible),
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "baseline_config_id": baseline_id,
        "baseline_full_n": baseline_row.get("full_n"),
        "best_robust_path_config_id": None if not best_robust else best_robust.get("config_id"),
        "best_robust_oos_status": None if not best_robust else best_robust.get("oos_confirmation_status"),
        "best_sensitive_config_id": None if not best_sens else best_sens.get("config_id"),
        "best_hedge_config_id": None if not best_hedge else best_hedge.get("config_id"),
        "disclaimer": "Estimated LuxAlgo-style levels; not real exchange liquidations. No trading-edge claim.",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_optimizer_readme(out, summary, baseline_row, best_robust, best_sens, is_ranked)
    return summary


def _sub(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return float(a) - float(b)


def write_optimizer_readme(
    out: Path,
    summary: dict[str, Any],
    baseline_row: dict[str, Any],
    best_robust: dict[str, Any] | None,
    best_sens: dict[str, Any] | None,
    is_ranked: list[dict[str, Any]],
) -> None:
    br = best_robust or {}
    bs = best_sens or {}
    text = f"""# Liquidation Level Optimizer Results

Estimated LuxAlgo-style levels — **not** real exchange liquidations.
This run ranks **path context** (further squeeze, then drop), not a trading edge.

## How many combinations were checked?

- Completed configurations: **{summary.get('n_completed')}**
- Eligible for ranking: **{summary.get('n_eligible')}**
- Failed: **{summary.get('n_failed')}**
- Cache hits / misses: **{summary.get('cache_hits')} / {summary.get('cache_misses')}**

## Baseline vs winners

Baseline (`{summary.get('baseline_config_id')}`):
- full events (50x immediate reclaim h50): **{baseline_row.get('full_n')}**
- IS adverse median: **{baseline_row.get('is_adverse_median')}**
- IS peak-drop median: **{baseline_row.get('is_peak_drop_median')}**
- IS peak-before-trough: **{baseline_row.get('is_peak_before_trough_rate')}**

Best robust (IS-ranked, OOS-confirmed if possible): `{br.get('config_id')}`
- OOS status: **{br.get('oos_confirmation_status')}**
- full_n: **{br.get('full_n')}**
- IS adverse / peak-drop / PBT: **{br.get('is_adverse_median')}** / **{br.get('is_peak_drop_median')}** / **{br.get('is_peak_before_trough_rate')}**

Best sensitive-but-stable: `{bs.get('config_id')}` · OOS **{bs.get('oos_confirmation_status')}** · full_n **{bs.get('full_n')}**

## Did more sensitive settings help?

Compare `baseline_comparison.csv` and the three ranking CSVs.
Often more sensitive volume/volatility settings **raise event counts** but also raise noise
(sideways / breakout). Prefer configs that stay eligible, keep OOS confirmation, and do not
inflate p95 adverse without a matching peak-drop structure.

## Recommendation

Use `recommended_configs/best_robust_path.json` for further research path context.
Keep `recommended_configs/baseline.json` as the reproducibility anchor.
See also `refinement_grid.json` for Stage-B (not auto-run).

No scanner/bot/live integration.
"""
    (out / "README_results.md").write_text(text, encoding="utf-8")
