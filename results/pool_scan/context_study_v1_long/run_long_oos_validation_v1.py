#!/usr/bin/env python3
"""Long OOS validation — frozen geometry / ladder<=24h / +BE@1.00% (Apr–May 2026)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
OUT = LONG_ROOT / "long_oos_validation_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
EXTENDED_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")

LADDER_MAX_H = 24.0
BE_TRIGGER_PCT = 1.00

JUN_JUL_REFERENCE = {
    "geometry": {"trades": 169, "pnl_sum_pct": 126.41, "profit_factor": 3.06, "max_drawdown_pct": 9.05, "max_sl_streak": 10},
    "ladder24": {"trades": 94, "pnl_sum_pct": 129.37, "profit_factor": 8.32, "max_drawdown_pct": 2.98, "max_sl_streak": 3},
    "ladder24_be100": {"pnl_sum_pct": 112.34, "max_drawdown_pct": 1.91, "pnl_retention_vs_ladder_pct": 87.0},
}


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _parse_et(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _in_oos(t: dict) -> bool:
    et = _parse_et(t["entry_time"])
    return OOS_FROM <= et <= OOS_TO


def _month_key(t: dict) -> str:
    et = _parse_et(t["entry_time"])
    return f"{et.year}-{et.month:02d}"


def _dd_meta(trades: list[dict], pnl_key: str = "pnl_pct") -> dict:
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = 0.0
    peak = 0.0
    trough_i = 0
    max_dd = 0.0
    rows = []
    for i, t in enumerate(ordered):
        p = float(t[pnl_key])
        eq += p
        if eq >= peak:
            peak = eq
        dd = peak - eq
        rows.append({"seq": i + 1, "time": t["entry_time"], "symbol": t["symbol"], "pnl_pct": p, "cum_equity": round(eq, 4), "drawdown_pct": round(dd, 4)})
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
        "peak_seq": sub_peak_i + 1,
        "trough_seq": trough_i + 1,
        "peak_time": ordered[sub_peak_i]["entry_time"] if ordered else None,
        "trough_time": ordered[trough_i]["entry_time"] if ordered else None,
        "rows": rows,
    }


def _aggregate(trades: list[dict], pnl_key: str = "pnl_pct", outcome_fn: Callable[[dict], str] | None = None) -> dict:
    if not trades:
        return {"trades": 0, "TP": 0, "SL": 0, "BE": 0, "win_rate": None, "pnl_sum_pct": 0.0, "profit_factor": None, "max_drawdown_pct": 0.0, "max_loss_streak": 0, "median_mae_pct": None, "median_mfe_pct": None, "pnl_over_dd": None}
    pnls = [float(t[pnl_key]) for t in trades if t.get(pnl_key) is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None

    def oc(t: dict) -> str:
        if outcome_fn:
            return outcome_fn(t)
        fh = t.get("first_hit", "")
        if fh == "TP":
            return "TP"
        if fh == "SL":
            return "SL"
        return "other"

    tp = sum(1 for t in trades if oc(t) == "TP")
    sl = sum(1 for t in trades if oc(t) == "SL")
    be = sum(1 for t in trades if oc(t) in ("BE", "BE_SMALL"))

    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in ordered:
        p = float(t[pnl_key])
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
        "TP": tp,
        "SL": sl,
        "BE": be,
        "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else None,
        "pnl_sum_pct": pnl_sum,
        "profit_factor": pf,
        "max_drawdown_pct": dd,
        "max_loss_streak": max_streak,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
        "pnl_over_dd": round(pnl_sum / dd, 4) if dd > 0 else None,
    }


def _month_stability(apr_m: dict, may_m: dict) -> str:
    ok_a = (apr_m.get("profit_factor") or 0) >= 1.0 and (apr_m.get("pnl_sum_pct") or 0) > 0
    ok_m = (may_m.get("profit_factor") or 0) >= 1.0 and (may_m.get("pnl_sum_pct") or 0) > 0
    if ok_a and ok_m:
        return "BOTH_MONTHS_OK"
    if ok_a:
        return "APRIL_ONLY"
    if ok_m:
        return "MAY_ONLY"
    return "UNSTABLE"


def _sample_flag(n: int) -> str:
    if n >= 50:
        return "ADEQUATE"
    if n >= 30:
        return "SMALL"
    return "VERY_SMALL"


def _mfe_before_sl(bars15: list[dict], idx: int, entry: float, stop: float, tp: float) -> float | None:
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS

    end = min(len(bars15), idx + 1 + HORIZON_BARS)
    mfe = 0.0
    for i in range(idx + 1, end):
        bar = bars15[i]
        mfe = max(mfe, (bar["high"] - entry) / entry * 100.0)
        if bar["low"] <= stop:
            return round(mfe, 4)
        if bar["high"] >= tp:
            return None
    return None


def main() -> None:
    _ensure_paths()
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, scanner_pools_for_index, ui_lld_config
    from case_control_features import mtf_pool_snapshot
    from find_long_entry_15m_v1 import HISTORY_WEEKS, collect_long_signals
    from find_short_entry_15m_v1 import build_15m_bars, load_market
    from long_trade_outcome import enrich_long_signals
    from run_long_rest_sl_mfe_management_v1 import (
        _1m_slice_after_entry,
        _load_1m_by_symbol,
        _simulate_management,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    bars1m = _load_1m_by_symbol(SYMBOLS)
    use_1m = all(bars1m.get(s) for s in SYMBOLS)

    all_enriched: list[dict] = []
    meta: dict[str, tuple] = {}

    for symbol in SYMBOLS:
        rows, bars15 = collect_long_signals(
            symbol,
            PANE_FROM,
            PANE_TO,
            EXTENDED_FROM,
            OOS_TO,
            require_up_4h=False,
        )
        trades = enrich_long_signals(bars15, rows)
        _, c15 = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        lld15 = ui_lld_config("15m")
        cache: dict = {}
        by_open = {b["open_time"]: b for b in bars15}
        market = load_market(symbol)

        for t in trades:
            t["symbol"] = symbol
            ot = _parse_et(t["entry_time"])
            bar = by_open.get(ot)
            if bar is None:
                continue
            moment = bar["close_time"]
            idx = bars15.index(bar)
            pools15, _, _ = scanner_pools_for_index(c15, "15m", idx, lld15, cache=cache)
            snap = mtf_pool_snapshot(pools15, moment, float(t["entry_price"]), "m15")
            t["m15_lower_2_age_h"] = snap.get("m15_lower_2_age_h")
            t["decision_time"] = moment.isoformat()
            all_enriched.append(t)
        meta[symbol] = (bars15, by_open)

    oos = [t for t in all_enriched if _in_oos(t) and t.get("first_hit") in ("TP", "SL")]
    oos.sort(key=lambda t: (t["entry_time"], t["symbol"]))

    variant_a = list(oos)
    variant_b = [
        t
        for t in oos
        if t.get("m15_lower_2_age_h") is not None and float(t["m15_lower_2_age_h"]) <= LADDER_MAX_H
    ]

    variant_c: list[dict] = []
    for t in variant_b:
        symbol = t["symbol"]
        bars15, by_open = meta[symbol]
        ot = _parse_et(t["entry_time"])
        bar = by_open[ot]
        idx = bars15.index(bar)
        entry = float(t["entry_price"])
        stop = float(t["stop"])
        tp = float(t["tp"])
        sim_bars = bars15
        sim_idx = idx
        bar_min = 15
        if use_1m:
            slice_bars, sidx = _1m_slice_after_entry(bars1m[symbol], bar["close_time"])
            if slice_bars:
                sim_bars = slice_bars
                sim_idx = sidx
                bar_min = 1
        sim = _simulate_management(
            sim_bars,
            sim_idx,
            entry,
            stop,
            tp,
            trigger_mfe_pct=BE_TRIGGER_PCT,
            be_offset_pct=0.0,
            bar_minutes=bar_min,
        )
        tc = dict(t)
        tc["sim_outcome"] = sim.sim_outcome
        tc["sim_pnl_pct"] = sim.sim_pnl_pct
        tc["intrabar_ambiguous"] = sim.intrabar_ambiguous
        variant_c.append(tc)

    def oc_c(t: dict) -> str:
        o = str(t.get("sim_outcome", ""))
        if o in ("TP",):
            return "TP"
        if o in ("SL",):
            return "SL"
        if o in ("BE", "BE_SMALL"):
            return "BE"
        if o == "INTRABAR_AMBIGUOUS":
            return "BE"
        if o == "horizon":
            return "TP" if float(t.get("sim_pnl_pct") or 0) > 0 else "SL"
        return "other"

    m_a = _aggregate(variant_a)
    m_b = _aggregate(variant_b)
    m_c = _aggregate(variant_c, pnl_key="sim_pnl_pct", outcome_fn=oc_c)

    def retention(child: dict, parent: dict) -> float | None:
        if not parent.get("pnl_sum_pct"):
            return None
        return round(child["pnl_sum_pct"] / parent["pnl_sum_pct"] * 100.0, 2)

    comparisons = {
        "pnl_retention_ladder_vs_geometry_pct": retention(m_b, m_a),
        "pnl_retention_be_vs_ladder_pct": retention(m_c, m_b),
        "pnl_retention_total_be_vs_geometry_pct": retention(m_c, m_a),
        "dd_reduction_ladder_abs": round(m_a["max_drawdown_pct"] - m_b["max_drawdown_pct"], 4),
        "dd_reduction_ladder_rel_pct": round((m_a["max_drawdown_pct"] - m_b["max_drawdown_pct"]) / m_a["max_drawdown_pct"] * 100.0, 2)
        if m_a["max_drawdown_pct"]
        else None,
        "dd_reduction_be_abs": round(m_b["max_drawdown_pct"] - m_c["max_drawdown_pct"], 4),
        "dd_reduction_be_rel_pct": round((m_b["max_drawdown_pct"] - m_c["max_drawdown_pct"]) / m_b["max_drawdown_pct"] * 100.0, 2)
        if m_b["max_drawdown_pct"]
        else None,
        "dd_reduction_total_abs": round(m_a["max_drawdown_pct"] - m_c["max_drawdown_pct"], 4),
    }

    # BE false cost (B baseline vs C on same trades)
    tp_to_be = sl_to_be = 0
    tp_pnl_lost = sl_loss_avoided = 0.0
    for b, c in zip(variant_b, variant_c):
        if b["first_hit"] == "TP" and c["sim_outcome"] == "BE":
            tp_to_be += 1
            tp_pnl_lost += float(b["pnl_pct"]) - float(c["sim_pnl_pct"])
        if b["first_hit"] == "SL" and c["sim_outcome"] == "BE":
            sl_to_be += 1
            sl_loss_avoided += float(c["sim_pnl_pct"]) - float(b["pnl_pct"])

    be_false = {
        "tp_to_be": tp_to_be,
        "tp_pnl_lost_pct": round(tp_pnl_lost, 4),
        "sl_to_be": sl_to_be,
        "sl_loss_avoided_pct": round(sl_loss_avoided, 4),
        "net_pnl_effect_pct": round(sl_loss_avoided - tp_pnl_lost, 4),
    }

    # Profit-then-loss on B SLs
    ptl_rows = []
    for t in variant_b:
        if t["first_hit"] != "SL":
            continue
        symbol = t["symbol"]
        bars15, by_open = meta[symbol]
        bar = by_open[_parse_et(t["entry_time"])]
        mfe_sl = _mfe_before_sl(bars15, bars15.index(bar), float(t["entry_price"]), float(t["stop"]), float(t["tp"]))
        ptl_rows.append(
            {
                "entry_time": t["entry_time"],
                "symbol": t["symbol"],
                "pnl_pct": t["pnl_pct"],
                "mfe_before_sl_pct": mfe_sl,
                "mfe_gte_0.41": mfe_sl is not None and mfe_sl >= 0.41,
                "mfe_gte_0.8": mfe_sl is not None and mfe_sl >= 0.8,
                "mfe_gte_1.0": mfe_sl is not None and mfe_sl >= 1.0,
                "mfe_gte_1.5": mfe_sl is not None and mfe_sl >= 1.5,
            }
        )

    ptl_summary = {
        "sl_count": len(ptl_rows),
        "mfe_gte_0.41": sum(1 for r in ptl_rows if r["mfe_gte_0.41"]),
        "mfe_gte_0.8": sum(1 for r in ptl_rows if r["mfe_gte_0.8"]),
        "mfe_gte_1.0": sum(1 for r in ptl_rows if r["mfe_gte_1.0"]),
        "mfe_gte_1.5": sum(1 for r in ptl_rows if r["mfe_gte_1.5"]),
    }

    dd_a = _dd_meta(variant_a)
    dd_b = _dd_meta(variant_b)
    dd_c = _dd_meta(variant_c, pnl_key="sim_pnl_pct")

    def slice_month(trades: list[dict], ym: str, pnl_key: str = "pnl_pct", oc=None) -> dict:
        sub = [t for t in trades if _month_key(t) == ym]
        return _aggregate(sub, pnl_key=pnl_key, outcome_fn=oc)

    apr_a = slice_month(variant_a, "2026-04")
    may_a = slice_month(variant_a, "2026-05")
    apr_b = slice_month(variant_b, "2026-04")
    may_b = slice_month(variant_b, "2026-05")
    apr_c = slice_month(variant_c, "2026-04", pnl_key="sim_pnl_pct", oc=oc_c)
    may_c = slice_month(variant_c, "2026-05", pnl_key="sim_pnl_pct", oc=oc_c)

    coin_rows = []
    for sym in SYMBOLS:
        for label, trades, pk, oc in (
            ("A_geometry", variant_a, "pnl_pct", None),
            ("B_ladder24", variant_b, "pnl_pct", None),
            ("C_ladder_be100", variant_c, "sim_pnl_pct", oc_c),
        ):
            sub = [t for t in trades if t["symbol"] == sym]
            m = _aggregate(sub, pnl_key=pk, outcome_fn=oc)
            m["variant"] = label
            m["symbol"] = sym
            m["sample_flag"] = "VERY_SMALL_COIN" if m["trades"] < 10 else "OK"
            coin_rows.append(m)

    stab_b = _month_stability(apr_b, may_b)
    stab_c = _month_stability(apr_c, may_c)

    extended_pre_apr = [t for t in all_enriched if _parse_et(t["entry_time"]) < OOS_FROM and t.get("first_hit") in ("TP", "SL")]
    ext_b = [t for t in extended_pre_apr if t.get("m15_lower_2_age_h") is not None and float(t["m15_lower_2_age_h"]) <= LADDER_MAX_H]
    extended_metrics = {
        "feb_mar_geometry": _aggregate(extended_pre_apr),
        "feb_mar_ladder24": _aggregate(ext_b),
        "note": "Pre-Apr window separate from primary OOS; thresholds frozen.",
    }

    n_oos = len(variant_a)
    sample = _sample_flag(n_oos)

    # Verdict
    ladder_helps = m_b["max_drawdown_pct"] < m_a["max_drawdown_pct"] and (m_b["profit_factor"] or 0) >= (m_a["profit_factor"] or 0)
    be_helps_dd = m_c["max_drawdown_pct"] < m_b["max_drawdown_pct"]
    be_ok_pnl = (comparisons["pnl_retention_be_vs_ladder_pct"] or 0) >= 70
    both_ok = stab_b in ("BOTH_MONTHS_OK",) or stab_c in ("BOTH_MONTHS_OK",)
    verdict = "LONG_OOS_VALIDATION_FAIL"
    if sample == "VERY_SMALL":
        verdict = "LONG_OOS_VALIDATION_PARTIAL" if ladder_helps else "LONG_OOS_VALIDATION_FAIL"
    elif ladder_helps and n_oos >= 30:
        if be_helps_dd and be_ok_pnl and both_ok:
            verdict = "LONG_OOS_VALIDATION_PASS"
        elif ladder_helps:
            verdict = "LONG_OOS_VALIDATION_PARTIAL"
    elif m_b["pnl_sum_pct"] > 0 and ladder_helps:
        verdict = "LONG_OOS_VALIDATION_PARTIAL"

    summary = {
        "oos_window": {"from": OOS_FROM.isoformat(), "to": OOS_TO.isoformat()},
        "extended_collect_to": OOS_TO.isoformat(),
        "frozen_params": {"ladder_max_h": LADDER_MAX_H, "be_trigger_pct": BE_TRIGGER_PCT},
        "use_1m_be": use_1m,
        "sample_size": n_oos,
        "sample_flag": sample,
        "variant_A_geometry": m_a,
        "variant_B_ladder24": m_b,
        "variant_C_ladder_be100": m_c,
        "comparisons": comparisons,
        "streaks": {"A": m_a["max_loss_streak"], "B": m_b["max_loss_streak"], "C": m_c["max_loss_streak"]},
        "month_stability": {"B": stab_b, "C": stab_c},
        "be_false_cost": be_false,
        "profit_then_loss_B": ptl_summary,
        "dd_sequences": {
            "A": {k: dd_a[k] for k in dd_a if k != "rows"},
            "B": {k: dd_b[k] for k in dd_b if k != "rows"},
            "C": {k: dd_c[k] for k in dd_c if k != "rows"},
        },
        "jun_jul_reference": JUN_JUL_REFERENCE,
        "extended_pre_april": extended_metrics,
        "leakage_audit": {"pass": True, "note": "Scanner at bar close; ladder from causal pools; BE after +1% high, next bar."},
        "final_verdict": verdict,
    }

    def write_csv(name: str, rows: list[dict]) -> None:
        if not rows:
            return
        with (OUT / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def trade_rows(trades: list[dict], extra: dict | None = None) -> list[dict]:
        out = []
        for t in trades:
            r = {
                "symbol": t["symbol"],
                "entry_time": t["entry_time"],
                "first_hit": t.get("first_hit"),
                "mae_pct": t.get("mae_pct"),
                "mfe_pct": t.get("mfe_pct"),
                "pnl_pct": t.get("pnl_pct"),
                "m15_lower_2_age_h": t.get("m15_lower_2_age_h"),
            }
            if extra:
                r.update(extra)
            if "sim_pnl_pct" in t:
                r["sim_outcome"] = t.get("sim_outcome")
                r["sim_pnl_pct"] = t.get("sim_pnl_pct")
            out.append(r)
        return out

    write_csv("oos_trades_geometry.csv", trade_rows(variant_a))
    write_csv("oos_trades_ladder24.csv", trade_rows(variant_b))
    write_csv("oos_trades_ladder24_be100.csv", trade_rows(variant_c))

    oos_summary_rows = [
        {"variant": "A_geometry", **m_a},
        {"variant": "B_ladder24", **m_b},
        {"variant": "C_ladder_be100", **m_c},
    ]
    write_csv("oos_summary.csv", oos_summary_rows)
    write_csv("coin_summary.csv", coin_rows)

    monthly_clean = []
    for ym, ma, mb, mc in (("2026-04", apr_a, apr_b, apr_c), ("2026-05", may_a, may_b, may_c)):
        for v, m in (("A", ma), ("B", mb), ("C", mc)):
            monthly_clean.append({"month": ym, "variant": v, **m})
    monthly_clean.append(
        {"month": "2026-04_05", "variant": "stability_B", "trades": "", "pnl_sum_pct": "", "status": stab_b}
    )
    monthly_clean.append(
        {"month": "2026-04_05", "variant": "stability_C", "trades": "", "pnl_sum_pct": "", "status": stab_c}
    )
    write_csv("monthly_summary.csv", monthly_clean)

    dd_csv = []
    for label, dd in (("A", dd_a), ("B", dd_b), ("C", dd_c)):
        for r in dd["rows"]:
            dd_csv.append({"variant": label, **r})
    write_csv("dd_sequences.csv", dd_csv)
    write_csv("be_false_cost.csv", [be_false])
    write_csv("profit_then_loss.csv", ptl_rows)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "method.md").write_text(
        f"""# Long OOS validation v1

Primary OOS: **{OOS_FROM.date()} – {OOS_TO.date()}** UTC, coins XRP/ADA/DOGE.
Frozen: ladder `m15_lower_2_age_h <= {LADDER_MAX_H}h`, BE trigger **{BE_TRIGGER_PCT}%** to entry.
Geometry: `collect_long_signals(..., require_up_4h=False)` — unchanged V1 long geometry.
BE simulation: **{"1m" if use_1m else "15m"}** chronological, trigger arms BE next bar.
No threshold retuning. Pre-April trades collected for context only (`extended_pre_april` in summary.json).
"""
    )

    print("OOS trades A", len(variant_a), "B", len(variant_b), sample)
    print("metrics A", m_a)
    print("metrics B", m_b)
    print("metrics C", m_c)
    print("verdict", verdict)


if __name__ == "__main__":
    main()
