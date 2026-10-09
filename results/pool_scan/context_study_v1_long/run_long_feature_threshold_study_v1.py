#!/usr/bin/env python3
"""Long geometry feature threshold / combo study (research only)."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LONG_ROOT = Path(__file__).resolve().parent
ROWS_PATH = LONG_ROOT / "long_geometry_case_control_v1" / "case_control_rows.csv"
OUT_DIR = LONG_ROOT / "long_feature_threshold_study_v1"

BASELINE = {
    "trades": 169,
    "pnl_sum_pct": 126.4098,
    "profit_factor": 3.0596,
    "max_drawdown_pct": 9.047,
    "max_sl_streak": 10,
    "pnl_per_dd": 126.4098 / 9.047,
}
BASELINE_COIN = {
    "XRPUSDT": {"trades": 55, "pnl_sum_pct": 32.03, "profit_factor": 3.1319, "max_drawdown_pct": 2.6005, "max_sl_streak": 4},
    "ADAUSDT": {"trades": 56, "pnl_sum_pct": 71.2109, "profit_factor": 4.2546, "max_drawdown_pct": 5.6354, "max_sl_streak": 6},
    "DOGEUSDT": {"trades": 58, "pnl_sum_pct": 23.1689, "profit_factor": 1.9468, "max_drawdown_pct": 7.1988, "max_sl_streak": 7},
}

ALLOWED_FILTER_FIELDS = {
    "entry_pool_age_h",
    "m15_lower_2_age_h",
    "tp_pool_age_h",
    "m15_nearest_upper_age_h",
    "prior_24bar_return_pct",
    "m4h_lowers_within_1.0pct",
    "ema30m_dist_ema9_pct",
    "ema15m_ema20_slope_3b_pct",
    "prior_4h_range_pct",
    "tp_distance_pct",
    "rr",
}

FORBIDDEN_IN_FILTER = {"mae_pct", "mfe_pct", "pnl_pct", "outcome", "first_hit"}


def _float(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v is None or v == "":
        return None
    return float(v)


def _is_tp(row: dict) -> bool:
    return row.get("first_hit") == "TP" or row.get("outcome") == "TP_FIRST"


def _metrics(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "TP": 0,
            "SL": 0,
            "win_rate": None,
            "pnl_sum_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "max_sl_streak": 0,
            "median_mae_pct": None,
            "median_mfe_pct": None,
            "pnl_per_dd": None,
        }
    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        p = float(t["pnl_pct"]) if t.get("pnl_pct") is not None else 0.0
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.get("first_hit") == "SL":
            streak += 1
            max_streak = max(max_streak, streak)
        elif t.get("first_hit") == "TP":
            streak = 0
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if _is_tp(t)),
        "SL": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "pnl_sum_pct": round(sum(pnls), 4) if pnls else 0.0,
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_sl_streak": max_streak,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
        "pnl_per_dd": round(sum(pnls) / max_dd, 4) if pnls and max_dd > 0 else None,
    }


def _month_label(entry_time: str) -> str:
    dt = datetime.fromisoformat(entry_time)
    return "june" if dt.month == 6 else "july"


def _sample_flags(n: int, per_coin: dict[str, int]) -> list[str]:
    flags = []
    if n < 50:
        flags.append("VERY_SMALL_SAMPLE")
    elif n < 80:
        flags.append("SMALL_SAMPLE")
    for sym, c in per_coin.items():
        if c < 15:
            flags.append(f"COIN_SAMPLE_WEAK:{sym}")
    return flags


def _coin_stability(filtered: list[dict], all_rows: list[dict]) -> str:
    hits = 0
    for sym in ("XRPUSDT", "ADAUSDT", "DOGEUSDT"):
        sub = [r for r in filtered if r["symbol"] == sym]
        base = [r for r in all_rows if r["symbol"] == sym]
        if len(sub) < 10:
            continue
        m = _metrics(sub)
        b = _metrics(base)
        better = (
            m["max_drawdown_pct"] <= b["max_drawdown_pct"] * 1.05
            and (m["profit_factor"] or 0) >= (b["profit_factor"] or 0) * 0.9
        )
        if better:
            hits += 1
    if hits >= 3:
        return "STABLE_3COIN"
    if hits == 2:
        return "STABLE_2COIN"
    if hits == 1:
        return "COIN_SPECIFIC"
    return "UNSTABLE"


def _month_stability(filtered: list[dict]) -> str:
    june = [r for r in filtered if _month_label(r["entry_time"]) == "june"]
    july = [r for r in filtered if _month_label(r["entry_time"]) == "july"]
    if len(june) < 20 or len(july) < 20:
        return "UNSTABLE"
    mj, ml = _metrics(june), _metrics(july)
    ok_j = (mj["profit_factor"] or 0) >= 1.0 and mj["pnl_sum_pct"] > 0
    ok_l = (ml["profit_factor"] or 0) >= 1.0 and ml["pnl_sum_pct"] > 0
    if ok_j and ok_l:
        return "BOTH_MONTHS_OK"
    if ok_j:
        return "JUNE_ONLY"
    if ok_l:
        return "JULY_ONLY"
    return "UNSTABLE"


def _vs_baseline(m: dict) -> dict:
    dd_red = BASELINE["max_drawdown_pct"] - m["max_drawdown_pct"]
    return {
        "trade_retention_pct": round(m["trades"] / BASELINE["trades"] * 100.0, 2),
        "pnl_retention_pct": round(m["pnl_sum_pct"] / BASELINE["pnl_sum_pct"] * 100.0, 2)
        if BASELINE["pnl_sum_pct"]
        else None,
        "dd_reduction_abs": round(dd_red, 4),
        "dd_reduction_pct": round(dd_red / BASELINE["max_drawdown_pct"] * 100.0, 2),
        "streak_reduction": BASELINE["max_sl_streak"] - m["max_sl_streak"],
    }


def _rank_score(row: dict) -> float:
    vb = row["vs_baseline"]
    score = 0.0
    score += vb["dd_reduction_pct"] * 2.0
    score += vb["streak_reduction"] * 3.0
    pf = row.get("profit_factor") or 0
    score += min(pf, 5) * 5.0
    score += min(vb["trade_retention_pct"], 100) * 0.15
    score += min(vb["pnl_retention_pct"] or 0, 150) * 0.08
    stab = row.get("cross_coin_stability", "")
    if stab == "STABLE_3COIN":
        score += 25
    elif stab == "STABLE_2COIN":
        score += 12
    month = row.get("month_stability", "")
    if month == "BOTH_MONTHS_OK":
        score += 20
    flags = row.get("sample_flags", [])
    if "VERY_SMALL_SAMPLE" in flags:
        score -= 40
    elif "SMALL_SAMPLE" in flags:
        score -= 15
    return score


def _apply(rows: list[dict], pred: Callable[[dict], bool]) -> list[dict]:
    return [r for r in rows if pred(r)]


def _build_variants(rows: list[dict]) -> list[tuple[str, Callable[[dict], bool], list[str]]]:
    variants: list[tuple[str, Callable[[dict], bool], list[str]]] = []

    def add(name: str, fields: list[str], pred: Callable[[dict], bool]) -> None:
        variants.append((name, pred, fields))

    for thr in (6, 12, 24, 36, 48):
        add(
            f"entry_pool_age_h<={thr}h",
            ["entry_pool_age_h"],
            lambda r, t=thr: (_float(r, "entry_pool_age_h") is not None and _float(r, "entry_pool_age_h") <= t),
        )
    for thr in (12, 24, 48, 72):
        add(
            f"m15_lower_2_age_h<={thr}h",
            ["m15_lower_2_age_h"],
            lambda r, t=thr: (_float(r, "m15_lower_2_age_h") is not None and _float(r, "m15_lower_2_age_h") <= t),
        )
    for thr in (3, 6, 12, 24):
        add(
            f"tp_pool_age_h>={thr}h",
            ["tp_pool_age_h"],
            lambda r, t=thr: (_float(r, "tp_pool_age_h") is not None and _float(r, "tp_pool_age_h") >= t),
        )
        add(
            f"m15_nearest_upper_age_h>={thr}h",
            ["m15_nearest_upper_age_h"],
            lambda r, t=thr: (
                _float(r, "m15_nearest_upper_age_h") is not None and _float(r, "m15_nearest_upper_age_h") >= t
            ),
        )
    for thr in (-2.0, -1.5, -1.0, -0.5):
        add(
            f"prior_24bar_return_pct>{thr}",
            ["prior_24bar_return_pct"],
            lambda r, t=thr: (_float(r, "prior_24bar_return_pct") is not None and _float(r, "prior_24bar_return_pct") > t),
        )
    for rule, pred in (
        ("m4h_lowers_within_1.0pct==0", lambda r: _float(r, "m4h_lowers_within_1.0pct") == 0),
        ("m4h_lowers_within_1.0pct<=1", lambda r: _float(r, "m4h_lowers_within_1.0pct") is not None and _float(r, "m4h_lowers_within_1.0pct") <= 1),
        ("m4h_lowers_within_1.0pct<=2", lambda r: _float(r, "m4h_lowers_within_1.0pct") is not None and _float(r, "m4h_lowers_within_1.0pct") <= 2),
    ):
        add(rule, ["m4h_lowers_within_1.0pct"], pred)

    ema9_vals = sorted(_float(r, "ema30m_dist_ema9_pct") for r in rows if _float(r, "ema30m_dist_ema9_pct") is not None)
    for q in (0.25, 0.5, 0.75):
        thr = ema9_vals[int(q * (len(ema9_vals) - 1))]
        add(
            f"ema30m_dist_ema9_pct>={thr:.4f}(q{int(q*100)})",
            ["ema30m_dist_ema9_pct"],
            lambda r, t=thr: (_float(r, "ema30m_dist_ema9_pct") is not None and _float(r, "ema30m_dist_ema9_pct") >= t),
        )
    slope_vals = sorted(
        _float(r, "ema15m_ema20_slope_3b_pct") for r in rows if _float(r, "ema15m_ema20_slope_3b_pct") is not None
    )
    for q in (0.25, 0.5, 0.75):
        thr = slope_vals[int(q * (len(slope_vals) - 1))]
        add(
            f"ema15m_ema20_slope_3b_pct>={thr:.6f}(q{int(q*100)})",
            ["ema15m_ema20_slope_3b_pct"],
            lambda r, t=thr: (
                _float(r, "ema15m_ema20_slope_3b_pct") is not None and _float(r, "ema15m_ema20_slope_3b_pct") >= t
            ),
        )

    # secondary
    for thr in (1.5, 2.0, 2.5):
        add(
            f"prior_4h_range_pct>={thr}",
            ["prior_4h_range_pct"],
            lambda r, t=thr: (_float(r, "prior_4h_range_pct") is not None and _float(r, "prior_4h_range_pct") >= t),
        )
    for thr in (1.8, 2.0, 2.5):
        add(
            f"tp_distance_pct>={thr}",
            ["tp_distance_pct"],
            lambda r, t=thr: (_float(r, "tp_distance_pct") is not None and _float(r, "tp_distance_pct") >= t),
        )

    return variants


def _evaluate(name: str, fields: list[str], pred: Callable[[dict], bool], rows: list[dict]) -> dict:
    for f in fields:
        if f in FORBIDDEN_IN_FILTER or f not in ALLOWED_FILTER_FIELDS:
            raise ValueError(f"filter field not allowed: {f}")
    filtered = _apply(rows, pred)
    m = _metrics(filtered)
    per_coin = {}
    for sym in ("XRPUSDT", "ADAUSDT", "DOGEUSDT"):
        per_coin[sym] = _metrics([r for r in filtered if r["symbol"] == sym])
    per_coin_n = {sym: per_coin[sym]["trades"] for sym in per_coin}
    flags = _sample_flags(m["trades"], per_coin_n)
    vb = _vs_baseline(m)
    out = {
        "variant": name,
        "filter_fields": fields,
        **m,
        "vs_baseline": vb,
        "per_coin": per_coin,
        "sample_flags": flags,
        "cross_coin_stability": _coin_stability(filtered, rows),
        "month_stability": _month_stability(filtered),
        "june": _metrics([r for r in filtered if _month_label(r["entry_time"]) == "june"]),
        "july": _metrics([r for r in filtered if _month_label(r["entry_time"]) == "july"]),
    }
    out["rank_score"] = _rank_score(out)
    return out


def _combo(name: str, preds: list[tuple[str, Callable[[dict], bool], list[str]]]) -> tuple[str, Callable[[dict], bool], list[str]]:
    fields: list[str] = []
    for _, _, fs in preds:
        fields.extend(fs)

    def pred(r: dict) -> bool:
        return all(p(r) for _, p, _ in preds)

    return (name, pred, fields)


def main() -> None:
    rows: list[dict] = []
    with ROWS_PATH.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if len(rows) != 169:
        raise SystemExit(f"expected 169 rows, got {len(rows)}")

    baseline_m = _metrics(rows)
    assert abs(baseline_m["pnl_sum_pct"] - BASELINE["pnl_sum_pct"]) < 2.0

    singles = _build_variants(rows)
    single_results = [_evaluate(n, f, p, rows) for n, p, f in singles]
    single_results.sort(key=lambda x: -x["rank_score"])

    # top singles for combos (by rank, diverse features)
    top_names = []
    seen_feat: set[str] = set()
    for r in single_results:
        feat = r["filter_fields"][0]
        if feat in seen_feat:
            continue
        if r["trades"] < 40:
            continue
        seen_feat.add(feat)
        top_names.append(r["variant"])
        if len(top_names) >= 5:
            break

    pred_map = {n: (p, f) for n, p, f in singles}
    combo_defs: list[tuple[str, Callable, list[str]]] = []
    # pairwise among top 4
    top4 = top_names[:4]
    for i in range(len(top4)):
        for j in range(i + 1, len(top4)):
            a, b = top4[i], top4[j]
            pa, fa = pred_map[a]
            pb, fb = pred_map[b]
            combo_defs.append(_combo(f"COMBO:{a} AND {b}", [(a, pa, fa), (b, pb, fb)]))

    # curated triples
    curated = [
        ("COMBO:young_entry+mild_prior+old_tp", [
            ("entry_pool_age_h<=24h", pred_map["entry_pool_age_h<=24h"][0], pred_map["entry_pool_age_h<=24h"][1]),
            ("prior_24bar_return_pct>-1.0", pred_map["prior_24bar_return_pct>-1.0"][0], pred_map["prior_24bar_return_pct>-1.0"][1]),
            ("tp_pool_age_h>=6h", pred_map["tp_pool_age_h>=6h"][0], pred_map["tp_pool_age_h>=6h"][1]),
        ]),
        ("COMBO:young_entry+0_4h_lowers", [
            ("entry_pool_age_h<=24h", pred_map["entry_pool_age_h<=24h"][0], pred_map["entry_pool_age_h<=24h"][1]),
            ("m4h_lowers_within_1.0pct==0", pred_map["m4h_lowers_within_1.0pct==0"][0], pred_map["m4h_lowers_within_1.0pct==0"][1]),
        ]),
        ("COMBO:young_ladder+mild_prior", [
            ("m15_lower_2_age_h<=48h", pred_map["m15_lower_2_age_h<=48h"][0], pred_map["m15_lower_2_age_h<=48h"][1]),
            ("prior_24bar_return_pct>-1.0", pred_map["prior_24bar_return_pct>-1.0"][0], pred_map["prior_24bar_return_pct>-1.0"][1]),
        ]),
    ]
    for name, parts in curated:
        combo_defs.append(_combo(name, parts))

    combo_results = [_evaluate(n, f, p, rows) for n, p, f in combo_defs]
    combo_results.sort(key=lambda x: -x["rank_score"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def _flat_single(r: dict) -> dict:
        feat = r["filter_fields"][0] if r["filter_fields"] else ""
        return {
            "variant": r["variant"],
            "feature": feat,
            "threshold": r["variant"],
            "trades": r["trades"],
            "pnl_sum_pct": r["pnl_sum_pct"],
            "profit_factor": r["profit_factor"],
            "max_drawdown_pct": r["max_drawdown_pct"],
            "max_sl_streak": r["max_sl_streak"],
            "dd_reduction_pct": r["vs_baseline"]["dd_reduction_pct"],
            "streak_reduction": r["vs_baseline"]["streak_reduction"],
            "pnl_retention_pct": r["vs_baseline"]["pnl_retention_pct"],
            "trade_retention_pct": r["vs_baseline"]["trade_retention_pct"],
            "pnl_per_dd": r["pnl_per_dd"],
            "cross_coin_stability": r["cross_coin_stability"],
            "month_stability": r["month_stability"],
            "sample_flags": ";".join(r["sample_flags"]),
            "rank_score": round(r["rank_score"], 2),
        }

    with (OUT_DIR / "single_feature_results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(_flat_single(single_results[0]).keys()))
        w.writeheader()
        for r in single_results:
            w.writerow(_flat_single(r))

    with (OUT_DIR / "combo_results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(_flat_single(combo_results[0]).keys()) if combo_results else ["variant"])
        w.writeheader()
        for r in combo_results:
            w.writerow(_flat_single(r))

    monthly_rows = []
    for r in single_results[:15] + combo_results[:10]:
        for period in ("june", "july"):
            pm = r[period]
            monthly_rows.append(
                {
                    "variant": r["variant"],
                    "period": period,
                    "trades": pm["trades"],
                    "pnl_sum_pct": pm["pnl_sum_pct"],
                    "profit_factor": pm["profit_factor"],
                    "max_drawdown_pct": pm["max_drawdown_pct"],
                    "max_sl_streak": pm["max_sl_streak"],
                    "month_stability": r["month_stability"],
                }
            )
    with (OUT_DIR / "monthly_stability.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(monthly_rows[0].keys()))
        w.writeheader()
        w.writerows(monthly_rows)

    coin_rows = []
    for r in single_results[:12] + combo_results[:8]:
        for sym in ("XRPUSDT", "ADAUSDT", "DOGEUSDT"):
            cm = r["per_coin"][sym]
            b = BASELINE_COIN[sym]
            coin_rows.append(
                {
                    "variant": r["variant"],
                    "symbol": sym,
                    "trades": cm["trades"],
                    "pnl_sum_pct": cm["pnl_sum_pct"],
                    "profit_factor": cm["profit_factor"],
                    "max_drawdown_pct": cm["max_drawdown_pct"],
                    "max_sl_streak": cm["max_sl_streak"],
                    "baseline_pf": b["profit_factor"],
                    "baseline_dd": b["max_drawdown_pct"],
                    "pf_vs_baseline": round((cm["profit_factor"] or 0) - b["profit_factor"], 4),
                    "dd_vs_baseline": round(cm["max_drawdown_pct"] - b["max_drawdown_pct"], 4),
                }
            )
    with (OUT_DIR / "coin_stability.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(coin_rows[0].keys()))
        w.writeheader()
        w.writerows(coin_rows)

    leakage = {
        "pass": True,
        "allowed_fields": sorted(ALLOWED_FILTER_FIELDS),
        "forbidden_never_used": sorted(FORBIDDEN_IN_FILTER),
        "note": "All filters use entry-time columns from case_control_rows only.",
    }

    # pick top 5 candidates
    pool = sorted(single_results + combo_results, key=lambda x: -x["rank_score"])
    candidates = []
    for r in pool:
        if r["trades"] < 50:
            continue
        if r["vs_baseline"]["dd_reduction_pct"] < 5 and r["vs_baseline"]["streak_reduction"] < 2:
            continue
        candidates.append(r)
        if len(candidates) >= 5:
            break
    if len(candidates) < 5:
        for r in pool:
            if r in candidates:
                continue
            candidates.append(r)
            if len(candidates) >= 5:
                break

    top_candidates = []
    for i, r in enumerate(candidates[:5], 1):
        top_candidates.append(
            {
                "candidate_id": i,
                "rules": r["variant"],
                "results": {k: r[k] for k in ("trades", "TP", "SL", "pnl_sum_pct", "profit_factor", "max_drawdown_pct", "max_sl_streak", "median_mae_pct", "median_mfe_pct")},
                "vs_baseline": r["vs_baseline"],
                "per_coin": r["per_coin"],
                "june": r["june"],
                "july": r["july"],
                "cross_coin_stability": r["cross_coin_stability"],
                "month_stability": r["month_stability"],
                "sample_flags": r["sample_flags"],
            }
        )

    good = [
        r
        for r in pool
        if r["trades"] >= 80
        and r["vs_baseline"]["dd_reduction_pct"] >= 10
        and r["month_stability"] == "BOTH_MONTHS_OK"
        and r["cross_coin_stability"] in ("STABLE_3COIN", "STABLE_2COIN")
    ]
    if good:
        verdict = "LONG_SIMPLE_FILTER_CANDIDATES_FOUND"
    elif any(r["trades"] >= 60 and r["vs_baseline"]["dd_reduction_pct"] >= 5 for r in pool):
        verdict = "LONG_FILTER_SIGNAL_WEAK"
    else:
        verdict = "LONG_FILTER_STUDY_INCONCLUSIVE"

    summary = {
        "baseline": {**BASELINE, **baseline_m},
        "baseline_per_coin": BASELINE_COIN,
        "single_count": len(single_results),
        "combo_count": len(combo_results),
        "top10_single": [_flat_single(r) for r in single_results[:10]],
        "top_combos": [_flat_single(r) for r in combo_results[:8]],
        "dd_focus": sorted(
            [r for r in pool if r["trades"] >= 40],
            key=lambda x: (-x["vs_baseline"]["dd_reduction_pct"], -x["vs_baseline"]["pnl_retention_pct"]),
        )[:12],
        "leakage_audit": leakage,
        "top_candidates": top_candidates,
        "final_verdict": verdict,
    }
    with (OUT_DIR / "top_candidates.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    (OUT_DIR / "method.md").write_text(
        """# Long feature threshold study V1

Filters applied to `case_control_rows.csv` (169 geometry trades, Jun–Jul 2026).
Entry-time features only. No strategy code changes.

Ranking favors DD reduction and SL streak reduction over raw PnL.
""",
        encoding="utf-8",
    )

    print("VERDICT", verdict)
    print("TOP SINGLE", single_results[0]["variant"], single_results[0]["max_drawdown_pct"], single_results[0]["trades"])
    if combo_results:
        print("TOP COMBO", combo_results[0]["variant"], combo_results[0]["max_drawdown_pct"])


if __name__ == "__main__":
    main()
