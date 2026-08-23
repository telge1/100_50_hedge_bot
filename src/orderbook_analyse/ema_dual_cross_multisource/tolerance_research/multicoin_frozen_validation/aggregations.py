"""Aggregations: pooled, equal-weight, median-coin, LOO, concentration, splits."""

from __future__ import annotations

from statistics import median
from typing import Any

from .constants import PRIMARY_COST_PCT, SMALL_SAMPLE_N


def _valid(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        t
        for t in trades
        if t.get("net_pnl_usdt") is not None
        and t.get("exit_reason") != "INCOMPLETE_OUTCOME_HORIZON"
        and t.get("include_in_primary_pnl", True) is not False
    ]


def pooled_all_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    from ..tpsl_pnl_engine import aggregate_strategy_stats

    stats = aggregate_strategy_stats(_valid(trades))
    return {"aggregation": "POOLED_ALL_TRADES", **stats}


def results_by_coin(
    trades: list[dict[str, Any]],
    *,
    strategy_key: str | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    from ..tpsl_pnl_engine import aggregate_strategy_stats

    filtered = _valid(trades)
    if strategy_key:
        filtered = [t for t in filtered if t.get("strategy_key") == strategy_key]
    if group:
        filtered = [t for t in filtered if t.get("group") == group]

    by_sym: dict[str, list[dict]] = {}
    for t in filtered:
        by_sym.setdefault(str(t.get("symbol", "")).upper(), []).append(t)

    rows = []
    for sym, ts in sorted(by_sym.items()):
        stats = aggregate_strategy_stats(ts)
        n = int(stats.get("n_trades") or 0)
        long_n = sum(1 for t in ts if str(t.get("direction", "")).upper() == "BULLISH")
        short_n = sum(1 for t in ts if str(t.get("direction", "")).upper() == "BEARISH")
        rows.append(
            {
                "symbol": sym,
                "strategy_key": strategy_key,
                "group": group,
                "sample_flag": "SMALL_SAMPLE" if n < SMALL_SAMPLE_N else "OK",
                "n_long": long_n,
                "n_short": short_n,
                "coverage_class": (ts[0].get("coverage_class") if ts else None),
                **stats,
            }
        )
    return rows


def equal_weight_per_coin(coin_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Each coin contributes equally via mean of per-coin expectancy / net PnL."""
    if not coin_rows:
        return {
            "aggregation": "EQUAL_WEIGHT_PER_COIN",
            "n_coins": 0,
            "mean_coin_expectancy_usdt": None,
            "mean_coin_net_pnl_usdt": None,
            "mean_coin_pf": None,
            "pct_coins_net_positive": None,
            "pct_coins_pf_gt_1": None,
            "pct_coins_expectancy_positive": None,
        }
    n = len(coin_rows)
    expectancies = [float(r.get("avg_net_pnl_usdt") or 0) for r in coin_rows]
    pnls = [float(r.get("net_pnl_usdt") or 0) for r in coin_rows]
    pfs = [r.get("profit_factor_net") for r in coin_rows]
    pf_vals = [float(x) for x in pfs if x is not None]
    return {
        "aggregation": "EQUAL_WEIGHT_PER_COIN",
        "n_coins": n,
        "mean_coin_expectancy_usdt": round(sum(expectancies) / n, 6),
        "mean_coin_net_pnl_usdt": round(sum(pnls) / n, 6),
        "mean_coin_pf": round(sum(pf_vals) / len(pf_vals), 6) if pf_vals else None,
        "pct_coins_net_positive": round(sum(1 for x in pnls if x > 0) / n, 6),
        "pct_coins_pf_gt_1": round(sum(1 for x in pf_vals if x > 1) / n, 6) if n else None,
        "pct_coins_expectancy_positive": round(sum(1 for x in expectancies if x > 0) / n, 6),
    }


def median_coin_result(coin_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not coin_rows:
        return {
            "aggregation": "MEDIAN_COIN_RESULT",
            "n_coins": 0,
            "median_expectancy_usdt": None,
            "median_net_pnl_usdt": None,
            "p25_expectancy_usdt": None,
            "p75_expectancy_usdt": None,
            "worst_coin": None,
            "best_coin": None,
        }
    expectancies = sorted(float(r.get("avg_net_pnl_usdt") or 0) for r in coin_rows)
    pnls = sorted(float(r.get("net_pnl_usdt") or 0) for r in coin_rows)
    by_pnl = sorted(coin_rows, key=lambda r: float(r.get("net_pnl_usdt") or 0))
    n = len(expectancies)

    def pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        i = int(round((n - 1) * q))
        return vals[max(0, min(n - 1, i))]

    return {
        "aggregation": "MEDIAN_COIN_RESULT",
        "n_coins": n,
        "median_expectancy_usdt": round(median(expectancies), 6),
        "median_net_pnl_usdt": round(median(pnls), 6),
        "p25_expectancy_usdt": round(pct(expectancies, 0.25), 6),
        "p75_expectancy_usdt": round(pct(expectancies, 0.75), 6),
        "worst_coin": by_pnl[0].get("symbol"),
        "worst_coin_net_pnl_usdt": float(by_pnl[0].get("net_pnl_usdt") or 0),
        "best_coin": by_pnl[-1].get("symbol"),
        "best_coin_net_pnl_usdt": float(by_pnl[-1].get("net_pnl_usdt") or 0),
    }


def leave_one_coin_out(coin_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each coin, recompute equal-weight mean expectancy without that coin."""
    rows = []
    for leave in coin_rows:
        rest = [r for r in coin_rows if r.get("symbol") != leave.get("symbol")]
        ew = equal_weight_per_coin(rest)
        pooled_pnl = sum(float(r.get("net_pnl_usdt") or 0) for r in rest)
        rows.append(
            {
                "left_out_symbol": leave.get("symbol"),
                "left_out_net_pnl_usdt": float(leave.get("net_pnl_usdt") or 0),
                "remaining_n_coins": ew.get("n_coins"),
                "remaining_mean_expectancy_usdt": ew.get("mean_coin_expectancy_usdt"),
                "remaining_mean_net_pnl_usdt": ew.get("mean_coin_net_pnl_usdt"),
                "remaining_pooled_net_pnl_usdt": round(pooled_pnl, 6),
                "remaining_pooled_positive": pooled_pnl > 0,
            }
        )
    return rows


def leave_best_worst(coin_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not coin_rows:
        return {"leave_best": None, "leave_worst": None}
    by_pnl = sorted(coin_rows, key=lambda r: float(r.get("net_pnl_usdt") or 0))
    worst, best = by_pnl[0], by_pnl[-1]
    without_best = [r for r in coin_rows if r.get("symbol") != best.get("symbol")]
    without_worst = [r for r in coin_rows if r.get("symbol") != worst.get("symbol")]
    return {
        "best_coin": best.get("symbol"),
        "worst_coin": worst.get("symbol"),
        "leave_best": {
            "pooled_net_pnl_usdt": round(sum(float(r.get("net_pnl_usdt") or 0) for r in without_best), 6),
            **equal_weight_per_coin(without_best),
        },
        "leave_worst": {
            "pooled_net_pnl_usdt": round(sum(float(r.get("net_pnl_usdt") or 0) for r in without_worst), 6),
            **equal_weight_per_coin(without_worst),
        },
    }


def pnl_concentration(coin_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not coin_rows:
        return {"top1_share": None, "top3_share": None, "top5_share": None, "total_net_pnl_usdt": 0.0}
    ordered = sorted(coin_rows, key=lambda r: float(r.get("net_pnl_usdt") or 0), reverse=True)
    total = sum(float(r.get("net_pnl_usdt") or 0) for r in ordered)
    abs_total = abs(total) if total != 0 else sum(abs(float(r.get("net_pnl_usdt") or 0)) for r in ordered) or 1.0

    def share(k: int) -> float:
        s = sum(float(r.get("net_pnl_usdt") or 0) for r in ordered[:k])
        return round(s / abs_total, 6) if abs_total else 0.0

    return {
        "total_net_pnl_usdt": round(total, 6),
        "top1_symbol": ordered[0].get("symbol"),
        "top1_share": share(1),
        "top3_share": share(min(3, len(ordered))),
        "top5_share": share(min(5, len(ordered))),
        "top1_pnl_usdt": float(ordered[0].get("net_pnl_usdt") or 0),
        "dominates": share(1) >= 0.5 and total > 0,
    }


def long_short_split(trades: list[dict[str, Any]]) -> dict[str, Any]:
    from ..tpsl_pnl_engine import aggregate_strategy_stats

    valid = _valid(trades)
    long_t = [t for t in valid if str(t.get("direction", "")).upper() == "BULLISH"]
    short_t = [t for t in valid if str(t.get("direction", "")).upper() == "BEARISH"]
    return {
        "long": aggregate_strategy_stats(long_t),
        "short": aggregate_strategy_stats(short_t),
        "only_long_driven": bool(long_t)
        and (not short_t or float(aggregate_strategy_stats(short_t).get("net_pnl_usdt") or 0) <= 0)
        and float(aggregate_strategy_stats(long_t).get("net_pnl_usdt") or 0) > 0
        and float(aggregate_strategy_stats(valid).get("net_pnl_usdt") or 0) > 0
        and float(aggregate_strategy_stats(long_t).get("net_pnl_usdt") or 0)
        >= 0.9 * float(aggregate_strategy_stats(valid).get("net_pnl_usdt") or 0),
        "only_short_driven": bool(short_t)
        and (not long_t or float(aggregate_strategy_stats(long_t).get("net_pnl_usdt") or 0) <= 0)
        and float(aggregate_strategy_stats(short_t).get("net_pnl_usdt") or 0) > 0
        and float(aggregate_strategy_stats(valid).get("net_pnl_usdt") or 0) > 0
        and float(aggregate_strategy_stats(short_t).get("net_pnl_usdt") or 0)
        >= 0.9 * float(aggregate_strategy_stats(valid).get("net_pnl_usdt") or 0),
    }


def half_window_split(
    trades: list[dict[str, Any]],
    *,
    midpoint_iso: str,
) -> dict[str, Any]:
    from ..tpsl_pnl_engine import aggregate_strategy_stats

    valid = _valid(trades)
    first = [t for t in valid if str(t.get("entry_at", "")) < midpoint_iso]
    second = [t for t in valid if str(t.get("entry_at", "")) >= midpoint_iso]
    a = aggregate_strategy_stats(first)
    b = aggregate_strategy_stats(second)
    a_pnl = float(a.get("net_pnl_usdt") or 0)
    b_pnl = float(b.get("net_pnl_usdt") or 0)
    contradictory = (a_pnl > 0 and b_pnl < -abs(a_pnl)) or (b_pnl > 0 and a_pnl < -abs(b_pnl))
    return {
        "midpoint": midpoint_iso,
        "first_15d": a,
        "second_15d": b,
        "strongly_contradictory": contradictory,
    }


def one_position_per_symbol(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep chronologically first non-overlapping trade per symbol (no global slot cap)."""
    ordered = sorted(_valid(trades), key=lambda t: (t.get("symbol", ""), t.get("entry_at", ""), t.get("candidate_id", "")))
    out: list[dict] = []
    open_until: dict[str, str] = {}
    for t in ordered:
        sym = str(t.get("symbol", "")).upper()
        entry = str(t.get("entry_at") or "")
        exit_at = str(t.get("exit_at") or entry)
        prev = open_until.get(sym)
        if prev and entry < prev:
            continue
        out.append(t)
        open_until[sym] = exit_at
    return out


def deduped_episode(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for t in sorted(_valid(trades), key=lambda x: (x.get("entry_at", ""), x.get("candidate_id", ""))):
        key = (str(t.get("symbol", "")).upper(), str(t.get("cross_episode_id") or t.get("candidate_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def xrp_vs_coins(coin_rows: list[dict[str, Any]], *, xrp_symbol: str = "XRPUSDT") -> dict[str, Any]:
    if not coin_rows:
        return {"xrp_present": False}
    by_pnl = sorted(coin_rows, key=lambda r: float(r.get("net_pnl_usdt") or 0), reverse=True)
    symbols = [r.get("symbol") for r in by_pnl]
    xrp = next((r for r in coin_rows if r.get("symbol") == xrp_symbol), None)
    med = median_coin_result(coin_rows)
    if xrp is None:
        return {"xrp_present": False, "median_net_pnl_usdt": med.get("median_net_pnl_usdt")}
    xrp_pnl = float(xrp.get("net_pnl_usdt") or 0)
    rank = symbols.index(xrp_symbol) + 1 if xrp_symbol in symbols else None
    better = sum(1 for r in coin_rows if float(r.get("net_pnl_usdt") or 0) > xrp_pnl)
    worse = sum(1 for r in coin_rows if float(r.get("net_pnl_usdt") or 0) < xrp_pnl)
    med_pnl = float(med.get("median_net_pnl_usdt") or 0)
    return {
        "xrp_present": True,
        "xrp_net_pnl_usdt": xrp_pnl,
        "xrp_expectancy_usdt": float(xrp.get("avg_net_pnl_usdt") or 0),
        "xrp_rank": rank,
        "n_coins": len(coin_rows),
        "n_better_than_xrp": better,
        "n_worse_than_xrp": worse,
        "median_net_pnl_usdt": med_pnl,
        "xrp_vs_median": "BETTER" if xrp_pnl > med_pnl else ("WORSE" if xrp_pnl < med_pnl else "EQUAL"),
        "xrp_sample_flag": xrp.get("sample_flag"),
    }


def evaluate_robustness(
    *,
    pooled: dict[str, Any],
    coin_rows: list[dict[str, Any]],
    leave: dict[str, Any],
    concentration: dict[str, Any],
    halves: dict[str, Any],
    ls: dict[str, Any],
    adjacent_cells_similar: bool,
    n_eligible: int,
    cost_pct: float = PRIMARY_COST_PCT,
) -> dict[str, Any]:
    """Frozen robustness checklist — thresholds not tuned after seeing coins."""
    from .constants import MIN_ELIGIBLE_FOR_ROBUST

    checks: dict[str, bool] = {}
    net = float(pooled.get("net_pnl_usdt") or 0)
    exp = float(pooled.get("avg_net_pnl_usdt") or 0)
    pf = pooled.get("profit_factor_net")
    med = median_coin_result(coin_rows)
    ew = equal_weight_per_coin(coin_rows)
    leave_best_pnl = float((leave.get("leave_best") or {}).get("pooled_net_pnl_usdt") or 0)

    checks["net_pnl_positive_015"] = cost_pct == 0.15 and net > 0
    checks["expectancy_positive"] = exp > 0
    checks["pf_gt_1"] = pf is not None and float(pf) > 1
    checks["n_eligible_ge_10"] = n_eligible >= MIN_ELIGIBLE_FOR_ROBUST
    non_neg_share = (
        sum(1 for r in coin_rows if float(r.get("net_pnl_usdt") or 0) >= 0) / len(coin_rows) if coin_rows else 0.0
    )
    checks["breadth_ok"] = non_neg_share >= 0.60 or float(med.get("median_expectancy_usdt") or 0) > 0
    checks["leave_best_still_positive"] = leave_best_pnl > 0
    checks["not_dominated"] = not bool(concentration.get("dominates"))
    checks["adjacent_cells_similar"] = adjacent_cells_similar
    checks["halves_not_contradictory"] = not bool(halves.get("strongly_contradictory"))
    checks["not_only_long_or_short"] = not bool(ls.get("only_long_driven")) and not bool(ls.get("only_short_driven"))

    ok = all(checks.values()) and len(coin_rows) >= MIN_ELIGIBLE_FOR_ROBUST
    return {
        "checks": checks,
        "passed": ok,
        "label": "MULTICOIN_ROBUST_CANDIDATE" if ok else "NOT_MULTICOIN_ROBUST",
        "equal_weight": ew,
        "median_coin": med,
        "pct_coins_non_negative": round(non_neg_share, 6),
    }
