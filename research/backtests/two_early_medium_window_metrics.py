"""Aggregation / decision helpers for large multi-coin × window TEM validation."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from typing import Any, Sequence

from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.two_early_medium_multistart_metrics import (
    bootstrap_ci,
    compare_pair,
    dist_stats,
    summarize_pairs,
)
from research.backtests.two_early_medium_multistart_starts import CATEGORY_NEUTRAL_POOL


def _cats(row: dict[str, Any]) -> list[str]:
    cats = row.get("categories") or []
    if isinstance(cats, str):
        try:
            cats = json.loads(cats)
        except Exception:  # noqa: BLE001
            cats = [cats]
    return list(cats)


def compare_window_pair(
    legacy: dict[str, Any],
    staging: dict[str, Any],
    start_meta: dict[str, Any],
) -> dict[str, Any]:
    base = compare_pair(legacy, staging, start_meta)
    base["window_id"] = start_meta.get("window_id") or legacy.get("window_id")
    base["window_kind"] = start_meta.get("window_kind") or legacy.get("window_kind")
    base["pair_key"] = start_meta.get("pair_key") or base.get("pair_key")
    base["run_end_index"] = start_meta.get("run_end_index")
    base["max_window_candles"] = start_meta.get("max_window_candles")
    return base


def summarize_by_keys(pairs: Sequence[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        k = tuple(p.get(key) for key in keys)
        groups[k].append(p)
    rows: list[dict[str, Any]] = []
    for key, subset in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        s = summarize_pairs(subset)
        row = {keys[i]: key[i] for i in range(len(keys))}
        row.update(
            {
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
                "sum_delta_closed_pnl": s["sum_delta_closed_pnl"],
                "sum_delta_open_mtm": s["sum_delta_open_mtm"],
                "win": int((s["delta_total"]["sum"] or 0) > 0),
            }
        )
        rows.append(row)
    return rows


def summarize_by_start_category(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        primary = str(p.get("primary_category") or "unknown")
        groups[primary].append(p)
        for cat in _cats(p):
            if cat != primary:
                groups[f"tag:{cat}"].append(p)
    rows = []
    for name, subset in sorted(groups.items()):
        s = summarize_pairs(subset)
        rows.append(
            {
                "start_category": name,
                "n_pairs": s["n_pairs"],
                "better": s["better"],
                "equal": s["equal"],
                "worse": s["worse"],
                "sum_delta_total_pnl": s["delta_total"]["sum"],
                "median_delta_total_pnl": s["delta_total"]["median"],
                "additional_valid_closes": s["additional_valid_closes"],
                "lost_valid_closes": s["lost_valid_closes"],
            }
        )
    return rows


def leaveout_analysis(pairs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_coin = summarize_by_keys(pairs, ["coin"])
    by_window = summarize_by_keys(pairs, ["window_id"])
    ranked_coins = sorted(by_coin, key=lambda r: safe_float(r.get("sum_delta_total_pnl")), reverse=True)
    ranked_windows = sorted(
        by_window, key=lambda r: safe_float(r.get("sum_delta_total_pnl")), reverse=True
    )
    total = sum(safe_float(p.get("delta_total_pnl")) for p in pairs)
    top1 = [ranked_coins[0]["coin"]] if ranked_coins else []
    top3 = [r["coin"] for r in ranked_coins[:3]]
    best_window = [ranked_windows[0]["window_id"]] if ranked_windows else []

    def _sum_without_coins(exclude: set[str]) -> float:
        return sum(
            safe_float(p.get("delta_total_pnl"))
            for p in pairs
            if str(p.get("coin") or "").upper() not in exclude
        )

    def _sum_without_windows(exclude: set[str]) -> float:
        return sum(
            safe_float(p.get("delta_total_pnl"))
            for p in pairs
            if str(p.get("window_id") or "") not in exclude
        )

    deltas = sorted(safe_float(p.get("delta_total_pnl")) for p in pairs)
    top3_pairs = set(id(p) for p in sorted(pairs, key=lambda r: safe_float(r.get("delta_total_pnl")), reverse=True)[:3])
    without_top3_pairs = sum(
        safe_float(p.get("delta_total_pnl")) for p in pairs if id(p) not in top3_pairs
    )

    neutral = [p for p in pairs if int(safe_float(p.get("is_neutral_pool"))) == 1 or CATEGORY_NEUTRAL_POOL in _cats(p)]
    regular_random = [
        p
        for p in pairs
        if str(p.get("primary_category")) in {"grid", "random"}
        or "grid" in _cats(p)
        or "random" in _cats(p)
    ]
    non_blocker = [p for p in pairs if int(safe_float(p.get("is_historical_blocker"))) == 0]

    coin_wins = sum(1 for r in by_coin if safe_float(r.get("sum_delta_total_pnl")) > 0)
    window_wins = sum(1 for r in by_window if safe_float(r.get("sum_delta_total_pnl")) > 0)
    cw = summarize_by_keys(pairs, ["coin", "window_id"])
    cw_wins = sum(1 for r in cw if safe_float(r.get("sum_delta_total_pnl")) > 0)

    return {
        "total_delta": total,
        "top1_coins": top1,
        "top3_coins": top3,
        "best_window": best_window,
        "without_apt": _sum_without_coins({"APTUSDT"}),
        "without_top1": _sum_without_coins(set(top1)),
        "without_top3": _sum_without_coins(set(top3)),
        "without_best_window": _sum_without_windows(set(best_window)),
        "without_top3_pairs": without_top3_pairs,
        "without_blocker_reference": sum(safe_float(p.get("delta_total_pnl")) for p in non_blocker),
        "neutral_pool_delta": sum(safe_float(p.get("delta_total_pnl")) for p in neutral),
        "regular_random_delta": sum(safe_float(p.get("delta_total_pnl")) for p in regular_random),
        "coin_win_rate": (coin_wins / len(by_coin)) if by_coin else None,
        "window_win_rate": (window_wins / len(by_window)) if by_window else None,
        "coin_window_win_rate": (cw_wins / len(cw)) if cw else None,
        "median_coin_delta": statistics.median([safe_float(r.get("sum_delta_total_pnl")) for r in by_coin])
        if by_coin
        else None,
        "median_window_delta": statistics.median(
            [safe_float(r.get("sum_delta_total_pnl")) for r in by_window]
        )
        if by_window
        else None,
        "concentration_top1_share": (safe_float(ranked_coins[0].get("sum_delta_total_pnl")) / total)
        if ranked_coins and total
        else None,
        "concentration_top3_share": (
            sum(safe_float(r.get("sum_delta_total_pnl")) for r in ranked_coins[:3]) / total
            if ranked_coins and total
            else None
        ),
        "best_window_share": (
            safe_float(ranked_windows[0].get("sum_delta_total_pnl")) / total
            if ranked_windows and total
            else None
        ),
        "delta_distribution": dist_stats(deltas),
    }


def decide_large(summary: dict[str, Any], leave: dict[str, Any], integrity_pass: bool) -> dict[str, Any]:
    delta = summary.get("delta_total") or {}
    by_window = summary.get("by_window_positive_count") or 0
    gates = {
        "safety_green": integrity_pass,
        "total_pnl_delta_positive": (delta.get("sum") or 0) > 0,
        "advantage_in_ge_2_windows": int(by_window) >= 2,
        "neutral_pool_positive": safe_float(leave.get("neutral_pool_delta")) > 0,
        "regular_random_positive": safe_float(leave.get("regular_random_delta")) > 0,
        "without_apt_positive": safe_float(leave.get("without_apt")) > 0,
        "without_top3_not_clearly_negative": safe_float(leave.get("without_top3")) >= -50.0,
        "coin_win_rate_positive": (leave.get("coin_win_rate") or 0) >= 0.5,
        "not_single_window_dominated": (leave.get("best_window_share") or 0) < 0.75,
        "additional_gt_lost": int(summary.get("additional_valid_closes") or 0)
        > int(summary.get("lost_valid_closes") or 0),
        "exposure_drawdown_ok": bool(summary.get("exposure_drawdown_ok", True)),
        "regression_class_bounded": bool(summary.get("regression_class_bounded", True)),
    }
    hard = all(
        [
            gates["safety_green"],
            gates["total_pnl_delta_positive"],
            gates["advantage_in_ge_2_windows"],
            gates["neutral_pool_positive"],
            gates["regular_random_positive"],
            gates["without_apt_positive"],
            gates["without_top3_not_clearly_negative"],
            gates["coin_win_rate_positive"],
            gates["not_single_window_dominated"],
            gates["additional_gt_lost"],
            gates["exposure_drawdown_ok"],
            gates["regression_class_bounded"],
        ]
    )
    soft = (
        gates["safety_green"]
        and gates["total_pnl_delta_positive"]
        and gates["without_apt_positive"]
        and int(summary.get("better") or 0) >= int(summary.get("worse") or 0)
    )
    if not gates["safety_green"]:
        verdict = "verwerfen"
        next_step = "Safety/Coverage reparieren."
    elif hard:
        verdict = "Kandidat für Shadow-/Paper"
        next_step = (
            "Shadow-/Paper-Runtime vorbereiten; weiterhin keine Live-Integration. "
            "Regressionsklasse (TRX/ATOM-artig) parallel beobachten."
        )
    elif soft:
        verdict = "Research-Kandidat"
        next_step = "Fenster-/Open-MTM-Follow-through vertiefen; Shadow erst nach härteren Gates."
    else:
        verdict = "verwerfen"
        next_step = "Vorteil trägt nicht über Multi-Coin×Fenster — Profil nicht priorisieren."

    return {
        "verdict": verdict,
        "next_step": next_step,
        "gates": gates,
        "live_integration": "noch keine Live-Integration",
        "shadow_paper": "ja-kandidat" if verdict == "Kandidat für Shadow-/Paper" else "nein",
    }


def open_mtm_followthrough_stub_rows(
    pairs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Placeholder rows for open pairs needing extended observation (filled by runner)."""
    rows = []
    for p in pairs:
        if p.get("bucket") not in {"both_open", "legacy_closed_staging_open", "legacy_open_staging_closed"}:
            if int(p.get("staging_flat") or 0) == 1 and int(p.get("legacy_flat") or 0) == 1:
                continue
        if int(p.get("staging_flat") or 0) == 1 and int(p.get("legacy_flat") or 0) == 1:
            continue
        # Any pair with at least one side open at window end
        if int(p.get("staging_flat") or 0) == 1 and int(p.get("legacy_flat") or 0) == 1:
            continue
        if int(p.get("staging_flat") or 0) == 0 or int(p.get("legacy_flat") or 0) == 0:
            rows.append(
                {
                    "pair_key": p.get("pair_key"),
                    "coin": p.get("coin"),
                    "window_id": p.get("window_id"),
                    "start_index": p.get("start_index"),
                    "bucket": p.get("bucket"),
                    "staging_open": int(p.get("staging_flat") or 0) == 0,
                    "legacy_open": int(p.get("legacy_flat") or 0) == 0,
                    "primary_delta_total_pnl": p.get("delta_total_pnl"),
                    "followthrough_status": "pending_or_no_extension",
                }
            )
    return rows
