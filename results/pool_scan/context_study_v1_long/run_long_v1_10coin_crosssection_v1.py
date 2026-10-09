#!/usr/bin/env python3
"""10-coin cross-section validation for frozen long_geometry_ladder24_be100_v1 (read-only)."""

from __future__ import annotations

import csv
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LONG_ROOT = Path(__file__).resolve().parent
REPO = LONG_ROOT.parents[2]
OUT = LONG_ROOT / "long_v1_10coin_crosssection_v1"

SYMBOLS = (
    "APTUSDT",
    "DOGEUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "AVAXUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "ACEUSDT",
    "BMTUSDT",
    "TUTUSDT",
)
KNOWN_COINS = {"XRPUSDT", "DOGEUSDT"}
NEW_COINS = [s for s in SYMBOLS if s not in KNOWN_COINS]

REQUESTED_START = datetime(2026, 4, 1, tzinfo=timezone.utc)
REQUESTED_END = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
FROZEN_TAG_COMMIT = "0a4e44836fb28d02b697fbd0d9a83dff05f7d223"


def _ensure_paths() -> None:
    root = str(REPO)
    for p in (
        root,
        str(REPO / "results" / "pool_scan" / "context_study_v1"),
        str(LONG_ROOT),
    ):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _sample_flag(n: int) -> str:
    if n >= 30:
        return "GOOD_SAMPLE"
    if n >= 15:
        return "MEDIUM_SAMPLE"
    if n >= 5:
        return "SMALL_SAMPLE"
    return "VERY_SMALL_SAMPLE"


def _dd_bucket(dd: float) -> str:
    if dd > 10:
        return "DD_GT_10"
    if dd > 7:
        return "DD_GT_7"
    if dd > 5:
        return "DD_GT_5"
    if dd < 3:
        return "DD_LT_3"
    return "DD_3_5"


def _outcome_label(o: str, pnl: float) -> str:
    if o in ("TP", "SL", "BE", "BE_SMALL", "INTRABAR_AMBIGUOUS"):
        return "BE" if o in ("BE", "BE_SMALL", "INTRABAR_AMBIGUOUS") else o
    if o == "horizon":
        return "horizon"
    if o == "stop":
        return "SL"
    if o == "tp":
        return "TP"
    return "other"


def _aggregate(trades: list[dict], pnl_key: str, outcome_key: str) -> dict:
    if not trades:
        return {
            "trades": 0,
            "TP": 0,
            "SL": 0,
            "BE": 0,
            "horizon": 0,
            "win_rate": None,
            "pnl_sum_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "max_loss_streak": 0,
            "median_mae_pct": None,
            "median_mfe_pct": None,
            "pnl_over_dd": None,
        }
    pnls = [float(t[pnl_key]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    labels = [_outcome_label(t[outcome_key], float(t[pnl_key])) for t in trades]
    ordered = sorted(trades, key=lambda t: (t["entry_time"], t["symbol"]))
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for p in [float(t[pnl_key]) for t in ordered]:
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        elif p > 0:
            streak = 0
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    pnl_sum = round(sum(pnls), 4)
    dd = round(max_dd, 4)
    return {
        "trades": len(trades),
        "TP": sum(1 for x in labels if x == "TP"),
        "SL": sum(1 for x in labels if x == "SL"),
        "BE": sum(1 for x in labels if x == "BE"),
        "horizon": sum(1 for x in labels if x == "horizon"),
        "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else None,
        "pnl_sum_pct": pnl_sum,
        "profit_factor": pf,
        "max_drawdown_pct": dd,
        "max_loss_streak": max_streak,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
        "pnl_over_dd": round(pnl_sum / dd, 4) if dd > 0 else None,
    }


def _cross_dd(trades: list[dict], pnl_key: str) -> dict:
    ordered = sorted(trades, key=lambda t: (t["entry_time"], t["symbol"]))
    eq = peak = max_dd = 0.0
    trough_i = peak_i = 0
    rows = []
    for i, t in enumerate(ordered):
        p = float(t[pnl_key])
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
                "pnl_pct": p,
                "cum_equity": round(eq, 4),
                "drawdown_pct": round(dd, 4),
            }
        )
        if dd > max_dd:
            max_dd = dd
            trough_i = i
    sub_peak_i = 0
    sub_peak = float("-inf")
    for j in range(trough_i + 1):
        if rows[j]["cum_equity"] >= sub_peak:
            sub_peak = rows[j]["cum_equity"]
            sub_peak_i = j
    for j, r in enumerate(rows):
        r["in_max_dd_segment"] = sub_peak_i <= j <= trough_i
    return {
        "max_drawdown_pct": round(max_dd, 4),
        "peak_time": ordered[sub_peak_i]["entry_time"] if ordered else None,
        "trough_time": ordered[trough_i]["entry_time"] if ordered else None,
        "rows": rows,
    }


def _coverage_row(symbol: str, bars15: list[dict], bars1m: list[dict] | None) -> dict:
    in_window = [b for b in bars15 if REQUESTED_START <= b["close_time"] <= REQUESTED_END]
    if not in_window:
        status = "INSUFFICIENT"
        usable_start = None
    else:
        usable_start = in_window[0]["close_time"].isoformat()
        n_bars = len(in_window)
        if n_bars < 30 * 24 * 4:  # ~30d of 15m bars rough
            status = "PARTIAL"
        else:
            status = "FULL"
    m1 = "yes" if bars1m else "no"
    m1_in = 0
    if bars1m:
        m1_in = sum(1 for b in bars1m if REQUESTED_START <= b["open_time"] <= REQUESTED_END)
    if status == "FULL" and m1 == "no":
        status = "PARTIAL"
    return {
        "symbol": symbol,
        "requested_start": REQUESTED_START.isoformat(),
        "usable_start": usable_start,
        "end": REQUESTED_END.isoformat(),
        "bars_15m_in_window": len(in_window),
        "bars_1m_in_window": m1_in,
        "15m_coverage": "ok" if in_window else "missing",
        "1m_coverage": m1,
        "management_1m": m1,
        "status": status,
    }


def _collect_symbol(symbol: str) -> tuple[list[dict], dict]:
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, scanner_pools_for_index, ui_lld_config
    from find_short_entry_15m_v1 import build_15m_bars
    from frozen_strategies.long_geometry_ladder24_be100_v1 import config as FC
    from frozen_strategies.long_geometry_ladder24_be100_v1.geometry import entry_setup_long, row_for_signal
    from frozen_strategies.long_geometry_ladder24_be100_v1.ladder import m15_lower_2_age_h, passes_ladder_filter
    from frozen_strategies.long_geometry_ladder24_be100_v1.outcome import (
        baseline_path_15m,
        simulate_be_management,
        slice_1m_after_entry,
    )
    from frozen_strategies.long_geometry_ladder24_be100_v1.strategy import _load_1m

    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(FC.PANE_FROM.timestamp()),
        to_unix=int(FC.PANE_TO.timestamp()),
        history_weeks=FC.HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)
    by_open = {b["open_time"]: b for b in bars15}
    bars1m = _load_1m(symbol)
    cov = _coverage_row(symbol, bars15, bars1m)
    lld_cfg = ui_lld_config("15m")
    cache: dict = {}
    seen: set[str] = set()
    rows: list[dict] = []

    for bar in bars15:
        moment = bar["close_time"]
        if moment < REQUESTED_START or moment > REQUESTED_END:
            continue
        idx = bar["candle_index"]
        pools, _ui, proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup_long(pools, bar, moment)
        if setup is None:
            continue
        pool, bridge_lower, upper, gap = setup
        if pool["pool_id"] in seen:
            continue
        seen.add(pool["pool_id"])
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        sig = row_for_signal(symbol, pool, birth, bar, bridge_lower, upper, gap)
        age_h = m15_lower_2_age_h(pools, moment, sig["entry_price"])
        base = baseline_path_15m(bars15, idx, sig["entry_price"], sig["stop"], sig["tp"])
        if base["first_hit"] not in ("TP", "SL"):
            continue
        sim_bars = bars15
        sim_idx = idx
        bar_min = 15
        if bars1m:
            slice_bars, sidx = slice_1m_after_entry(bars1m, bar["close_time"])
            if slice_bars:
                sim_bars = slice_bars
                sim_idx = sidx
                bar_min = 1
        managed = simulate_be_management(
            sim_bars, sim_idx, sig["entry_price"], sig["stop"], sig["tp"], bar_minutes=bar_min
        )
        rows.append(
            {
                **sig,
                "decision_time": moment.isoformat(),
                FC.LADDER_FEATURE: age_h,
                "ladder_pass": passes_ladder_filter(age_h),
                "mae_pct": base["mae_pct"],
                "mfe_pct": base["mfe_pct"],
                "baseline_outcome": base["first_hit"],
                "baseline_pnl_pct": base["pnl_pct"],
                "managed_outcome": managed.outcome,
                "managed_pnl_pct": managed.pnl_pct,
                "be_trigger_fired": managed.trigger_fired,
                "management_tf": managed.resolution_tf,
                "pool_known_ok": pool["known"] <= moment,
                "pool_break_ok": pool.get("break_at") is None or pool["break_at"] > moment,
            }
        )
    return rows, cov


def _variant_rows(all_rows: list[dict], variant: str) -> list[dict]:
    out = []
    for r in all_rows:
        if variant == "A":
            t = dict(r)
            t["pnl_pct"] = r["baseline_pnl_pct"]
            t["outcome"] = r["baseline_outcome"]
            out.append(t)
        elif variant == "B":
            if not r["ladder_pass"]:
                continue
            t = dict(r)
            t["pnl_pct"] = r["baseline_pnl_pct"]
            t["outcome"] = r["baseline_outcome"]
            out.append(t)
        else:
            if not r["ladder_pass"]:
                continue
            t = dict(r)
            t["pnl_pct"] = r["managed_pnl_pct"]
            t["outcome"] = r["managed_outcome"]
            out.append(t)
    return out


def _ladder_effect(a: dict, b: dict) -> str:
    if b["trades"] == 0:
        return "LADDER_HURT"
    if b["max_drawdown_pct"] < a["max_drawdown_pct"] and (b["profit_factor"] or 0) >= (a["profit_factor"] or 0):
        return "LADDER_HELPED"
    if b["pnl_sum_pct"] >= a["pnl_sum_pct"] * 0.85 and b["max_drawdown_pct"] <= a["max_drawdown_pct"]:
        return "LADDER_HELPED"
    if b["pnl_sum_pct"] < a["pnl_sum_pct"] * 0.5 and b["max_drawdown_pct"] >= a["max_drawdown_pct"]:
        return "LADDER_HURT"
    return "LADDER_NEUTRAL"


def _be_effect(b: dict, c: dict) -> str:
    if c["max_drawdown_pct"] < b["max_drawdown_pct"] - 0.05:
        return "BE_HELPED"
    if c["pnl_sum_pct"] < b["pnl_sum_pct"] * 0.7 and c["max_drawdown_pct"] >= b["max_drawdown_pct"]:
        return "BE_HURT"
    if abs(c["max_drawdown_pct"] - b["max_drawdown_pct"]) < 0.05 and abs(c["pnl_sum_pct"] - b["pnl_sum_pct"]) < 1:
        return "BE_NEUTRAL"
    if c["max_drawdown_pct"] <= b["max_drawdown_pct"]:
        return "BE_HELPED"
    return "BE_NEUTRAL"


def main() -> None:
    _ensure_paths()
    OUT.mkdir(parents=True, exist_ok=True)

    hash_out = subprocess.run(
        [sys.executable, "-m", "frozen_strategies.long_geometry_ladder24_be100_v1.reproduce", "--verify-hashes"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    hash_pass = "HASH_VERIFY_PASS" in hash_out.stdout
    tag_out = subprocess.run(["git", "rev-list", "-n", "1", "long_geometry_ladder24_be100_v1"], cwd=REPO, capture_output=True, text=True)
    tag_commit = tag_out.stdout.strip()

    all_by_sym: dict[str, list[dict]] = {}
    coverage_rows = []
    for sym in SYMBOLS:
        print(f"collect {sym}", flush=True)
        rows, cov = _collect_symbol(sym)
        all_by_sym[sym] = rows
        coverage_rows.append(cov)

    all_rows = []
    for sym in SYMBOLS:
        all_rows.extend(all_by_sym[sym])

    variants = {}
    for v in ("A", "B", "C"):
        variants[v] = _variant_rows(all_rows, v)

    def write_trades(name: str, trades: list[dict]) -> None:
        if not trades:
            return
        fields = list(trades[0].keys())
        with (OUT / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)

    write_trades("trades_geometry.csv", variants["A"])
    write_trades("trades_ladder24.csv", variants["B"])
    write_trades("trades_frozen_v1.csv", variants["C"])

    overall = []
    coin_summary = []
    for v, label in (("A", "geometry"), ("B", "ladder24"), ("C", "frozen_v1")):
        trades = variants[v]
        m = _aggregate(trades, "pnl_pct", "outcome")
        m["variant"] = label
        overall.append(m)
        for sym in SYMBOLS:
            sub = [t for t in trades if t["symbol"] == sym]
            sm = _aggregate(sub, "pnl_pct", "outcome")
            coin_summary.append(
                {
                    "variant": label,
                    "symbol": sym,
                    "group": "KNOWN" if sym in KNOWN_COINS else "NEW",
                    "sample_flag": _sample_flag(sm["trades"]),
                    "dd_bucket": _dd_bucket(sm["max_drawdown_pct"]),
                    **sm,
                }
            )

    dd_seq_rows = []
    cross_dd = {}
    for v, label in (("A", "geometry"), ("B", "ladder24"), ("C", "frozen_v1")):
        meta = _cross_dd(variants[v], "pnl_pct")
        cross_dd[label] = {k: meta[k] for k in meta if k != "rows"}
        for r in meta["rows"]:
            dd_seq_rows.append({"variant": label, **r})

    known_vs_new = []
    for v, label in (("A", "geometry"), ("B", "ladder24"), ("C", "frozen_v1")):
        for group, syms in (("KNOWN", list(KNOWN_COINS)), ("NEW", NEW_COINS)):
            sub = [t for t in variants[v] if t["symbol"] in syms]
            known_vs_new.append({"variant": label, "group": group, **_aggregate(sub, "pnl_pct", "outcome")})

    new_only = [t for t in variants["C"] if t["symbol"] in NEW_COINS]
    new_only_m = _aggregate(new_only, "pnl_pct", "outcome")
    new_only_dd = _cross_dd(new_only, "pnl_pct")

    monthly = []
    for v, label in (("A", "geometry"), ("B", "ladder24"), ("C", "frozen_v1")):
        for ym in ("2026-04", "2026-05", "2026-06", "2026-07"):
            sub = [t for t in variants[v] if t["entry_time"].startswith(ym)]
            monthly.append({"variant": label, "month": ym, **_aggregate(sub, "pnl_pct", "outcome")})

    matrix = []
    for sym in SYMBOLS:
        row = {"symbol": sym}
        total = 0.0
        for ym in ("2026-04", "2026-05", "2026-06", "2026-07"):
            sub = [t for t in variants["C"] if t["symbol"] == sym and t["entry_time"].startswith(ym)]
            pnl = round(sum(float(t["pnl_pct"]) for t in sub), 4)
            row[ym] = f"{pnl} ({len(sub)})"
            total += pnl
        row["total"] = round(total, 4)
        matrix.append(row)

    ladder_eff = []
    be_eff = []
    for sym in SYMBOLS:
        a = _aggregate([t for t in variants["A"] if t["symbol"] == sym], "pnl_pct", "outcome")
        b = _aggregate([t for t in variants["B"] if t["symbol"] == sym], "pnl_pct", "outcome")
        c = _aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")
        ladder_eff.append(
            {
                "symbol": sym,
                "trades_a": a["trades"],
                "trades_b": b["trades"],
                "pnl_retention_pct": round(b["pnl_sum_pct"] / a["pnl_sum_pct"] * 100, 2) if a["pnl_sum_pct"] else None,
                "dd_reduction": round(a["max_drawdown_pct"] - b["max_drawdown_pct"], 4),
                "pf_a": a["profit_factor"],
                "pf_b": b["profit_factor"],
                "streak_a": a["max_loss_streak"],
                "streak_b": b["max_loss_streak"],
                "label": _ladder_effect(a, b),
            }
        )
        tp_be = sum(
            1
            for t in variants["C"]
            if t["symbol"] == sym
            and t["baseline_outcome"] == "TP"
            and _outcome_label(t["managed_outcome"], float(t["managed_pnl_pct"])) == "BE"
        )
        sl_be = sum(
            1
            for t in variants["C"]
            if t["symbol"] == sym
            and t["baseline_outcome"] == "SL"
            and _outcome_label(t["managed_outcome"], float(t["managed_pnl_pct"])) == "BE"
        )
        be_eff.append(
            {
                "symbol": sym,
                "pnl_b": b["pnl_sum_pct"],
                "pnl_c": c["pnl_sum_pct"],
                "dd_b": b["max_drawdown_pct"],
                "dd_c": c["max_drawdown_pct"],
                "streak_b": b["max_loss_streak"],
                "streak_c": c["max_loss_streak"],
                "tp_to_be": tp_be,
                "sl_to_be": sl_be,
                "label": _be_effect(b, c),
            }
        )

    c_pnls = {sym: _aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")["pnl_sum_pct"] for sym in SYMBOLS}
    total_c = sum(c_pnls.values()) or 1.0
    contrib = sorted(
        ((sym, c_pnls[sym], abs(c_pnls[sym]) / abs(total_c) * 100) for sym in SYMBOLS),
        key=lambda x: -abs(x[1]),
    )
    top1 = contrib[0][2] if contrib else 0
    top3 = sum(x[2] for x in contrib[:3])
    concentration = "CONCENTRATED" if top3 > 75 else "BROAD"

    robust = []
    for sym in SYMBOLS:
        c = _aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")
        if c["trades"] >= 15 and c["pnl_sum_pct"] > 0 and (c["profit_factor"] or 0) > 1 and c["max_drawdown_pct"] < 5:
            robust.append(sym)

    failures = []
    for sym in SYMBOLS:
        c = _aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")
        if (c["profit_factor"] or 0) < 1 or c["pnl_sum_pct"] < 0 or c["max_drawdown_pct"] > 5:
            b_tr = [t for t in all_by_sym[sym] if t["ladder_pass"]]
            failures.append(
                {
                    "symbol": sym,
                    "trades_c": c["trades"],
                    "pnl_c": c["pnl_sum_pct"],
                    "pf_c": c["profit_factor"],
                    "dd_c": c["max_drawdown_pct"],
                    "ladder_pass_rate": round(len(b_tr) / max(len(all_by_sym[sym]), 1), 4),
                    "sample_flag": _sample_flag(c["trades"]),
                    "coverage_status": next(x["status"] for x in coverage_rows if x["symbol"] == sym),
                }
            )

    pos_coins = sum(1 for sym in SYMBOLS if c_pnls[sym] > 0)
    pf_coins = sum(1 for sym in SYMBOLS if (_aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")["profit_factor"] or 0) > 1)
    dd_coins = sum(1 for sym in SYMBOLS if _aggregate([t for t in variants["C"] if t["symbol"] == sym], "pnl_pct", "outcome")["max_drawdown_pct"] < 5)

    causality_pass = all(
        r["pool_known_ok"] and r["pool_break_ok"] for r in all_rows
    )

    verdict = "LONG_V1_10COIN_CROSSSECTION_PARTIAL"
    oc = overall[2] if len(overall) > 2 else {}
    if oc.get("pnl_sum_pct", 0) > 0 and (oc.get("profit_factor") or 0) > 1 and pos_coins >= 5:
        verdict = "LONG_V1_10COIN_CROSSSECTION_PASS"
    if pos_coins < 3 or (oc.get("pnl_sum_pct", 0) < 0):
        verdict = "LONG_V1_10COIN_CROSSSECTION_FAIL"

    summary = {
        "period": [REQUESTED_START.isoformat(), REQUESTED_END.isoformat()],
        "symbols": list(SYMBOLS),
        "known_coins": list(KNOWN_COINS),
        "new_coins": NEW_COINS,
        "frozen_hash_verify": hash_pass,
        "frozen_tag_commit": tag_commit,
        "expected_tag_commit": FROZEN_TAG_COMMIT,
        "tag_match": tag_commit == FROZEN_TAG_COMMIT,
        "overall": {r["variant"]: r for r in overall},
        "cross_coin_dd": cross_dd,
        "new_coins_only_c": {**new_only_m, "cross_dd": new_only_dd["max_drawdown_pct"]},
        "pnl_concentration": {"top1_share_pct": round(top1, 2), "top3_share_pct": round(top3, 2), "label": concentration, "by_coin": contrib},
        "robust_positive_coins": robust,
        "positive_coins": pos_coins,
        "pf_gt1_coins": pf_coins,
        "dd_lt5_coins": dd_coins,
        "leakage_audit": {"pass": causality_pass},
        "final_verdict": verdict,
    }

    def wcsv(name: str, rows: list[dict]) -> None:
        if not rows:
            return
        with (OUT / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    wcsv("coverage.csv", coverage_rows)
    wcsv("overall_summary.csv", overall)
    wcsv("coin_summary.csv", coin_summary)
    wcsv("known_vs_new.csv", known_vs_new)
    wcsv("new_coins_only.csv", [{"metric": k, "value": v} for k, v in {**new_only_m, "cross_dd": new_only_dd["max_drawdown_pct"]}.items()])
    wcsv("monthly_summary.csv", monthly)
    wcsv("coin_month_matrix.csv", matrix)
    wcsv("ladder_effect.csv", ladder_eff)
    wcsv("be_effect.csv", be_eff)
    wcsv("dd_sequences.csv", dd_seq_rows)
    wcsv("failure_cases.csv", failures)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "method.md").write_text(
        """# Long V1 10-coin cross-section v1

Frozen package `long_geometry_ladder24_be100_v1` unchanged. Variants A/B/C share geometry;
B adds ladder<=24h; C adds BE@1.00% per freeze. Apr–Jul 2026 UTC.
"""
    )

    print(json.dumps(summary, indent=2))
    print("verdict", verdict)


if __name__ == "__main__":
    main()
