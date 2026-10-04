"""Final Baseline (with_guard) vs E1R shadow PnL comparison — no rule changes."""

from __future__ import annotations

import csv
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_crosscoin_e1r_final_pnl_comparison_v1.json"
OUT_MD = ROOT / "analyze_crosscoin_e1r_final_pnl_comparison_v1.md"
OUT_TRADES = ROOT / "analyze_crosscoin_e1r_final_pnl_trades_v1.csv"
OUT_EQUITY = ROOT / "analyze_crosscoin_e1r_final_equity_curve_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
BAD4_XRP = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}

BASELINE_PARITY = {
    "XRPUSDT": {"trades": 25, "sl": 8, "tp": 17},
    "ADAUSDT": {"trades": 18, "sl": 7, "tp": 11},
    "DOGEUSDT": {"trades": 28, "sl": 8, "tp": 19},
}
E1R_PARITY = {
    "XRPUSDT": {"trades": 15, "sl": 2, "tp_first_lost": 4, "max_sl_streak": 1},
    "ADAUSDT": {"trades": 11, "sl": 2, "tp_first_lost": 2, "max_sl_streak": 2},
    "DOGEUSDT": {"trades": 22, "sl": 3, "tp_first_lost": 1, "max_sl_streak": 1},
}

E1R_SHADOW_FILES = [
    "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py",
    "analyze_xrp_ema200_cluster3_persistent_shadow_v1.py",
    "analyze_xrp_bull_regime_exit_shadow_v1.py",
    "analyze_xrp_ema200_cluster_transition_shadow_v1.py",
    "analyze_xrp_pool_ladder_forensics_v1.py",
]
BASELINE_FILES = [
    "floor_guard_signal_list_v1.json",
    "floor_guard_signal_list_v1.md",
    "find_short_entry_15m_v1.py",
]


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def git_status() -> dict:
    try:
        head = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        branch = subprocess.check_output(["git", "-C", str(REPO), "branch", "--show-current"], text=True).strip()
    except Exception as e:
        return {"error": str(e)}
    return {"repo": str(REPO), "branch": branch, "head": head}


def code_status_doc() -> dict:
    return {
        "classification": {
            "A_production_baseline": (
                "Live/Scanner-Pfad: Short-Entry-Scanner (find_short_entry_15m_v1) + bestehende Guards "
                "außerhalb dieses Reports. `with_guard` in floor_guard_signal_list_v1 = dokumentierte "
                "Baseline-Kohorte Jun–Jul 2026 (Floor-Guard gefilterte Signale), nicht E1R."
            ),
            "B_shadow_analysis_only": (
                "E1R und alle analyze_*ema200* / crosscoin_e1* Skripte unter results/pool_scan/context_study_v1/ — "
                "reine Shadow-/Forensik-Logik, keine Integration in pool_pattern/machine oder Live-Bot gefunden."
            ),
            "C_not_integrated": (
                "E1R one-bar reclaim, E1, GAP3, Entry-Forensik, D4-Shadows — nur JSON/MD/CSV-Artefakte."
            ),
        },
        "e1r_in_executable_strategy_path": False,
        "note": "Repo grep: E1R nur in context_study_v1 Shadow-Skripten referenziert.",
    }


def trade_row(t: dict, executed: bool, variant: str) -> dict:
    fh = t.get("first_hit")
    exit_reason = "TP" if fh == "TP" else ("SL" if fh == "SL" else fh)
    return {
        "symbol": t["symbol"],
        "signal_time": t.get("signal_time"),
        "entry_time": t["entry_open"],
        "entry_price": t.get("entry_price"),
        "exit_time": t.get("exit_time"),
        "exit_price": t.get("exit_price"),
        "exit_reason": exit_reason,
        "direction": "short",
        "mae_pct": t.get("mae_pct"),
        "mfe_pct": t.get("mfe_pct"),
        "pnl_pct": t.get("pnl_pct"),
        "pnl_R": t.get("r_multiple"),
        "pnl_usdt": "NOT_AVAILABLE",
        "fees": "NOT_AVAILABLE",
        "funding": "NOT_AVAILABLE",
        "net_pnl_pct": t.get("pnl_pct"),
        "final_outcome": t.get("final_outcome"),
        "variant": variant,
        "executed": executed,
        "E1R_blocked": t.get("ignored_E1R") if variant == "baseline_row" else None,
        "baseline_outcome": t.get("final_outcome"),
        "baseline_pnl_pct": t.get("pnl_pct"),
        "E1R_status": t.get("E1R_status"),
        "block_reason": "E1R_active_ignore" if t.get("ignored_E1R") else None,
    }


def sl_streak_max(trades: list[dict]) -> int:
    streak = best = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def sl_streaks_ge2(trades: list[dict]) -> list[int]:
    streaks = []
    streak = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
        else:
            if streak >= 2:
                streaks.append(streak)
            streak = 0
    if streak >= 2:
        streaks.append(streak)
    return streaks


def equity_series(trades: list[dict], label: str) -> list[dict]:
    """Non-compounded: equity = 100 + sum(pnl_pct); matches signal list cumulative style."""
    rows = sorted(trades, key=lambda x: x["entry_open"])
    eq = 100.0
    peak = eq
    peak_time = None
    out = []
    max_dd = 0.0
    dd_start = dd_trough_time = None
    for t in rows:
        pnl = float(t.get("pnl_pct") or 0.0)
        ts = t["entry_open"]
        eq += pnl
        if eq >= peak:
            peak = eq
            peak_time = ts
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            dd_start = peak_time
            dd_trough_time = ts
        out.append(
            {
                "timestamp": ts,
                "symbol": t["symbol"],
                "curve": label,
                "trade_pnl_pct": pnl,
                "equity": round(eq, 6),
                "drawdown_pct": round(dd, 6),
            }
        )
    return out


def drawdown_detail(series: list[dict]) -> dict:
    if not series:
        return {}
    max_dd = max(s["drawdown_pct"] for s in series)
    trough = next(s for s in series if s["drawdown_pct"] == max_dd)
    peak_eq = trough["equity"] + max_dd
    start_peak_ts = None
    for s in series:
        if abs(s["equity"] - peak_eq) < 1e-9:
            start_peak_ts = s["timestamp"]
    recovery_ts = None
    after = False
    for s in series:
        if s["timestamp"] == trough["timestamp"]:
            after = True
            continue
        if after and s["drawdown_pct"] <= 1e-9:
            recovery_ts = s["timestamp"]
            break
    return {
        "max_drawdown_pct": round(max_dd, 6),
        "peak_timestamp": start_peak_ts,
        "trough_timestamp": trough["timestamp"],
        "recovery_timestamp": recovery_ts,
        "dd_duration_trades": sum(
            1 for s in series if start_peak_ts and trough["timestamp"] and start_peak_ts <= s["timestamp"] <= trough["timestamp"]
        ),
    }


def trading_stats(trades: list[dict]) -> dict:
    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
    oc = [t.get("final_outcome") for t in trades]
    return {
        "trades": len(trades),
        "winners": len(wins),
        "losers": len(losses),
        "winrate": round(len(wins) / len(trades), 4) if trades else None,
        "TP_FIRST": sum(1 for o in oc if o == "TP_FIRST"),
        "SL_FIRST": sum(1 for o in oc if o == "SL_FIRST"),
        "MOVE_041_THEN_SL": sum(1 for o in oc if o == "MOVE_041_THEN_SL"),
        "profit_factor": pf,
        "expectancy_per_trade_pct": round(statistics.mean(pnls), 6) if pnls else None,
        "avg_winner_pct": round(statistics.mean(wins), 6) if wins else None,
        "avg_loser_pct": round(statistics.mean(losses), 6) if losses else None,
        "total_pnl_pct": round(sum(pnls), 6),
        "median_pnl_pct": round(statistics.median(pnls), 6) if pnls else None,
        "best_trade_pct": round(max(pnls), 6) if pnls else None,
        "worst_trade_pct": round(min(pnls), 6) if pnls else None,
        "max_sl_streak": sl_streak_max(trades),
        "sl_streaks_ge2": sl_streaks_ge2(trades),
        "median_mae": round(statistics.median(maes), 6) if maes else None,
        "mean_mae": round(statistics.mean(maes), 6) if maes else None,
        "median_mfe": round(statistics.median(mfes), 6) if mfes else None,
        "mean_mfe": round(statistics.mean(mfes), 6) if mfes else None,
    }


def opportunity_cost(blocked: list[dict]) -> dict:
    winners = [t for t in blocked if float(t.get("pnl_pct") or 0) > 0]
    losers = [t for t in blocked if float(t.get("pnl_pct") or 0) < 0]
    def pack(rows):
        pnls = [float(r["pnl_pct"]) for r in rows]
        maes = [float(r["mae_pct"]) for r in rows if r.get("mae_pct") is not None]
        mfes = [float(r["mfe_pct"]) for r in rows if r.get("mfe_pct") is not None]
        return {
            "count": len(rows),
            "sum_pnl_pct": round(sum(pnls), 6) if pnls else 0,
            "avg_pnl_pct": round(statistics.mean(pnls), 6) if pnls else None,
            "median_pnl_pct": round(statistics.median(pnls), 6) if pnls else None,
            "median_mae": round(statistics.median(maes), 6) if maes else None,
            "median_mfe": round(statistics.median(mfes), 6) if mfes else None,
        }
    return {"blocked_winners": pack(winners), "blocked_losers": pack(losers)}


def verify_parity(coin: str, baseline_stats: dict, e1r_stats: dict, cohort_e1r: dict) -> dict:
    b = BASELINE_PARITY[coin]
    e = E1R_PARITY[coin]
    checks = {
        "baseline_trades": baseline_stats["trades"] == b["trades"],
        "baseline_sl": sum(1 for t in baseline_stats.get("_trades", []) if t.get("first_hit") == "SL") == b["sl"]
        if "_trades" in baseline_stats
        else None,
        "e1r_trades": e1r_stats["trades"] == e["trades"],
        "e1r_sl": e1r_stats.get("sl_count") == e["sl"],
        "e1r_tp_lost": cohort_e1r.get("tp_first_lost") == e["tp_first_lost"],
        "e1r_max_sl_streak": e1r_stats["max_sl_streak"] == e["max_sl_streak"],
    }
    ok = all(v for v in checks.values() if v is not None)
    return {"ok": ok, "checks": checks, "expected_baseline": b, "expected_e1r": e}


def build_md(report: dict) -> str:
    lines = [
        "# Final Baseline vs E1R PnL comparison v1",
        "",
        "### Code status",
        json.dumps(report["code_status"], indent=2),
        "",
        "### Run manifest",
        json.dumps(report["manifest"], indent=2),
        "",
        "### PnL methodology",
        json.dumps(report["pnl_methodology"], indent=2),
        "",
        "### Baseline parity",
        json.dumps(report["parity"]["baseline"], indent=2),
        "",
        "### E1R parity",
        json.dumps(report["parity"]["e1r"], indent=2),
        "",
    ]
    if not report["parity"]["all_ok"]:
        lines += ["**PARITY FAILED — see parity section.**", ""]
    for sym in COINS:
        short = sym.replace("USDT", "")
        lines += [
            f"### {short} PnL",
            "| | Baseline | E1R |",
            "|---|--:|--:|",
        ]
        b, e = report["by_coin"][sym]["baseline"]["pnl"], report["by_coin"][sym]["e1r"]["pnl"]
        for k in ("total_pnl_pct", "expectancy_per_trade_pct", "profit_factor", "max_drawdown_pct"):
            lines.append(f"| {k} | {b.get(k) if k != 'max_drawdown_pct' else report['by_coin'][sym]['baseline']['drawdown'].get('max_drawdown_pct')} | {e.get(k) if k != 'max_drawdown_pct' else report['by_coin'][sym]['e1r']['drawdown'].get('max_drawdown_pct')} |")
        lines.append("")
    lines += ["### Cross-coin PnL", report["cross_coin_table"], ""]
    lines += ["### Drawdown", json.dumps(report["drawdown_summary"], indent=2), ""]
    lines += ["### Blocked winners", json.dumps(report["blocked_opportunity"]["winners_agg"], indent=2), ""]
    lines += ["### Avoided losses", json.dumps(report["blocked_opportunity"]["losers_agg"], indent=2), ""]
    lines += ["### BAD-4", json.dumps(report["bad4"], indent=2), ""]
    lines += ["### Equity curve", report["equity_summary"], ""]
    lines += ["### Freeze readiness", report["freeze_readiness"], ""]
    lines += ["### Freeze plan", json.dumps(report["freeze_plan"], indent=2), ""]
    return "\n".join(lines)


def main() -> None:
    _ensure_paths()
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import analyze_coin, cohort

    data = json.loads(SIGNAL_JSON.read_text())
    window = data.get("window")
    git = git_status()

    all_baseline_rows = []
    all_e1r_rows = []
    all_blocked = []
    by_coin = {}
    parity = {"baseline": {}, "e1r": {}, "all_ok": True}

    for sym in COINS:
        coin = analyze_coin(sym, data["symbols"][sym], BAD4_XRP if sym == "XRPUSDT" else set())
        wg = coin["wg_trades"]
        baseline_trades = list(wg)
        e1r_trades = [t for t in wg if not t["ignored_E1R"]]
        blocked = [t for t in wg if t["ignored_E1R"]]
        all_blocked.extend(blocked)

        b_stats = trading_stats(baseline_trades)
        b_stats["_trades"] = baseline_trades
        b_stats["sl_count"] = sum(1 for t in baseline_trades if t.get("first_hit") == "SL")
        e_stats = trading_stats(e1r_trades)
        e_stats["sl_count"] = sum(1 for t in e1r_trades if t.get("first_hit") == "SL")
        e_cohort = coin["comparison"]["E1R"]

        bp = verify_parity(sym, b_stats, e_stats, e_cohort)
        bp["baseline_sl_tp"] = {"sl": b_stats["sl_count"], "tp": sum(1 for t in baseline_trades if t.get("first_hit") == "TP")}
        parity["baseline"][sym] = bp
        parity["e1r"][sym] = bp
        if not bp["ok"]:
            parity["all_ok"] = False

        b_eq = equity_series(baseline_trades, "baseline")
        e_eq = equity_series(e1r_trades, "e1r")
        b_dd = drawdown_detail(b_eq)
        e_dd = drawdown_detail(e_eq)
        rf_b = round(b_stats["total_pnl_pct"] / b_dd["max_drawdown_pct"], 4) if b_dd.get("max_drawdown_pct") else None
        rf_e = round(e_stats["total_pnl_pct"] / e_dd["max_drawdown_pct"], 4) if e_dd.get("max_drawdown_pct") else None

        by_coin[sym] = {
            "baseline": {"pnl": {k: b_stats[k] for k in b_stats if not k.startswith("_")}, "drawdown": b_dd, "recovery_factor": rf_b},
            "e1r": {"pnl": {k: e_stats[k] for k in e_stats if k != "sl_count"}, "drawdown": e_dd, "recovery_factor": rf_e},
            "opportunity_cost": opportunity_cost(blocked),
            "equity_baseline": b_eq,
            "equity_e1r": e_eq,
        }
        all_baseline_rows.extend(baseline_trades)
        all_e1r_rows.extend(e1r_trades)

    # Cross-coin chronological equity
    cross_b = equity_series(all_baseline_rows, "baseline_cross")
    cross_e = equity_series(all_e1r_rows, "e1r_cross")

    b_agg = trading_stats(all_baseline_rows)
    e_agg = trading_stats(all_e1r_rows)
    cross_dd_b = drawdown_detail(cross_b)
    cross_dd_e = drawdown_detail(cross_e)

    winners_b = [t for t in all_blocked if float(t.get("pnl_pct") or 0) > 0]
    losers_b = [t for t in all_blocked if float(t.get("pnl_pct") or 0) < 0]

    bad4 = []
    for t in all_blocked:
        if t["entry_open"] in BAD4_XRP:
            bad4.append(
                {
                    "entry_open": t["entry_open"],
                    "baseline_pnl_pct": t.get("pnl_pct"),
                    "E1R_blocked": True,
                    "avoided_loss_pct": t.get("pnl_pct"),
                    "mae_pct": t.get("mae_pct"),
                    "mfe_pct": t.get("mfe_pct"),
                    "final_outcome": t.get("final_outcome"),
                }
            )

    cross_table = (
        "| Metrik | Baseline | E1R |\n|---|---:|---:|\n"
        f"| Trades gesamt | {b_agg['trades']} | {e_agg['trades']} |\n"
        f"| Gewinner | {b_agg['winners']} | {e_agg['winners']} |\n"
        f"| Verlierer | {b_agg['losers']} | {e_agg['losers']} |\n"
        f"| Gesamt-PnL % | {b_agg['total_pnl_pct']} | {e_agg['total_pnl_pct']} |\n"
        f"| Gesamt-PnL USDT | NOT_AVAILABLE | NOT_AVAILABLE |\n"
        f"| Profit Factor | {b_agg['profit_factor']} | {e_agg['profit_factor']} |\n"
        f"| Expectancy | {b_agg['expectancy_per_trade_pct']} | {e_agg['expectancy_per_trade_pct']} |\n"
        f"| Max Drawdown % | {cross_dd_b.get('max_drawdown_pct')} | {cross_dd_e.get('max_drawdown_pct')} |\n"
        f"| Max Verlustserie | {b_agg['max_sl_streak']} | {e_agg['max_sl_streak']} |\n"
        f"| Median MAE | {b_agg['median_mae']} | {e_agg['median_mae']} |\n"
        f"| Median MFE | {b_agg['median_mfe']} | {e_agg['median_mfe']} |\n"
    )

    report = {
        "manifest": {
            **git,
            "signal_source": str(SIGNAL_JSON.relative_to(REPO)),
            "window": window,
            "coins": list(COINS),
            "baseline_files": [str((ROOT / f).relative_to(REPO)) for f in BASELINE_FILES],
            "e1r_shadow_files": [str((ROOT / f).relative_to(REPO)) for f in E1R_SHADOW_FILES],
            "e1r_canonical": "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py",
        },
        "code_status": code_status_doc(),
        "pnl_methodology": {
            "position_size": "NOT_AVAILABLE — nur pro-Trade pnl_pct aus Signal-Backtest",
            "leverage": "NOT_AVAILABLE",
            "sl_tp": "Aus Scanner/Signal-JSON (stop/tp Felder); Entry = 15m Close",
            "fees": "NOT_AVAILABLE",
            "funding": "NOT_AVAILABLE",
            "slippage": "NOT_AVAILABLE",
            "compounding": False,
            "equity_definition": "Start 100 + chronologische Summe pnl_pct pro ausgeführtem Trade (wie cumulative_pnl_pct in floor_guard_signal_list_v1.md)",
            "pnl_pct_source": "floor_guard_signal_list_v1.json field pnl_pct",
            "pnl_R": "r_multiple aus Signal-JSON wenn vorhanden",
            "net_pnl": "identisch pnl_pct (keine Fee-Logik)",
            "variants": {"A_raw": "pnl_pct ohne Fees", "B_net": "NOT_AVAILABLE"},
        },
        "parity": parity,
        "by_coin": by_coin,
        "cross_coin": {
            "baseline": b_agg,
            "e1r": e_agg,
            "table_md": cross_table,
            "drawdown_baseline": cross_dd_b,
            "drawdown_e1r": cross_dd_e,
        },
        "blocked_opportunity": {
            "winners_agg": opportunity_cost(winners_b)["blocked_winners"],
            "losers_agg": opportunity_cost(losers_b)["blocked_losers"],
            "per_coin": {s: by_coin[s]["opportunity_cost"] for s in COINS},
        },
        "bad4": bad4,
        "drawdown_summary": {
            "per_coin": {s: {"baseline": by_coin[s]["baseline"]["drawdown"], "e1r": by_coin[s]["e1r"]["drawdown"]} for s in COINS},
            "cross": {"baseline": cross_dd_b, "e1r": cross_dd_e},
        },
        "equity_summary": (
            f"Baseline end equity {cross_b[-1]['equity'] if cross_b else None}; "
            f"E1R end equity {cross_e[-1]['equity'] if cross_e else None}; "
            f"CSV: analyze_crosscoin_e1r_final_equity_curve_v1.csv"
        ),
        "freeze_readiness": (
            "Ja, sofern Parität ok: reproduzierbar via floor_guard_signal_list_v1.json + "
            "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.analyze_coin @ dokumentiertem HEAD."
            if parity["all_ok"]
            else "NEIN bis Paritätsabweichung geklärt."
        ),
        "freeze_plan": {
            "proposed_tag": "e1r_cluster3_ema200_shadow_freeze_v1",
            "branch": git.get("branch"),
            "head": git.get("head"),
            "shadow_code": [f"results/pool_scan/context_study_v1/{f}" for f in E1R_SHADOW_FILES + ["analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py"]],
            "research_artifacts": [
                "results/pool_scan/context_study_v1/floor_guard_signal_list_v1.json",
                "results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.json",
                "results/pool_scan/context_study_v1/analyze_crosscoin_e1r_final_pnl_comparison_v1.json",
            ],
            "production_code": "Nicht Teil dieses Freeze (E1R nicht integriert)",
            "config_constants": "REPORT_FROM/TO, PANE_FROM/TO in analyze_xrp_ema200_cluster3_persistent_shadow_v1.py; cluster gap 0.35% via price_clusters",
            "do_not_commit_automatically": True,
        },
    }
    report["cross_coin_table"] = cross_table
    report["executive_summary"] = build_md(report)

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(report["executive_summary"] + "\n")

    # Trades CSV
    fields = [
        "symbol", "signal_time", "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason",
        "direction", "mae_pct", "mfe_pct", "pnl_pct", "pnl_R", "pnl_usdt", "fees", "funding", "net_pnl_pct",
        "final_outcome", "variant", "executed", "E1R_blocked", "baseline_outcome", "baseline_pnl_pct",
        "E1R_status", "block_reason",
    ]
    with OUT_TRADES.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in sorted(all_baseline_rows, key=lambda x: x["entry_open"]):
            ex = not t["ignored_E1R"]
            row = trade_row(t, ex, "E1R" if ex else "baseline_blocked")
            row["E1R_blocked"] = t.get("ignored_E1R")
            w.writerow(row)

    with OUT_EQUITY.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "curve", "trade_pnl_pct", "equity", "drawdown_pct"])
        w.writeheader()
        for sym in COINS:
            for r in by_coin[sym]["equity_baseline"]:
                w.writerow(r)
            for r in by_coin[sym]["equity_e1r"]:
                w.writerow(r)
        for r in cross_b:
            r2 = {**r, "symbol": "CROSS"}
            w.writerow(r2)
        for r in cross_e:
            r2 = {**r, "symbol": "CROSS"}
            w.writerow(r2)

    print("parity_ok", parity["all_ok"])
    print("wrote", OUT_JSON, OUT_MD, OUT_TRADES, OUT_EQUITY)
    if not parity["all_ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
