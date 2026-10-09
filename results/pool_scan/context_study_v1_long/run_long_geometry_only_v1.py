#!/usr/bin/env python3
"""Geometry-only long research (no up_4h filter) — Jun–Jul 2026."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
OUT_DIR = LONG_ROOT / "long_geometry_only_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
MOVEMENT_REPORT_PCT = 0.41

def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _outcome_label(first_hit: str) -> str:
    if first_hit == "TP":
        return "TP_FIRST"
    if first_hit == "SL":
        return "SL_FIRST"
    return "UNRESOLVED"


def _bucket_distribution_fixed(values: list[float]) -> dict[str, int]:
    """Bucket labels per spec (MFE/MAE)."""
    out = {
        "lt_0.20": 0,
        "0.20_0.40": 0,
        "0.41_0.79": 0,
        "0.80_1.49": 0,
        "gte_1.50": 0,
    }
    for v in values:
        if v < 0.20:
            out["lt_0.20"] += 1
        elif v < 0.41:
            out["0.20_0.40"] += 1
        elif v < 0.80:
            out["0.41_0.79"] += 1
        elif v < 1.50:
            out["0.80_1.49"] += 1
        else:
            out["gte_1.50"] += 1
    return out


def _aggregate(trades: list[dict]) -> dict:
    empty = {
        "signals": 0,
        "TP": 0,
        "SL": 0,
        "unresolved": 0,
        "win_rate": None,
        "pnl_sum_pct": None,
        "profit_factor": None,
        "max_drawdown_pct": 0.0,
        "max_sl_streak": 0,
        "median_sl_distance_pct": None,
        "median_tp_distance_pct": None,
        "median_rr": None,
        "median_mae_pct": None,
        "median_mfe_pct": None,
        "pct_tp_distance_gte_0_41": None,
        "pct_mfe_gte_0_41": None,
        "sl_pnl_all_negative": None,
    }
    if not trades:
        return empty

    pnls = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    resolved = [t for t in trades if t.get("first_hit") in ("TP", "SL")]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if losses and wins else None

    ordered = sorted(trades, key=lambda t: t["entry_time"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        p = t["pnl_pct"] if t.get("pnl_pct") is not None else 0.0
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.get("first_hit") == "SL":
            streak += 1
            max_streak = max(max_streak, streak)
        elif t.get("first_hit") == "TP":
            streak = 0

    sl_d = [t["stop_distance_pct"] for t in trades if t.get("stop_distance_pct") is not None]
    tp_d = [t["tp_distance_pct"] for t in trades if t.get("tp_distance_pct") is not None]
    rr = []
    for t in trades:
        sd, td = t.get("stop_distance_pct"), t.get("tp_distance_pct")
        if sd and sd > 0 and td is not None:
            rr.append(td / sd)
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]

    sl_pnls = [t["pnl_pct"] for t in trades if t.get("first_hit") == "SL" and t.get("pnl_pct") is not None]

    return {
        "signals": len(trades),
        "TP": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "SL": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "unresolved": sum(1 for t in trades if t.get("first_hit") == "none"),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "win_rate_resolved_only": round(
            sum(1 for t in resolved if t.get("first_hit") == "TP") / len(resolved), 4
        )
        if resolved
        else None,
        "pnl_sum_pct": round(sum(pnls), 4) if pnls else None,
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_sl_streak": max_streak,
        "median_sl_distance_pct": round(statistics.median(sl_d), 4) if sl_d else None,
        "median_tp_distance_pct": round(statistics.median(tp_d), 4) if tp_d else None,
        "median_rr": round(statistics.median(rr), 4) if rr else None,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
        "pct_tp_distance_gte_0_41": round(
            sum(1 for d in tp_d if d >= MOVEMENT_REPORT_PCT) / len(tp_d) * 100.0, 2
        )
        if tp_d
        else None,
        "pct_mfe_gte_0_41": round(
            sum(1 for m in mfes if m >= MOVEMENT_REPORT_PCT) / len(mfes) * 100.0, 2
        )
        if mfes
        else None,
        "sl_pnl_all_negative": all(p < 0 for p in sl_pnls) if sl_pnls else True,
    }


def _trade_row_public(t: dict) -> dict:
    return {
        "time": t["entry_time"],
        "symbol": t["symbol"],
        "entry": t["entry_price"],
        "sl": t["stop"],
        "tp": t["tp"],
        "outcome": t["outcome"],
        "pnl_pct": t.get("pnl_pct"),
        "MAE_pct": t.get("mae_pct"),
        "MFE_pct": t.get("mfe_pct"),
        "pool_id": t.get("pool_id"),
    }


def _setup_bar_count(symbol: str) -> int:
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_long_entry_15m_v1 import HISTORY_WEEKS, entry_setup_long
    from find_short_entry_15m_v1 import build_15m_bars

    _, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)
    lld_cfg = ui_lld_config("15m")
    cache: dict = {}
    n = 0
    for bar in bars15:
        moment = bar["close_time"]
        if moment < REPORT_FROM or moment > REPORT_TO:
            continue
        pools, _, _ = scanner_pools_for_index(candles, "15m", bar["candle_index"], lld_cfg, cache=cache)
        if entry_setup_long(pools, bar, moment) is not None:
            n += 1
    return n


def _causality_audit(trade: dict, bars15: list[dict], candles, lld_cfg, market: dict) -> dict:
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index
    from find_long_entry_15m_v1 import (
        STOP_PAD,
        attached_lower,
        entry_setup_long,
        next_lower_bridge,
        next_upper_tp,
        MIN_TP_PCT,
    )

    ot = datetime.fromisoformat(trade["entry_time"])
    bar = next(b for b in bars15 if b["open_time"] == ot)
    moment = bar["close_time"]
    idx = bar["candle_index"]
    pools, _, _ = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache={})
    setup = entry_setup_long(pools, bar, moment)
    pool = bridge = upper = None
    if setup:
        pool, bridge, upper, _gap = setup
    entry = float(trade["entry_price"])
    stop = float(trade["stop"])
    tp = float(trade["tp"])
    exp_stop = pool["bottom"] * (1.0 - STOP_PAD) if pool else None
    tp_pool = next_upper_tp(pools, bar["close"], moment, MIN_TP_PCT) if pool else None
    br = next_lower_bridge(pools, pool, moment) if pool else None

    checks = {
        "symbol": trade["symbol"],
        "entry_time": trade["entry_time"],
        "pool_known": pool is not None and pool["known"] <= moment,
        "pool_not_broken": pool is not None
        and (pool["break_at"] is None or pool["break_at"] > moment),
        "attach_ok": pool is not None and attached_lower(pool, bar),
        "bridge_ok": bridge is not None and br is not None and bridge["pool_id"] == br["pool_id"],
        "tp_upper_known": upper is not None and upper["known"] <= moment,
        "tp_matches_geometry": tp_pool is not None and abs(tp_pool["bottom"] - tp) < 1e-6,
        "sl_ok": exp_stop is not None and abs(exp_stop - stop) < 1e-6,
        "entry_is_close": abs(entry - bar["close"]) < 1e-9,
        "outcome_starts_after_entry": idx < len(bars15) - 1,
        "pool_id_match": pool is not None and pool["pool_id"] == trade["pool_id"],
    }
    checks["pass"] = all(checks[k] for k in checks if k not in ("symbol", "entry_time", "pass"))
    return checks


def main() -> None:
    _ensure_paths()
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from find_long_entry_15m_v1 import HISTORY_WEEKS, collect_long_signals
    from find_short_entry_15m_v1 import load_market
    from long_trade_outcome import enrich_long_signals

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: list[dict] = []
    meta_by_sym: dict[str, tuple] = {}

    for symbol in SYMBOLS:
        rows, bars15 = collect_long_signals(
            symbol,
            PANE_FROM,
            PANE_TO,
            REPORT_FROM,
            REPORT_TO,
            require_up_4h=False,
        )
        print(f"{symbol} geometry signals: {len(rows)}", flush=True)
        trades = enrich_long_signals(bars15, rows)
        for t in trades:
            t["symbol"] = symbol
            t["outcome"] = _outcome_label(t.get("first_hit", "none"))
        all_trades.extend(trades)
        _, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        meta_by_sym[symbol] = (bars15, candles, load_market(symbol))

    all_trades.sort(key=lambda t: (t["entry_time"], t["symbol"]))

    counts = {sym: sum(1 for t in all_trades if t["symbol"] == sym) for sym in SYMBOLS}
    setup_bar_counts = {sym: _setup_bar_count(sym) for sym in SYMBOLS}
    overall = _aggregate(all_trades)
    per_coin = {sym: _aggregate([t for t in all_trades if t["symbol"] == sym]) for sym in SYMBOLS}

    mfes = [float(t["mfe_pct"]) for t in all_trades if t.get("mfe_pct") is not None]
    maes = [float(t["mae_pct"]) for t in all_trades if t.get("mae_pct") is not None]

    by_mfe = sorted(all_trades, key=lambda t: float(t.get("mfe_pct") or -1), reverse=True)[:10]
    by_worst = sorted(
        all_trades,
        key=lambda t: (float(t.get("mae_pct") or 0), float(t.get("pnl_pct") or 0)),
        reverse=True,
    )[:10]

    lld_cfg = ui_lld_config("15m")
    audit_cases: list[dict] = []
    for sym in SYMBOLS:
        sym_trades = [t for t in all_trades if t["symbol"] == sym]
        picks = [sym_trades[0], sym_trades[len(sym_trades) // 2]] if len(sym_trades) >= 2 else sym_trades[:1]
        bars15, candles, market = meta_by_sym[sym]
        for t in picks[:2]:
            audit_cases.append(_causality_audit(t, bars15, candles, lld_cfg, market))

    causality_pass = len(audit_cases) >= 6 and all(c.get("pass") for c in audit_cases)

    summary = {
        "report_from": REPORT_FROM.isoformat(),
        "report_to": REPORT_TO.isoformat(),
        "geometry_counts": {
            "signals_deduped_total": len(all_trades),
            "signals_deduped_by_coin": counts,
            "setup_bars_total": sum(setup_bar_counts.values()),
            "setup_bars_by_coin": setup_bar_counts,
        },
        "expected_setup_bar_counts_reference": {"XRPUSDT": 115, "ADAUSDT": 100, "DOGEUSDT": 101},
        "setup_bar_counts_match_reference": setup_bar_counts == {
            "XRPUSDT": 115,
            "ADAUSDT": 100,
            "DOGEUSDT": 101,
        },
        "note_dedup": "One signal per pool_id (first bar); setup_bars counts every qualifying 15m bar.",
        "overall": overall,
        "per_coin": per_coin,
        "distribution_mfe_pct": _bucket_distribution_fixed(mfes),
        "distribution_mae_pct": _bucket_distribution_fixed(maes),
        "best_10_by_mfe": [_trade_row_public(t) for t in by_mfe],
        "worst_10_by_mae_then_pnl": [_trade_row_public(t) for t in by_worst],
        "causality_audit": audit_cases,
        "causality_audit_pass": causality_pass,
        "interpretation_questions": {
            "A_edge": "See win_rate, PF, median MFE vs MAE — geometry-only, no regime.",
            "B_sample_size": len(all_trades),
            "C_outcomes": {
                "TP": overall["TP"],
                "SL": overall["SL"],
                "unresolved": overall["unresolved"],
            },
            "D_regime_filter_worth_it": "Deferred until geometry edge assessed.",
        },
    }

    with (OUT_DIR / "long_geometry_signals.json").open("w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2, default=str)

    fields = list(all_trades[0].keys()) if all_trades else []
    with (OUT_DIR / "long_geometry_signals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_trades)

    with (OUT_DIR / "long_geometry_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    method = """# Long geometry-only V1

Same entry geometry as `find_long_entry_15m_v1` / long baseline V1.

**Regime:** `up_4h` is **not** applied for this report only (`require_up_4h=False`).
Baseline code and `up_4h()` definition unchanged.

Outcome: existing `long_trade_detail` (SL before TP on same bar).

Report metric 0.41% is not a filter.
"""
    (OUT_DIR / "long_geometry_method.md").write_text(method, encoding="utf-8")

    # Verdict heuristic (geometry edge, not "long works")
    wr = overall.get("win_rate_resolved_only") or 0
    pf = overall.get("profit_factor") or 0
    med_mfe = overall.get("median_mfe_pct") or 0
    med_mae = overall.get("median_mae_pct") or 0
    if not causality_pass or not summary["setup_bar_counts_match_reference"]:
        verdict = "LONG_GEOMETRY_FAIL"
    elif pf is not None and pf >= 1.05 and wr >= 0.45 and med_mfe > med_mae:
        verdict = "LONG_GEOMETRY_HAS_EDGE"
    else:
        verdict = "LONG_GEOMETRY_WEAK"

    summary["final_verdict"] = verdict
    with (OUT_DIR / "long_geometry_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("COUNTS", counts, "total", len(all_trades))
    print("OVERALL", json.dumps(overall, indent=2))
    print("VERDICT", verdict)
    print("CAUSALITY", causality_pass, len(audit_cases))


if __name__ == "__main__":
    main()
