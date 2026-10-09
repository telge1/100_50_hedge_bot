"""4-month E1R V1 OOS robustness (frozen logic, no tuning)."""

from __future__ import annotations

import csv
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent
V1 = OUT_DIR.parents[1] / "context_study_v1"
REPO = OUT_DIR.parents[3]
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
FOUR_MO_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
FOUR_MO_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
DEV_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
DEV_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
EXT_TO = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
FREEZE_TAG = "e1r_cluster3_ema200_shadow_freeze_v1"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    for p in (str(REPO), str(V1)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def collect_signals(symbol: str, report_from: datetime, report_to: datetime) -> dict:
    from analyze_floor_guard_signal_list_v1 import collect_signals as _base

    # Re-use canonical pipeline; temporarily override module window via closure copy
    import analyze_floor_guard_signal_list_v1 as fg

    old_rf, old_rt = fg.REPORT_FROM, fg.REPORT_TO
    fg.REPORT_FROM, fg.REPORT_TO = report_from, report_to
    try:
        data = _base(symbol)
    finally:
        fg.REPORT_FROM, fg.REPORT_TO = old_rf, old_rt
    from analyze_floor_guard_signal_list_v1 import enrich_and_drawdown

    wg = enrich_and_drawdown(symbol, "with_guard", data["with_guard"], data["bars15"])
    ng = enrich_and_drawdown(symbol, "without_guard", data["no_guard"], data["bars15"])
    return {"with_guard": wg, "without_guard": ng, "bars15": data["bars15"]}


def apply_e1r(trades: list[dict], symbol: str, report_from: datetime, report_to: datetime) -> list[dict]:
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import outcome_class, signal_ignore, simulate_e1_e1r
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars

    _p, candles15 = load_pane_candles(
        symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}
    sim = simulate_e1_e1r(bars15, candles15, cfg, cache, report_from, report_to)
    out = []
    wg_opens = {t["entry_open"] for t in trades}
    for t in trades:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        oc = outcome_class(t, bars15, by_open)
        e1r = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1R")
        out.append(
            {
                **t,
                "symbol": symbol,
                "final_outcome": oc,
                "ignored_E1R": e1r["ignore"] and t["entry_open"] in wg_opens,
                "E1R_status": e1r["status"],
            }
        )
    return out


def tp_sl_rr(t: dict) -> tuple[float, float, float]:
    e, tp, sl = float(t["entry_price"]), float(t["tp"]), float(t["stop"])
    tp_d = (e - tp) / e * 100.0
    sl_d = (sl - e) / e * 100.0
    rr = tp_d / sl_d if sl_d > 0 else 0.0
    return tp_d, sl_d, rr


def metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    tp_ds, sl_ds, rrs = zip(*[tp_sl_rr(t) for t in trades])
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    streak = best = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    eq = equity_dd(trades)
    return {
        "trades": len(trades),
        "tp_count": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "sl_count": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "winrate": round(len(wins) / len(pnls), 4) if pnls else None,
        "gross_profit": round(gw, 4),
        "gross_loss": round(-gl, 4),
        "total_pnl_pct": round(sum(pnls), 4),
        "profit_factor": round(gw / gl, 4) if gl > 0 else None,
        "max_drawdown_pct": eq["max_drawdown_pct"],
        "max_sl_streak": best,
        "mean_mae": round(statistics.mean(maes), 4) if maes else None,
        "mean_mfe": round(statistics.mean(mfes), 4) if mfes else None,
        "median_mae": round(statistics.median(maes), 4) if maes else None,
        "median_mfe": round(statistics.median(mfes), 4) if mfes else None,
        "mean_tp_dist_pct": round(statistics.mean(tp_ds), 4),
        "mean_sl_dist_pct": round(statistics.mean(sl_ds), 4),
        "mean_rr": round(statistics.mean(rrs), 4),
        "median_rr": round(statistics.median(rrs), 4),
    }


def equity_dd(trades: list[dict]) -> dict:
    rows = sorted([t for t in trades if t.get("pnl_pct") is not None], key=lambda x: x["entry_open"])
    eq = peak = 100.0
    max_dd = 0.0
    for t in rows:
        eq += float(t["pnl_pct"])
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return {"max_drawdown_pct": round(max_dd, 4)}


def filter_window(trades: list[dict], start: datetime, end: datetime) -> list[dict]:
    out = []
    for t in trades:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if start <= ot <= end:
            out.append(t)
    return out


def execution_realistic(trades: list[dict]) -> list[dict]:
    """Max one open position per coin."""
    by_sym: dict[str, list[dict]] = {s: [] for s in COINS}
    for t in trades:
        by_sym[t["symbol"]].append(t)
    taken = []
    for sym in COINS:
        open_until = None
        for t in sorted(by_sym[sym], key=lambda x: x["entry_open"]):
            ot = _utc(datetime.fromisoformat(t["entry_open"]))
            if open_until and ot < open_until:
                continue
            taken.append(t)
            if t.get("exit_time"):
                open_until = _utc(datetime.fromisoformat(t["exit_time"]))
            else:
                open_until = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(taken, key=lambda x: x["entry_open"])


def month_key(entry_open: str) -> str:
    return entry_open[:7]


def main() -> None:
    _ensure_paths()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    coverage = {
        "candle_15m": "2025-12-11 .. 2026-09-26 (strict_complete_buckets=True)",
        "pane_load": f"{PANE_FROM.date()} .. {PANE_TO.date()}",
        "warmup_note": "PANE_FROM 2026-02-10; EMA200 needs ~200x15m bars inside pane",
        "pool_path": "scanner_pools_for_index + H4FloorGuard (floor_guard_signal_list_v1 pipeline)",
        "data_gap": "No candles after 2026-09-26 09:00 UTC in current store",
    }

    all_wg: dict[str, list[dict]] = {}
    all_e1r_allowed: dict[str, list[dict]] = {}
    for sym in COINS:
        print("collect 4mo", sym, flush=True)
        data = collect_signals(sym, FOUR_MO_FROM, FOUR_MO_TO)
        wg = data["with_guard"]
        all_wg[sym] = wg
        tagged = apply_e1r(wg, sym, FOUR_MO_FROM, FOUR_MO_TO)
        allowed = [t for t in tagged if not t["ignored_E1R"]]
        all_e1r_allowed[sym] = allowed

    # Parity Jun-Jul vs freeze
    freeze_path = V1 / "analyze_crosscoin_e1r_final_pnl_comparison_v1.json"
    freeze_dev = {}
    if freeze_path.exists():
        fj = json.loads(freeze_path.read_text())
        for sym in COINS:
            freeze_dev[sym] = fj["by_coin"][sym]["e1r"]["pnl"]["trades"]

    dev_recomputed = {sym: len(filter_window(all_e1r_allowed[sym], DEV_FROM, DEV_TO)) for sym in COINS}

    research_all = []
    for sym in COINS:
        research_all.extend(all_e1r_allowed[sym])
    exec_all = execution_realistic(research_all)

    segments = {
        "four_months": metrics(research_all),
        "oos_apr_may": metrics(filter_window(research_all, OOS_FROM, OOS_TO)),
        "dev_jun_jul": metrics(filter_window(research_all, DEV_FROM, DEV_TO)),
        "execution_realistic_four_months": metrics(exec_all),
    }
    per_coin = {sym: metrics(all_e1r_allowed[sym]) for sym in COINS}
    per_month: dict[str, dict] = {}
    for m in ("2026-04", "2026-05", "2026-06", "2026-07"):
        per_month[m] = metrics([t for t in research_all if month_key(t["entry_open"]) == m])

    freeze_metrics = json.loads((V1 / "analyze_crosscoin_e1r_final_pnl_comparison_v1.json").read_text())
    fc = freeze_metrics["cross_coin"]["e1r"]
    compare = {
        "jun_jul_freeze": {
            "trades": fc["trades"],
            "pnl": fc["total_pnl_pct"],
            "pf": fc["profit_factor"],
            "dd": freeze_metrics["cross_coin"]["drawdown_e1r"]["max_drawdown_pct"],
            "winrate": fc["winners"] / fc["trades"] if fc["trades"] else None,
            "max_sl_streak": fc["max_sl_streak"],
        },
        "four_months_total": {k: segments["four_months"].get(k) for k in ("trades", "total_pnl_pct", "profit_factor", "max_drawdown_pct", "winrate", "max_sl_streak")},
        "oos_only": {k: segments["oos_apr_may"].get(k) for k in ("trades", "total_pnl_pct", "profit_factor", "max_drawdown_pct", "winrate", "max_sl_streak")},
    }

    # stability (descriptive)
    monthly_pnls = {m: per_month[m]["total_pnl_pct"] for m in per_month}
    coin_pnls = {sym: per_coin[sym]["total_pnl_pct"] for sym in COINS}
    best_coin = max(coin_pnls, key=coin_pnls.get)
    best_month = max(monthly_pnls, key=monthly_pnls.get)
    stability = {
        "all_coins_profitable": all(coin_pnls[s] > 0 for s in COINS),
        "all_months_profitable": all(monthly_pnls[m] > 0 for m in monthly_pnls),
        "coin_pnl": coin_pnls,
        "monthly_pnl": monthly_pnls,
        "pnl_without_best_coin": round(sum(coin_pnls[s] for s in COINS if s != best_coin), 4),
        "pnl_without_best_month": round(sum(monthly_pnls[m] for m in monthly_pnls if m != best_month), 4),
        "best_coin": best_coin,
        "best_month": best_month,
    }

    # spot checks
    blocked = [t for sym in COINS for t in apply_e1r(all_wg[sym], sym, FOUR_MO_FROM, FOUR_MO_TO) if t["ignored_E1R"]]
    allowed = research_all
    spot = {
        "blocked_sample": blocked[:10],
        "allowed_sample": allowed[:10],
        "note": "Full causal audit not re-run; samples for manual review",
    }

    dev_parity_ok = all(dev_recomputed[s] == freeze_dev.get(s) for s in COINS)
    verdict = "INVALID_DUE_TO_DATA_OR_CAUSALITY"
    if not dev_parity_ok:
        verdict = "INVALID_DUE_TO_DATA_OR_CAUSALITY"
    elif segments["four_months"]["total_pnl_pct"] > 0 and segments["oos_apr_may"]["total_pnl_pct"] > 0:
        if all(coin_pnls[s] > 0 for s in COINS):
            verdict = "ROBUST_OOS"
        else:
            verdict = "MIXED_OOS"
    elif segments["four_months"]["total_pnl_pct"] > 0:
        verdict = "MIXED_OOS"
    else:
        verdict = "WEAK_OOS"

    report = {
        "manifest": {
            "freeze_tag": FREEZE_TAG,
            "git_head": git_head(),
            "signal_pipeline": "analyze_floor_guard_signal_list_v1.collect_signals + enrich (unchanged)",
            "e1r_logic": "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.simulate_e1_e1r + signal_ignore",
            "command": "python results/pool_scan/context_study_v2/e1r_4month_oos/run_e1r_4month_oos_v1.py",
            "four_month_window": [FOUR_MO_FROM.isoformat(), FOUR_MO_TO.isoformat()],
            "oos_window": [OOS_FROM.isoformat(), OOS_TO.isoformat()],
            "dev_window": [DEV_FROM.isoformat(), DEV_TO.isoformat()],
            "extended_data_available_to": EXT_TO.isoformat(),
        },
        "coverage": coverage,
        "dev_parity_vs_freeze": {"recomputed": dev_recomputed, "freeze": freeze_dev, "ok": dev_parity_ok},
        "segments": segments,
        "per_coin": per_coin,
        "per_month": per_month,
        "compare_table": compare,
        "stability": stability,
        "costs": {"fees": "NOT_AVAILABLE", "funding": "NOT_AVAILABLE", "slippage": "NOT_AVAILABLE"},
        "spot_check": spot,
        "verdict": verdict,
    }

    (OUT_DIR / "oos_summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    # trades csv
    with (OUT_DIR / "e1r_allowed_trades_4mo.csv").open("w", newline="") as f:
        fields = ["symbol", "entry_open", "entry_price", "stop", "tp", "first_hit", "pnl_pct", "mae_pct", "mfe_pct", "exit_time", "ignored_E1R"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in research_all:
            w.writerow(t)

    md = build_md(report)
    (OUT_DIR / "OOS_REPORT.md").write_text(md + "\n")
    print("verdict", verdict, "dev_parity", dev_parity_ok)
    print("wrote", OUT_DIR)


def build_md(r: dict) -> str:
    c = r["compare_table"]
    lines = [
        "# E1R V1 — 4-month OOS (Apr–Jul 2026)",
        "",
        f"**Verdict:** `{r['verdict']}`",
        "",
        "## Data coverage",
        json.dumps(r["coverage"], indent=2),
        "",
        f"Extended candles available to **{r['manifest']['extended_data_available_to']}** (not in primary 4mo report).",
        "",
        "## Dev parity (Jun–Jul E1R trade count)",
        json.dumps(r["dev_parity_vs_freeze"], indent=2),
        "",
        "## Comparison",
        "| Metrik | Jun–Jul Freeze | 4 Monate | OOS Apr–May |",
        "|---|---:|---:|---:|",
    ]
    for k, label in [
        ("trades", "Trades"),
        ("pnl", "PnL %"),
        ("pf", "PF"),
        ("dd", "Max DD %"),
        ("winrate", "Winrate"),
        ("max_sl_streak", "Max SL-Serie"),
    ]:
        f = c["jun_jul_freeze"].get("pnl" if k == "pnl" else k)
        a = c["four_months_total"].get("total_pnl_pct" if k == "pnl" else k)
        o = c["oos_only"].get("total_pnl_pct" if k == "pnl" else k)
        lines.append(f"| {label} | {f} | {a} | {o} |")
    lines += ["", "## Per coin (research, E1R allowed)", json.dumps(r["per_coin"], indent=2)]
    lines += ["", "## Per month", json.dumps(r["per_month"], indent=2)]
    lines += ["", "## Execution-realistic (1 pos/coin)", json.dumps(r["segments"]["execution_realistic_four_months"], indent=2)]
    lines += ["", "## Stability", json.dumps(r["stability"], indent=2)]
    lines += ["", "## Costs", json.dumps(r["costs"], indent=2)]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
