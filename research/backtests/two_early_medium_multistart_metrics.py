"""Aggregation / decision helpers for two_early_medium multi-start validation."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Sequence

from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.two_early_medium_multistart_starts import (
    CATEGORY_HISTORICAL_BLOCKER,
    CATEGORY_NEUTRAL_POOL,
)


def percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def dist_stats(values: Sequence[float]) -> dict[str, Any]:
    xs = [float(v) for v in values]
    if not xs:
        return {
            "n": 0,
            "sum": 0.0,
            "mean": None,
            "median": None,
            "p10": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p90": None,
            "best": None,
            "worst": None,
        }
    return {
        "n": len(xs),
        "sum": sum(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "p10": percentile(xs, 10),
        "p25": percentile(xs, 25),
        "p50": percentile(xs, 50),
        "p75": percentile(xs, 75),
        "p90": percentile(xs, 90),
        "best": max(xs),
        "worst": min(xs),
    }


def bootstrap_ci(
    values: Sequence[float],
    *,
    n_boot: int = 2000,
    seed: int = 20260721,
    alpha: float = 0.05,
) -> dict[str, Any]:
    xs = [float(v) for v in values]
    if not xs:
        return {"n": 0, "mean_ci": None, "median_ci": None}
    rng = random.Random(seed)
    means: list[float] = []
    medians: list[float] = []
    n = len(xs)
    for _ in range(int(n_boot)):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(sample))
        medians.append(statistics.median(sample))
    lo = 100.0 * (alpha / 2.0)
    hi = 100.0 * (1.0 - alpha / 2.0)
    return {
        "n": n,
        "n_boot": n_boot,
        "alpha": alpha,
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "mean_ci": [percentile(means, lo), percentile(means, hi)],
        "median_ci": [percentile(medians, lo), percentile(medians, hi)],
    }


def status_bucket(legacy_flat: bool, staging_flat: bool) -> str:
    if (not legacy_flat) and staging_flat:
        return "legacy_open_staging_closed"
    if legacy_flat and (not staging_flat):
        return "legacy_closed_staging_open"
    if legacy_flat and staging_flat:
        return "both_closed"
    return "both_open"


def compare_pair(legacy: dict[str, Any], staging: dict[str, Any], start_meta: dict[str, Any]) -> dict[str, Any]:
    l_flat = int(legacy.get("trade_flat") or 0) == 1
    s_flat = int(staging.get("trade_flat") or 0) == 1
    l_total = safe_float(legacy.get("total_pnl"))
    s_total = safe_float(staging.get("total_pnl"))
    delta = s_total - l_total
    if abs(delta) <= 1e-9:
        better = "equal"
    elif delta > 0:
        better = "staging_better"
    else:
        better = "staging_worse"
    return {
        "coin": legacy.get("coin") or staging.get("coin"),
        "start_index": int(legacy.get("start_index") or staging.get("start_index") or 0),
        "pair_key": start_meta.get("pair_key")
        or f"{str(legacy.get('coin') or '').upper()}|{int(legacy.get('start_index') or 0)}",
        "primary_category": start_meta.get("primary_category"),
        "categories": start_meta.get("categories"),
        "is_historical_blocker": int(bool(start_meta.get("is_historical_blocker"))),
        "is_neutral_pool": int(bool(start_meta.get("is_neutral_pool"))),
        "bucket": status_bucket(l_flat, s_flat),
        "better": better,
        "delta_total_pnl": delta,
        "delta_closed_pnl": safe_float(staging.get("closed_pnl")) - safe_float(legacy.get("closed_pnl")),
        "delta_open_mtm": safe_float(staging.get("open_mtm")) - safe_float(legacy.get("open_mtm")),
        "delta_duration": int(staging.get("duration_candles") or 0)
        - int(legacy.get("duration_candles") or 0),
        "legacy_status": legacy.get("status"),
        "staging_status": staging.get("status"),
        "legacy_flat": int(l_flat),
        "staging_flat": int(s_flat),
        "legacy_closed_pnl": safe_float(legacy.get("closed_pnl")),
        "staging_closed_pnl": safe_float(staging.get("closed_pnl")),
        "legacy_open_mtm": safe_float(legacy.get("open_mtm")),
        "staging_open_mtm": safe_float(staging.get("open_mtm")),
        "legacy_total_pnl": l_total,
        "staging_total_pnl": s_total,
        "legacy_duration": int(legacy.get("duration_candles") or 0),
        "staging_duration": int(staging.get("duration_candles") or 0),
        "legacy_max_cycle": legacy.get("max_cycle"),
        "staging_max_cycle": staging.get("max_cycle"),
        "legacy_coverage_class": legacy.get("coverage_class"),
        "staging_coverage_class": staging.get("coverage_class"),
        "legacy_valid_close": int(legacy.get("economically_valid_close") or 0),
        "staging_valid_close": int(staging.get("economically_valid_close") or 0),
        "legacy_max_long_notional": safe_float(legacy.get("max_long_notional")),
        "staging_max_long_notional": safe_float(staging.get("max_long_notional")),
        "legacy_max_short_notional": safe_float(legacy.get("max_short_notional")),
        "staging_max_short_notional": safe_float(staging.get("max_short_notional")),
        "legacy_max_abs_net_exposure": safe_float(legacy.get("max_abs_net_exposure")),
        "staging_max_abs_net_exposure": safe_float(staging.get("max_abs_net_exposure")),
        "legacy_max_drawdown_pct": safe_float(legacy.get("max_drawdown_pct")),
        "staging_max_drawdown_pct": safe_float(staging.get("max_drawdown_pct")),
        "legacy_fees": safe_float(legacy.get("fees")),
        "staging_fees": safe_float(staging.get("fees")),
        "staging_filled_stages": staging.get("filled_stage_indices"),
        "staging_cancelled_stages": staging.get("cancelled_stage_indices"),
        "staging_exit_reason": staging.get("exit_reason"),
        "legacy_exit_reason": legacy.get("exit_reason"),
        "atom_like_regression": int(
            l_flat
            and (not s_flat)
            and str(legacy.get("coin") or "").upper() == "ATOMUSDT"
        ),
    }


def summarize_pairs(pairs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    deltas = [safe_float(p.get("delta_total_pnl")) for p in pairs]
    better = sum(1 for p in pairs if p.get("better") == "staging_better")
    equal = sum(1 for p in pairs if p.get("better") == "equal")
    worse = sum(1 for p in pairs if p.get("better") == "staging_worse")
    buckets = Counter(str(p.get("bucket")) for p in pairs)
    return {
        "n_pairs": len(pairs),
        "better": better,
        "equal": equal,
        "worse": worse,
        "share_better": (better / len(pairs)) if pairs else None,
        "share_positive_delta": (
            sum(1 for d in deltas if d > 1e-9) / len(deltas) if deltas else None
        ),
        "delta_total": dist_stats(deltas),
        "sum_delta_closed_pnl": sum(safe_float(p.get("delta_closed_pnl")) for p in pairs),
        "sum_delta_open_mtm": sum(safe_float(p.get("delta_open_mtm")) for p in pairs),
        "sum_legacy_total": sum(safe_float(p.get("legacy_total_pnl")) for p in pairs),
        "sum_staging_total": sum(safe_float(p.get("staging_total_pnl")) for p in pairs),
        "legacy_valid_closes": sum(int(p.get("legacy_valid_close") or 0) for p in pairs),
        "staging_valid_closes": sum(int(p.get("staging_valid_close") or 0) for p in pairs),
        "status_transitions": dict(buckets),
        "additional_valid_closes": sum(
            1
            for p in pairs
            if int(p.get("legacy_valid_close") or 0) == 0
            and int(p.get("staging_valid_close") or 0) == 1
        ),
        "lost_valid_closes": sum(
            1
            for p in pairs
            if int(p.get("legacy_valid_close") or 0) == 1
            and int(p.get("staging_valid_close") or 0) == 0
        ),
    }


def summarize_by_coin(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        by[str(p.get("coin") or "").upper()].append(p)
    rows: list[dict[str, Any]] = []
    for coin in sorted(by):
        s = summarize_pairs(by[coin])
        rows.append(
            {
                "coin": coin,
                "n_pairs": s["n_pairs"],
                "better": s["better"],
                "equal": s["equal"],
                "worse": s["worse"],
                "sum_delta_total_pnl": s["delta_total"]["sum"],
                "median_delta_total_pnl": s["delta_total"]["median"],
                "mean_delta_total_pnl": s["delta_total"]["mean"],
                "worst_delta": s["delta_total"]["worst"],
                "best_delta": s["delta_total"]["best"],
                "legacy_valid_closes": s["legacy_valid_closes"],
                "staging_valid_closes": s["staging_valid_closes"],
                "additional_valid_closes": s["additional_valid_closes"],
                "lost_valid_closes": s["lost_valid_closes"],
                "sum_delta_open_mtm": s["sum_delta_open_mtm"],
            }
        )
    return rows


def summarize_by_regime(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per analysis group; a pair may contribute to multiple groups."""
    groups = {
        "bullish": "bullish",
        "bearish": "bearish",
        "range": "range",
        "high_vol": "high_vol",
        "low_vol": "low_vol",
        "pre_high_vol": "pre_high_vol",
        "historical_blocker": CATEGORY_HISTORICAL_BLOCKER,
        "neutral_pool": CATEGORY_NEUTRAL_POOL,
        "all": None,
    }
    rows: list[dict[str, Any]] = []
    for name, tag in groups.items():
        if tag is None:
            subset = list(pairs)
        else:
            subset = []
            for p in pairs:
                cats = p.get("categories") or []
                if isinstance(cats, str):
                    try:
                        import json

                        cats = json.loads(cats)
                    except Exception:
                        cats = [cats]
                if tag in cats or p.get("primary_category") == tag:
                    subset.append(p)
        s = summarize_pairs(subset)
        durs = [safe_float(p.get("delta_duration")) for p in subset]
        rows.append(
            {
                "regime_group": name,
                "n_pairs": s["n_pairs"],
                "legacy_valid_closes": s["legacy_valid_closes"],
                "staging_valid_closes": s["staging_valid_closes"],
                "close_rate_legacy": (
                    s["legacy_valid_closes"] / s["n_pairs"] if s["n_pairs"] else None
                ),
                "close_rate_staging": (
                    s["staging_valid_closes"] / s["n_pairs"] if s["n_pairs"] else None
                ),
                "sum_delta_total_pnl": s["delta_total"]["sum"],
                "median_delta_total_pnl": s["delta_total"]["median"],
                "mean_delta_total_pnl": s["delta_total"]["mean"],
                "worst_delta": s["delta_total"]["worst"],
                "better": s["better"],
                "equal": s["equal"],
                "worse": s["worse"],
                "sum_delta_open_mtm": s["sum_delta_open_mtm"],
                "median_delta_duration": statistics.median(durs) if durs else None,
            }
        )
    return rows


def leaveouts(pairs: Sequence[dict[str, Any]], coin_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(coin_rows, key=lambda r: safe_float(r.get("sum_delta_total_pnl")), reverse=True)
    top1 = [ranked[0]["coin"]] if ranked else []
    top3 = [r["coin"] for r in ranked[:3]]
    total = sum(safe_float(p.get("delta_total_pnl")) for p in pairs)

    def _sum_without(exclude: set[str]) -> float:
        return sum(
            safe_float(p.get("delta_total_pnl"))
            for p in pairs
            if str(p.get("coin") or "").upper() not in exclude
        )

    return {
        "total_delta": total,
        "top1_coins": top1,
        "top3_coins": top3,
        "without_apt": _sum_without({"APTUSDT"}),
        "without_atom": _sum_without({"ATOMUSDT"}),
        "without_top1": _sum_without(set(top1)),
        "without_top3": _sum_without(set(top3)),
        "top1_share": (safe_float(ranked[0].get("sum_delta_total_pnl")) / total) if ranked and total else None,
        "top3_share": (
            sum(safe_float(r.get("sum_delta_total_pnl")) for r in ranked[:3]) / total
            if ranked and total
            else None
        ),
        "coin_win_rate": (
            sum(1 for r in coin_rows if safe_float(r.get("sum_delta_total_pnl")) > 0) / len(coin_rows)
            if coin_rows
            else None
        ),
        "median_coin_delta": statistics.median(
            [safe_float(r.get("sum_delta_total_pnl")) for r in coin_rows]
        )
        if coin_rows
        else None,
    }


def atom_regression_rows(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for p in pairs:
        if str(p.get("coin") or "").upper() != "ATOMUSDT":
            continue
        if p.get("bucket") != "legacy_closed_staging_open" and safe_float(p.get("delta_total_pnl")) >= 0:
            continue
        rows.append(dict(p))
    # Also keep all ATOM worse pairs for diagnosis breadth
    if not rows:
        rows = [
            dict(p)
            for p in pairs
            if str(p.get("coin") or "").upper() == "ATOMUSDT" and p.get("better") == "staging_worse"
        ]
    return sorted(rows, key=lambda r: safe_float(r.get("delta_total_pnl")))


def decide(summary: dict[str, Any], leave: dict[str, Any], integrity_pass: bool) -> dict[str, Any]:
    delta = summary.get("delta_total") or {}
    median_ok = (delta.get("median") or 0) >= -1e-9
    majority_not_worse = int(summary.get("better") or 0) >= int(summary.get("worse") or 0)
    total_pos = (delta.get("sum") or 0) > 0
    without_apt_pos = safe_float(leave.get("without_apt")) > 0
    # Neutral pool must be provided in summary_extra
    neutral = summary.get("neutral_pool") or {}
    neutral_pos = safe_float((neutral.get("delta_total") or {}).get("sum")) > 0 if neutral else False
    exposure_ok = bool(summary.get("exposure_drawdown_ok", True))
    atom_bounded = bool(summary.get("atom_regression_bounded", False))

    gates = {
        "safety_green": integrity_pass,
        "median_pair_delta_non_negative": median_ok,
        "majority_not_worse": majority_not_worse,
        "total_pnl_delta_positive": total_pos,
        "neutral_pool_advantage": neutral_pos,
        "without_apt_positive": without_apt_pos,
        "atom_regression_bounded": atom_bounded,
        "exposure_drawdown_ok": exposure_ok,
    }
    hard = (
        gates["safety_green"]
        and gates["median_pair_delta_non_negative"]
        and gates["majority_not_worse"]
        and gates["total_pnl_delta_positive"]
        and gates["neutral_pool_advantage"]
        and gates["without_apt_positive"]
    )
    if not gates["safety_green"]:
        verdict = "verwerfen"
        next_step = "Safety/Coverage reparieren — kein wirtschaftlicher Aufstieg."
    elif hard and gates["atom_regression_bounded"] and gates["exposure_drawdown_ok"]:
        verdict = "Kandidat für größere Multi-Coin-Validierung"
        next_step = (
            "Größere Multi-Coin-/Fenster-Validierung; Shadow-/Paper erst danach. "
            "Keine Live-Integration."
        )
    elif hard:
        verdict = "Research-Kandidat behalten"
        next_step = (
            "ATOM-Regressionsklasse weiter diagnostizieren (keine Sonderregel); "
            "dann größere Multi-Coin-Validierung."
        )
    elif total_pos and median_ok:
        verdict = "Research-Kandidat behalten"
        next_step = "Neutrale Starts / Leave-outs / ATOM-Pfad nachschärfen."
    else:
        verdict = "verwerfen"
        next_step = "Vorteil trägt nicht über Multi-Start — Profil nicht weiter priorisieren."

    return {
        "verdict": verdict,
        "next_step": next_step,
        "gates": gates,
        "live_integration": "noch keine Live-Integration",
        "shadow_paper": (
            "nein"
            if verdict
            not in {
                "Kandidat für größere Multi-Coin-Validierung",
                "Kandidat für Shadow-/Paper-Runtime",
            }
            else "noch nicht — erst größere Multi-Coin-Validierung"
        ),
    }
