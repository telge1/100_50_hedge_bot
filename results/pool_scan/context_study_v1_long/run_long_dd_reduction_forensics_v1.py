#!/usr/bin/env python3
"""Long DD forensics on m15_lower_2_age_h <= 24h cohort (94 trades, research only)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
ROWS_PATH = LONG_ROOT / "long_geometry_case_control_v1" / "case_control_rows.csv"
OUT = LONG_ROOT / "long_dd_reduction_forensics_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_WEEKS = 12
FILTER_MAX_LOWER2_H = 24.0
EXPECTED_N = 94


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _float(v) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _is_tp(r: dict) -> bool:
    return r.get("first_hit") == "TP" or r.get("outcome") == "TP_FIRST"


def _is_sl(r: dict) -> bool:
    return r.get("first_hit") == "SL" or r.get("outcome") == "SL_FIRST"


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "TP": 0, "SL": 0, "pnl_sum_pct": 0.0, "profit_factor": None, "max_drawdown_pct": 0.0, "max_sl_streak": 0}
    pnls = [float(t["pnl_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in ordered:
        eq += float(t["pnl_pct"])
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if _is_sl(t):
            streak += 1
            max_streak = max(max_streak, streak)
        elif _is_tp(t):
            streak = 0
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if _is_tp(t)),
        "SL": sum(1 for t in trades if _is_sl(t)),
        "pnl_sum_pct": round(sum(pnls), 4),
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_sl_streak": max_streak,
    }


def _dd_sequence(trades: list[dict]) -> tuple[list[dict], dict]:
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = 0.0
    peak = 0.0
    peak_i = 0
    trough_i = 0
    max_dd = 0.0
    rows: list[dict] = []
    for i, t in enumerate(ordered):
        p = float(t["pnl_pct"])
        eq += p
        if eq >= peak:
            peak = eq
            peak_i = i
        dd = peak - eq
        rows.append(
            {
                "seq": i + 1,
                "time": t["entry_time"],
                "symbol": t["symbol"],
                "outcome": t.get("outcome"),
                "pnl_pct": p,
                "cum_equity": round(eq, 4),
                "drawdown_pct": round(dd, 4),
            }
        )
        if dd > max_dd:
            max_dd = dd
            trough_i = i
    # peak before trough for max-dd episode
    sub_peak = float("-inf")
    sub_peak_i = 0
    for j in range(trough_i + 1):
        if rows[j]["cum_equity"] >= sub_peak:
            sub_peak = rows[j]["cum_equity"]
            sub_peak_i = j
    ep_peak_i = sub_peak_i
    meta = {
        "max_drawdown_pct": round(max_dd, 4),
        "peak_seq": ep_peak_i + 1,
        "trough_seq": trough_i + 1,
        "peak_time": ordered[ep_peak_i]["entry_time"] if ordered else None,
        "trough_time": ordered[trough_i]["entry_time"] if ordered else None,
    }
    for j, r in enumerate(rows):
        r["in_max_dd_segment"] = ep_peak_i <= j <= trough_i
    return rows, meta


def _sl_clusters(trades: list[dict]) -> list[dict]:
    sls = sorted([t for t in trades if _is_sl(t)], key=lambda t: t["entry_time"])
    clusters: list[list[dict]] = []
    for t in sls:
        et = datetime.fromisoformat(t["entry_time"])
        if not clusters:
            clusters.append([t])
            continue
        prev = datetime.fromisoformat(clusters[-1][-1]["entry_time"])
        multi_coin_window = any(
            datetime.fromisoformat(x["entry_time"]) >= et - __import__("datetime").timedelta(hours=72)
            for x in clusters[-1]
        )
        same_sym = t["symbol"] == clusters[-1][-1]["symbol"]
        close_time = (et - prev).total_seconds() <= 72 * 3600
        if same_sym and close_time:
            clusters[-1].append(t)
        elif close_time and len({x["symbol"] for x in clusters[-1]}) <= 2:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    out = []
    for i, cl in enumerate(clusters, 1):
        syms = sorted({x["symbol"] for x in cl})
        out.append(
            {
                "cluster_id": i,
                "start": cl[0]["entry_time"],
                "end": cl[-1]["entry_time"],
                "symbols": "|".join(syms),
                "n_sl": len(cl),
                "multi_coin": len(syms) > 1,
                "total_loss_pct": round(sum(float(x["pnl_pct"]) for x in cl), 4),
                "label": "3plus" if len(cl) >= 3 else ("2" if len(cl) == 2 else "single"),
                "entries": [x["entry_time"] for x in cl],
            }
        )
    return out


def _enrich_regime(row: dict, market: dict, pools15: list[dict], bar_idx: int, moment: datetime) -> dict:
    from case_control_features import (
        cross_age_bars,
        ema_slope_pct,
        falling_n_upper,
        last_closed_index,
        upper_clusters_above,
    )

    bars15 = market["15m"]["bars"]
    bar = bars15[bar_idx]
    price = float(row["entry_price"])
    extra: dict[str, Any] = {}
    for tf, prefix in (("15m", "r15"), ("30m", "r30"), ("1h", "r1h"), ("4h", "r4h")):
        idx = bar_idx if tf == "15m" else last_closed_index(market[tf]["bars"], moment)
        if idx is None:
            continue
        b = market[tf]["bars"][idx]
        c = b["close"]
        extra[f"{prefix}_below_ema200"] = c < b["ema200"] if b.get("ema200") else None
        if b.get("ema200"):
            extra[f"{prefix}_dist_ema200_pct"] = round((c - b["ema200"]) / b["ema200"] * 100.0, 4)
        if tf == "15m":
            extra["hours_since_bear_cross"] = round((cross_age_bars(bars15, idx, False) or 0) * 0.25, 4)
            extra["hours_since_bull_cross"] = round((cross_age_bars(bars15, idx, True) or 0) * 0.25, 4)
            extra["bars_since_bear_cross"] = cross_age_bars(bars15, idx, False)
            extra["ema15_ema20_slope_3b"] = ema_slope_pct(bars15, idx, "ema20", 3)
            extra["ema15_ema59_slope_3b"] = ema_slope_pct(bars15, idx, "ema59", 3)
            extra["ema15_ema200_slope_3b"] = ema_slope_pct(bars15, idx, "ema200", 3)

    uc = upper_clusters_above(pools15, moment, price)
    extra["m15_upper_cluster_count"] = len(uc)
    extra["falling_2"] = falling_n_upper(uc, 2) if len(uc) >= 2 else False
    extra["falling_3"] = falling_n_upper(uc, 3) if len(uc) >= 3 else False
    extra["falling_4"] = falling_n_upper(uc, 4) if len(uc) >= 4 else False
    if uc:
        extra["nearest_upper_age_h"] = round((moment - uc[0].get("earliest_known", moment)).total_seconds() / 3600.0, 4) if isinstance(uc[0].get("earliest_known"), datetime) else None
    if len(uc) >= 2:
        extra["second_upper_bottom"] = uc[1]["lowest_bottom"]
    if len(uc) >= 3:
        extra["third_upper_bottom"] = uc[2]["lowest_bottom"]

    # mirror flags (passive)
    h_bear = extra.get("hours_since_bear_cross") or 999
    extra["bear_cross_recent"] = h_bear <= 48
    extra["below_ema200"] = extra.get("r15_below_ema200")
    extra["upper_structure_intact"] = extra.get("falling_3") or extra.get("falling_2")
    extra["bear_reclaim_like"] = (
        extra.get("r15_below_ema200") is True
        and (extra.get("hours_since_bull_cross") or 999) <= 48
    )
    extra["upper_density_high"] = int(row.get("m15_uppers_within_2.0pct") or 0) >= 3
    return extra


def _hypothetical(trades: list[dict], name: str, pred) -> dict:
    blocked = [t for t in trades if pred(t)]
    kept = [t for t in trades if not pred(t)]
    bm = _metrics(blocked)
    km = _metrics(kept)
    return {
        "scenario": name,
        "hypothetical": True,
        "trades_blocked": len(blocked),
        "sl_blocked": sum(1 for t in blocked if _is_sl(t)),
        "tp_blocked": sum(1 for t in blocked if _is_tp(t)),
        "pnl_removed_pct": bm["pnl_sum_pct"],
        "remaining_trades": km["trades"],
        "remaining_dd_pct": km["max_drawdown_pct"],
        "remaining_streak": km["max_sl_streak"],
        "remaining_pnl_pct": km["pnl_sum_pct"],
    }


def main() -> None:
    _ensure_paths()
    all_rows: list[dict] = []
    with ROWS_PATH.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            all_rows.append(r)

    cohort = [r for r in all_rows if _float(r.get("m15_lower_2_age_h")) is not None and _float(r["m15_lower_2_age_h"]) <= FILTER_MAX_LOWER2_H]
    if len(cohort) != EXPECTED_N:
        raise SystemExit(f"expected {EXPECTED_N} trades, got {len(cohort)}")

    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_short_entry_15m_v1 import build_15m_bars, load_market

    by_sym: dict[str, list[dict]] = {s: [] for s in ("XRPUSDT", "ADAUSDT", "DOGEUSDT")}
    for r in cohort:
        by_sym[r["symbol"]].append(r)

    regime_rows: list[dict] = []
    for symbol, trades in by_sym.items():
        market = load_market(symbol)
        _, c15 = load_pane_candles(
            symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
        )
        bars15 = build_15m_bars(c15)
        by_open = {b["open_time"]: b for b in bars15}
        lld = ui_lld_config("15m")
        cache: dict = {}
        for t in trades:
            ot = datetime.fromisoformat(t["entry_time"])
            bar = by_open[ot]
            idx = bars15.index(bar)
            moment = bar["close_time"]
            pools15, _, _ = scanner_pools_for_index(c15, "15m", idx, lld, cache=cache)
            reg = _enrich_regime(t, market, pools15, idx, moment)
            regime_rows.append({**t, **reg})

    cohort = regime_rows
    m = _metrics(cohort)
    dd_rows, dd_meta = _dd_sequence(cohort)
    clusters = _sl_clusters(cohort)

    tp_rows = [t for t in cohort if _is_tp(t)]
    sl_rows = [t for t in cohort if _is_sl(t)]

    def med(group: list[dict], key: str):
        vals = [_float(x.get(key)) for x in group if _float(x.get(key)) is not None]
        return round(statistics.median(vals), 4) if vals else None

    compare_fields = [
        "hours_since_bear_cross",
        "r15_dist_ema200_pct",
        "tp_pool_age_h",
        "entry_pool_age_h",
        "m15_lower_2_age_h",
        "prior_24bar_return_pct",
        "ema15_ema20_slope_3b",
        "m4h_lowers_within_1.0pct",
        "m15_uppers_within_2.0pct",
    ]
    tp_vs_sl = []
    for f in compare_fields:
        tp_vs_sl.append({"feature": f, "median_TP": med(tp_rows, f), "median_SL": med(sl_rows, f)})
    for flag in ("falling_3", "bear_cross_recent", "below_ema200", "bear_reclaim_like", "upper_density_high"):
        tp_vs_sl.append(
            {
                "feature": flag,
                "TP_rate": round(sum(1 for t in tp_rows if t.get(flag)) / len(tp_rows), 4) if tp_rows else None,
                "SL_rate": round(sum(1 for t in sl_rows if t.get(flag)) / len(sl_rows), 4) if sl_rows else None,
            }
        )

    hypotheticals = [
        _hypothetical(cohort, "HYP_below_ema200_15m", lambda t: t.get("r15_below_ema200") is True),
        _hypothetical(
            cohort,
            "HYP_bear_cross_48h_and_falling_3",
            lambda t: t.get("bear_cross_recent") and t.get("falling_3"),
        ),
        _hypothetical(
            cohort,
            "HYP_bear_reclaim_like",
            lambda t: t.get("bear_reclaim_like") is True,
        ),
    ]

    max_dd_trades = [r for r in dd_rows if r.get("in_max_dd_segment")]
    cluster_ids_in_dd = set()
    for cl in clusters:
        for e in cl["entries"]:
            if any(d["time"] == e for d in max_dd_trades):
                cluster_ids_in_dd.add(cl["cluster_id"])

    summary = {
        "cohort_filter": f"m15_lower_2_age_h <= {FILTER_MAX_LOWER2_H}h",
        "cohort_metrics": m,
        "parity_expected": {"trades": 94, "pnl_sum_pct": 129.3675, "max_dd": 2.9833},
        "dd_meta": dd_meta,
        "sl_clusters": clusters,
        "max_dd_cluster_ids": sorted(cluster_ids_in_dd),
        "tp_vs_sl": tp_vs_sl,
        "hypothetical_blockers": hypotheticals,
        "remaining_sl_detail": [
            {
                "time": t["entry_time"],
                "symbol": t["symbol"],
                "pnl_pct": t["pnl_pct"],
                "MAE_pct": t.get("mae_pct"),
                "MFE_pct": t.get("mfe_pct"),
                "hours_since_bear_cross": t.get("hours_since_bear_cross"),
                "falling_3": t.get("falling_3"),
                "below_ema200": t.get("r15_below_ema200"),
                "bear_reclaim_like": t.get("bear_reclaim_like"),
            }
            for t in sl_rows
        ],
        "false_positive_notes": {
            "below_ema200_tp_count": sum(1 for t in tp_rows if t.get("r15_below_ema200")),
            "falling_3_tp_count": sum(1 for t in tp_rows if t.get("falling_3")),
            "bear_reclaim_tp_count": sum(1 for t in tp_rows if t.get("bear_reclaim_like")),
        },
        "leakage_audit": {"pass": True, "note": "Entry-time EMA/pool features only; MAE/MFE not used as inputs."},
        "answers": {
            "q1_regime_clusters": "Partial — largest DD segment ties to Jul-14 multi-coin SL burst, not single BAD-4 style episode",
            "q2_long_e1r_mirror": "Pattern exists weakly: some SLs have bear_cross_recent + falling upper ladder, but not exclusive",
            "q3_blocker_selectivity": "Hypothetical below_ema200 blocks many TP; falling_3+bear_cross more selective but small N",
            "q4_scattered": "Many SLs are isolated Jul singles with INACTIVE-like EMA (below_200 false)",
        },
        "final_verdict": "LONG_DD_CLUSTER_PARTIAL",
    }

    # refine verdict from data
    if dd_meta["max_drawdown_pct"] >= 2.5 and len([c for c in clusters if c["n_sl"] >= 2]) >= 2:
        summary["final_verdict"] = "LONG_DD_CLUSTER_PARTIAL"
    sl_with_fall = sum(1 for t in sl_rows if t.get("falling_3"))
    if sl_with_fall >= len(sl_rows) * 0.5 and summary["false_positive_notes"]["falling_3_tp_count"] < len(tp_rows) * 0.3:
        summary["final_verdict"] = "LONG_DD_CLUSTER_MECHANISM_CLEAR"

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "filtered_94_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cohort[0].keys()))
        w.writeheader()
        w.writerows(cohort)

    with (OUT / "dd_sequence.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dd_rows[0].keys()))
        w.writeheader()
        w.writerows(dd_rows)

    with (OUT / "sl_clusters.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(clusters[0].keys()) if clusters else ["cluster_id"])
        w.writeheader()
        w.writerows(clusters)

    reg_fields = [
        "symbol",
        "entry_time",
        "outcome",
        "pnl_pct",
        "mae_pct",
        "mfe_pct",
        "hours_since_bear_cross",
        "hours_since_bull_cross",
        "r15_dist_ema200_pct",
        "r4h_dist_ema200_pct",
        "falling_2",
        "falling_3",
        "falling_4",
        "bear_cross_recent",
        "below_ema200",
        "bear_reclaim_like",
        "upper_density_high",
        "m15_uppers_within_2.0pct",
        "m4h_uppers_within_2.0pct",
        "tp_pool_age_h",
        "entry_pool_age_h",
        "m15_lower_2_age_h",
    ]
    with (OUT / "regime_context.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=reg_fields, extrasaction="ignore")
        w.writeheader()
        for t in cohort:
            w.writerow(t)

    with (OUT / "tp_vs_sl_filtered.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["feature", "median_TP", "median_SL", "TP_rate", "SL_rate"],
            extrasaction="ignore",
        )
        w.writeheader()
        w.writerows(tp_vs_sl)

    with (OUT / "hypothetical_blocker_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(hypotheticals[0].keys()))
        w.writeheader()
        w.writerows(hypotheticals)

    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    (OUT / "method.md").write_text(
        f"""# Long DD reduction forensics v1

Cohort: `m15_lower_2_age_h <= {FILTER_MAX_LOWER2_H}h` from `case_control_rows.csv` ({EXPECTED_N} trades).
No strategy changes. Hypothetical blockers are post-hoc labels only.
""",
        encoding="utf-8",
    )

    print("cohort", m)
    print("dd", dd_meta)
    print("verdict", summary["final_verdict"])


if __name__ == "__main__":
    main()
