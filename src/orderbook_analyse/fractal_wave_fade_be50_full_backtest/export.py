"""Export full BE50 A/B results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_full_backtest import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def _fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:+.{digits}f}%" if abs(x) < 1e6 else f"{x:+.4g}%"


def _fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1e6:
        return f"{x:.4g}"
    return f"{x:.{digits}f}"


def render_summary(p: dict[str, Any]) -> str:
    if p.get("baseline_reproduction_failed"):
        br = p.get("baseline_reproduction", {})
        return (
            "Primary Decision: **BASELINE_REPRODUCTION_FAILED**\n\n"
            f"Reason: `{br.get('reason')}`\n\n"
            f"Details: `{json.dumps(_jsonable(br), indent=2)}`\n"
        )

    b, e = p["base_summary"], p["be_summary"]
    c = p["counts"]
    tb, te = p["true_sl_base"], p["true_sl_be"]
    rb, re_ = p["risk_base"], p["risk_be"]
    monthly = p["monthly"]
    br = p["baseline_reproduction"]

    def d(a, z):
        if a is None or z is None:
            return "—"
        return z - a

    lines = [
        f"Primary Decision: **{p['decision']}**",
        "",
        "| Metric | Baseline | BE50 | Delta |",
        "| ---------------------- | -------: | ---: | ----: |",
        f"| Total Return | {_fmt_pct(b['performance_pct'])} | {_fmt_pct(e['performance_pct'])} | {_fmt_pct(d(b['performance_pct'], e['performance_pct']))} |",
        f"| End Equity | {_fmt_num(b['end_total'])} | {_fmt_num(e['end_total'])} | {_fmt_num(d(b['end_total'], e['end_total']))} |",
        f"| Max DD | {_fmt_pct(b['max_dd_pct'])} | {_fmt_pct(e['max_dd_pct'])} | {d(b['max_dd_pct'], e['max_dd_pct']):+.2f}pp |",
        f"| Longest TRUE SL Streak | {tb['max_streak']} | {te['max_streak']} | {te['max_streak']-tb['max_streak']:+d} |",
        f"| 3+ SL Streaks | {tb['n_ge_3']} | {te['n_ge_3']} | {te['n_ge_3']-tb['n_ge_3']:+d} |",
        f"| 5+ SL Streaks | {tb['n_ge_5']} | {te['n_ge_5']} | {te['n_ge_5']-tb['n_ge_5']:+d} |",
        f"| SL -> BE | — | {c['SL_TO_BE']} | — |",
        f"| TP -> BE | — | {c['TP_TO_BE']} | — |",
        f"| Return / Max DD | {_fmt_num(rb['return_over_max_dd'])} | {_fmt_num(re_['return_over_max_dd'])} | {_fmt_num(d(rb['return_over_max_dd'], re_['return_over_max_dd']))} |",
        "",
        "## Baseline reproduction",
        "",
        f"- Period: `{br['period_start']}` → `{br['period_end']}`",
        f"- Trades: **{br['n_trades']}**",
        f"- Symbols: `{br['symbols']}`",
        f"- Start capital: ACTIVE={br['cashout_summary']['start_active']}, RESERVE={br['cashout_summary']['start_reserve']}",
        f"- Cashout 30%/100% end wealth (exit-sorted): **{_fmt_num(br['end_total_cashout'])}** ({_fmt_pct(br['cashout_summary']['total_wealth_return_pct'])})",
        f"- Cashout Max DD: **{_fmt_pct(br['max_dd_cashout'])}**",
        f"- A/B path (entry-sorted) end: **{_fmt_num(br['end_total_local'])}**, Max DD **{_fmt_pct(br['max_dd_local'])}**",
        f"- Entry order == exit order: `{br['order_same']}`",
        "",
        f"- Price path: `{p['price_resolution']}`",
        f"- Fee: gross − {p['fee_pct']}%",
        f"- Ambiguous intrabar: **{c['n_ambiguous']}**",
        "",
        "## Hauptvergleich",
        "",
        "| Kennzahl | Baseline | BE50 | Delta |",
        "|---|---:|---:|---:|",
        f"| Trades | {p['n_trades']} | {p['n_trades']} | 0 |",
        f"| TP | {b['n_tp']} | {e['n_tp']} | {e['n_tp']-b['n_tp']:+d} |",
        f"| SL | {b['n_sl']} | {e['n_sl']} | {e['n_sl']-b['n_sl']:+d} |",
        f"| BE | {b['n_be']} | {e['n_be']} | {e['n_be']-b['n_be']:+d} |",
        f"| Winrate | {b['winrate']*100:.2f}% | {e['winrate']*100:.2f}% | {(e['winrate']-b['winrate'])*100:+.2f}pp |",
        f"| Loss Rate | {b['loss_rate']*100:.2f}% | {e['loss_rate']*100:.2f}% | {(e['loss_rate']-b['loss_rate'])*100:+.2f}pp |",
        f"| End Equity | {_fmt_num(b['end_total'])} | {_fmt_num(e['end_total'])} | {_fmt_num(equity_d := e['end_total']-b['end_total'])} |",
        f"| Gesamtperformance | {_fmt_pct(b['performance_pct'])} | {_fmt_pct(e['performance_pct'])} | {_fmt_pct(e['performance_pct']-b['performance_pct'])} |",
        f"| Max Drawdown | {_fmt_pct(b['max_dd_pct'])} | {_fmt_pct(e['max_dd_pct'])} | {e['max_dd_pct']-b['max_dd_pct']:+.2f}pp |",
        f"| Profit Factor | {_fmt_num(b['profit_factor'], 3)} | {_fmt_num(e['profit_factor'], 3)} | {_fmt_num((e['profit_factor'] or 0)-(b['profit_factor'] or 0), 3)} |",
        f"| Avg Trade | {_fmt_pct(b['avg_trade_pct'])} | {_fmt_pct(e['avg_trade_pct'])} | {_fmt_pct(e['avg_trade_pct']-b['avg_trade_pct'])} |",
        f"| Median Trade | {_fmt_pct(b['median_trade_pct'])} | {_fmt_pct(e['median_trade_pct'])} | {_fmt_pct(e['median_trade_pct']-b['median_trade_pct'])} |",
        "",
        "## TRUE_SL_STREAK",
        "",
        "| Metric | Baseline | BE50 |",
        "|---|---:|---:|",
        f"| longest | {tb['max_streak']} | {te['max_streak']} |",
        f"| 2nd | {tb['second_max']} | {te['second_max']} |",
        f"| 3rd | {tb['third_max']} | {te['third_max']} |",
        f"| mean | {tb['mean_streak']:.2f} | {te['mean_streak']:.2f} |",
        f"| median | {tb['median_streak']:.2f} | {te['median_streak']:.2f} |",
        f"| >=2 | {tb['n_ge_2']} | {te['n_ge_2']} |",
        f"| >=3 | {tb['n_ge_3']} | {te['n_ge_3']} |",
        f"| >=4 | {tb['n_ge_4']} | {te['n_ge_4']} |",
        f"| >=5 | {tb['n_ge_5']} | {te['n_ge_5']} |",
        f"| >=6 | {tb['n_ge_6']} | {te['n_ge_6']} |",
        f"| >=7 | {tb['n_ge_7']} | {te['n_ge_7']} |",
        f"| >=8 | {tb['n_ge_8']} | {te['n_ge_8']} |",
        f"| >=10 | {tb['n_ge_10']} | {te['n_ge_10']} |",
        "",
        "## NON_WINNER_STREAK (SL+BE)",
        "",
        "| Metric | Baseline | BE50 |",
        "|---|---:|---:|",
        f"| longest | {p['nw_base']['max_streak']} | {p['nw_be']['max_streak']} |",
        f"| >=3 | {p['nw_base']['n_ge_3']} | {p['nw_be']['n_ge_3']} |",
        f"| >=5 | {p['nw_base']['n_ge_5']} | {p['nw_be']['n_ge_5']} |",
        "",
        "## SL→BE vs TP→BE",
        "",
        f"- SL_TO_BE: **{c['SL_TO_BE']}**",
        f"- TP_TO_BE: **{c['TP_TO_BE']}**",
        f"- UNCHANGED_SL: {c['UNCHANGED_SL']}",
        f"- UNCHANGED_TP: {c['UNCHANGED_TP']}",
        f"- Saved Loss (sum Δnet): **{p['total_saved_loss_pct']:+.2f}pp**",
        f"- Lost Winner Profit: **{p['total_lost_winner_profit_pct']:+.2f}pp**",
        f"- net_BE50_effect (pct-sum): **{p['be50_net_benefit_pct']:+.2f}pp**",
        f"- Equity delta: **{_fmt_num(p['equity_delta'])}** ({(p['equity_delta']/b['end_total']*100) if b['end_total'] else 0:+.2f}% of baseline end)",
        "",
        "## Drawdown",
        "",
        "| Metric | Baseline | BE50 |",
        "|---|---:|---:|",
        f"| Max DD | {_fmt_pct(b['max_dd_pct'])} | {_fmt_pct(e['max_dd_pct'])} |",
        f"| Avg DD (episodes) | {_fmt_pct(b['avg_dd_pct'])} | {_fmt_pct(e['avg_dd_pct'])} |",
        f"| Median DD | {_fmt_pct(b['median_dd_pct'])} | {_fmt_pct(e['median_dd_pct'])} |",
        f"| DD >2% | {b['n_dd_gt_2']} | {e['n_dd_gt_2']} |",
        f"| DD >5% | {b['n_dd_gt_5']} | {e['n_dd_gt_5']} |",
        f"| DD >10% | {b['n_dd_gt_10']} | {e['n_dd_gt_10']} |",
        f"| Longest DD duration (trades) | {b['longest_dd_duration_trades']} | {e['longest_dd_duration_trades']} |",
        f"| Trades to recover max DD | {b['max_dd_recovery_trades']} | {e['max_dd_recovery_trades']} |",
        f"| Strongest loss cluster (sum net %) | {_fmt_pct(b['strongest_loss_cluster_net_pct'])} | {_fmt_pct(e['strongest_loss_cluster_net_pct'])} |",
        "",
        "## Risk / Return",
        "",
        "| Metric | Baseline | BE50 |",
        "|---|---:|---:|",
        f"| Return / Max DD | {_fmt_num(rb['return_over_max_dd'])} | {_fmt_num(re_['return_over_max_dd'])} |",
        f"| Return / Avg DD | {_fmt_num(rb['return_over_avg_dd'])} | {_fmt_num(re_['return_over_avg_dd'])} |",
        f"| Profit Factor | {_fmt_num(rb['profit_factor'], 3)} | {_fmt_num(re_['profit_factor'], 3)} |",
        f"| Expectancy / trade | {_fmt_pct(rb['expectancy_pct'])} | {_fmt_pct(re_['expectancy_pct'])} |",
        f"| Worst month | {_fmt_pct(rb['worst_month_pct'])} | {_fmt_pct(re_['worst_month_pct'])} |",
        f"| Worst TRUE SL streak (len) | {rb['worst_true_sl_streak']} | {re_['worst_true_sl_streak']} |",
        f"| Worst SL streak sum net | {_fmt_pct(rb['worst_sl_streak_net_pct'])} | {_fmt_pct(re_['worst_sl_streak_net_pct'])} |",
        f"| Worst 10-trade block | {_fmt_pct(rb['worst_10_trade_block_pct'])} | {_fmt_pct(re_['worst_10_trade_block_pct'])} |",
        "",
        "## Monthly (local restart 1000)",
        "",
        f"- Months BE50 better perf: **{int(monthly['be50_better_perf'].sum())}/{len(monthly)}**",
        f"- Months Baseline better perf: **{int((~monthly['be50_better_perf']).sum())}/{len(monthly)}**",
        f"- Months BE50 better Max DD: **{int(monthly['be50_better_dd'].sum())}/{len(monthly)}**",
        f"- Months BE50 shorter max SL streak: **{int(monthly['be50_shorter_sl'].sum())}/{len(monthly)}**",
        "",
    ]

    # July sanity
    jul = monthly[monthly["month"] == "2026-07"] if len(monthly) else monthly
    if len(jul):
        r = jul.iloc[0]
        lines += [
            "### July 2026 sanity",
            "",
            f"- Baseline: {_fmt_pct(r['baseline_pct'])}, Max DD {_fmt_pct(r['baseline_max_dd'])}, longest SL {int(r['longest_sl_baseline'])}",
            f"- BE50: {_fmt_pct(r['be50_pct'])}, Max DD {_fmt_pct(r['be50_max_dd'])}, longest SL {int(r['longest_sl_be50'])}",
            "- Expected ≈ Baseline +26.32% / DD -6.37% / SL streak 5; BE50 +25.32% / DD -3.58% / SL streak 2",
            "",
        ]

    lines += [
        "## Closing answers",
        "",
        f"1. Baseline Gesamtperformance: **{_fmt_pct(b['performance_pct'])}** (end {_fmt_num(b['end_total'])})",
        f"2. BE50 Gesamtperformance: **{_fmt_pct(e['performance_pct'])}** (end {_fmt_num(e['end_total'])})",
        f"3. Max DD: {_fmt_pct(b['max_dd_pct'])} → {_fmt_pct(e['max_dd_pct'])} ({e['max_dd_pct']-b['max_dd_pct']:+.2f}pp)",
        f"4. Longest TRUE SL Baseline: **{tb['max_streak']}**",
        f"5. Longest TRUE SL BE50: **{te['max_streak']}**",
        f"6. 3+ SL streaks: **{tb['n_ge_3']} → {te['n_ge_3']}**",
        f"7. 5+ SL streaks: **{tb['n_ge_5']} → {te['n_ge_5']}**",
        f"8. SL→BE: **{c['SL_TO_BE']}**",
        f"9. TP→BE: **{c['TP_TO_BE']}**",
        f"10. Renditekosten: end equity Δ **{_fmt_num(p['equity_delta'])}** ({(e['end_total']/b['end_total']-1)*100:+.2f}% vs baseline end); perf Δ {_fmt_pct(e['performance_pct']-b['performance_pct'])}",
        f"11. Risikoreduktion wert? → siehe Primary Decision **{p['decision']}**",
        f"12. Monate: BE50 besser in {int(monthly['be50_better_perf'].sum())}/{len(monthly)}; SL-Streak kürzer in {int(monthly['be50_shorter_sl'].sum())}/{len(monthly)}",
        "",
    ]
    return "\n".join(lines)


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    if payload.get("baseline_reproduction_failed"):
        p = out_dir / "summary.md"
        p.write_text(render_summary(payload), encoding="utf-8")
        paths["summary_md"] = p
        p = out_dir / "summary.json"
        p.write_text(json.dumps(_jsonable(payload), indent=2) + "\n", encoding="utf-8")
        paths["summary_json"] = p
        return paths

    def w(name: str, df: pd.DataFrame) -> None:
        path = out_dir / name
        df.to_csv(path, index=False)
        paths[name] = path

    w("full_trade_comparison.csv", payload["comparison"])
    w("equity_comparison.csv", payload["equity_comparison"])
    w("monthly_comparison.csv", payload["monthly"])
    w("sl_streak_distribution.csv", payload["sl_streak_distribution"])
    w("non_winner_streak_distribution.csv", payload["non_winner_streak_distribution"])
    w("top_sl_streaks.csv", payload["top_sl_streaks"])
    w("symbol_comparison.csv", payload["symbol_comparison"])
    w("side_comparison.csv", payload["side_comparison"])
    w("tp_profile_comparison.csv", payload["tp_profile_comparison"])
    w("changed_trades.csv", payload["changed_trades"])

    (out_dir / "DEFINITIONS.md").write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")

    sm = out_dir / "summary.md"
    sm.write_text(render_summary(payload), encoding="utf-8")
    paths["summary_md"] = sm

    summary = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "price_resolution": payload["price_resolution"],
        "fee_pct": payload["fee_pct"],
        "n_trades": payload["n_trades"],
        "baseline_reproduction": {
            k: v
            for k, v in payload["baseline_reproduction"].items()
            if k not in ("local_path",)
        },
        "baseline": payload["base_summary"],
        "be50": payload["be_summary"],
        "counts": payload["counts"],
        "true_sl_base": {k: v for k, v in payload["true_sl_base"].items() if k != "top_streaks"},
        "true_sl_be": {k: v for k, v in payload["true_sl_be"].items() if k != "top_streaks"},
        "nw_base": {k: v for k, v in payload["nw_base"].items() if k != "top_streaks"},
        "nw_be": {k: v for k, v in payload["nw_be"].items() if k != "top_streaks"},
        "total_saved_loss_pct": payload["total_saved_loss_pct"],
        "total_lost_winner_profit_pct": payload["total_lost_winner_profit_pct"],
        "be50_net_benefit_pct": payload["be50_net_benefit_pct"],
        "equity_delta": payload["equity_delta"],
        "risk_base": payload["risk_base"],
        "risk_be": payload["risk_be"],
    }
    sj = out_dir / "summary.json"
    sj.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = sj
    return paths
