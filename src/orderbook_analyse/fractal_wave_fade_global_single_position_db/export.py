"""Write global-single-position backtest artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_global_single_position_db import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):  # NaN
        return None
    return x


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    cov = payload["coverage_df"]
    p = out_dir / "coverage.csv"
    cov.to_csv(p, index=False)
    paths["coverage"] = p

    trades = payload["trades_df"]
    trade_cols = [
        "trade_id",
        "symbol",
        "side",
        "signal_time",
        "entry_time",
        "exit_time",
        "first_signal_tf",
        "highest_tf_reached",
        "entry_price",
        "exit_price",
        "exit_reason",
        "gross_return_pct",
        "fee_pct",
        "net_return_pct",
        "upgrade_count",
        "upgrade_sequence",
        "holding_minutes",
        "equity_before_25",
        "equity_after_25",
        "equity_before_50",
        "equity_after_50",
        "equity_before_100",
        "equity_after_100",
    ]
    p = out_dir / "trades.csv"
    if trades is None or trades.empty:
        pd.DataFrame(columns=trade_cols).to_csv(p, index=False)
    else:
        cols = [c for c in trade_cols if c in trades.columns]
        trades[cols].to_csv(p, index=False)
    paths["trades"] = p

    for tag in ("25", "50", "100"):
        eq = payload["equity_curves"][tag]
        p = out_dir / f"equity_curve_{tag}.csv"
        eq.to_csv(p, index=False)
        paths[f"equity_{tag}"] = p

    supp = payload["suppressed_df"]
    p = out_dir / "suppressed_signals.csv"
    supp.to_csv(p, index=False)
    paths["suppressed"] = p

    p = out_dir / "comparison_old_vs_global_single.csv"
    payload["comparison_df"].to_csv(p, index=False)
    paths["comparison"] = p

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n\n" + payload["tie_break"] + "\n", encoding="utf-8")
    paths["definitions"] = p

    summary_json = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "data_source": payload["data_source"],
        "env_file": payload["env_file"],
        "common_start": payload["common_start"].isoformat(),
        "common_end": payload["common_end"].isoformat(),
        "fee_pct_primary": payload["fee_pct_primary"],
        "start_equity": payload["start_equity"],
        "tie_break": payload["tie_break"],
        "funnel_new": payload["funnel_new"],
        "funnel_old": payload["funnel_old"],
        "old_additive": payload["old_additive"],
        "new_additive": payload["new_additive"],
        "old_metrics": payload["old_metrics"],
        "new_metrics": payload["new_metrics"],
        "fraction_summaries": payload["fraction_summaries"],
        "segments": payload["segments"],
        "fee_stress": payload["fee_stress"],
        "research_2000_note": payload["research_2000_note"],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary_json), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary_json"] = p

    md = _render_md(payload)
    p = out_dir / "summary.md"
    p.write_text(md, encoding="utf-8")
    paths["summary_md"] = p
    return paths


def _render_md(payload: dict[str, Any]) -> str:
    cs = payload["common_start"]
    ce = payload["common_end"]
    d = payload["decision"]
    o = payload["old_additive"]
    n = payload["new_additive"]
    fr = payload["fraction_summaries"]
    seg = payload["segments"]
    r2 = payload["research_2000_note"]
    lines = [
        f"# Global Single-Position Wave-Fade — {payload['audit_version']}",
        "",
        f"## Primary decision",
        f"**{d}**",
        "",
        f"## Coverage",
        f"- Common MySQL window (DOGE+APT, 1m/15m/30m/1h/4h): `{cs}` → `{ce}`",
        f"- Source: MySQL `market_candles` only",
        "",
        f"## OLD (per-symbol max-1) vs NEW (global max-1)",
        "",
        "| Mode | Trades | Expectancy | PF | Cum additive net | MaxDD additive | Suppressed | Trades/mo |",
        "|------|--------|------------|----|------------------|----------------|------------|-----------|",
    ]
    for _, row in payload["comparison_df"].iterrows():
        lines.append(
            f"| {row['mode']} | {row['trades']} | {row['expectancy']} | {row['profit_factor']} | "
            f"{row['cumulative_additive_net']} | {row['max_drawdown_additive']} | "
            f"{row['suppressed_signals']} | {row['trades_per_month']} |"
        )
    lines += [
        "",
        "## Compounding equity (start 1000 USDT)",
        "",
        "| Fraction | End | PnL | Return% | CAGR% | MaxDD% | MaxDD USDT | Peak | Trough | Trades | WR | PF | Exp |",
        "|----------|-----|-----|---------|-------|--------|------------|------|--------|--------|----|----|-----|",
    ]
    for tag in ("25", "50", "100"):
        s = fr[tag]
        lines.append(
            f"| {tag}% | {s['end_equity']:.2f} | {s['pnl_usdt']:.2f} | {s['total_return_pct']} | "
            f"{s['cagr_pct']} | {s['max_drawdown_pct']} | {s['max_drawdown_usdt']} | "
            f"{s['peak_equity']:.2f} | {s['trough_equity_after_start']:.2f} | {s['trades']} | "
            f"{s['win_rate']} | {s['profit_factor']} | {s['expectancy']} |"
        )
    best = seg.get("best")
    worst = seg.get("worst")
    lines += [
        "",
        "## Strategy metrics (NEW)",
        f"- total signals: {payload['funnel_new'].get('total_signals')}",
        f"- executed trades: {payload['funnel_new'].get('executed_trades')}",
        f"- suppressed: {payload['funnel_new'].get('suppressed_signals')} "
        f"(rate={payload['funnel_new'].get('suppression_rate')})",
        f"- DOGE trades: {payload['new_metrics'].get('doge_trades')} · "
        f"APT trades: {payload['new_metrics'].get('apt_trades')}",
        f"- LONG: {payload['new_metrics'].get('long')} · SHORT: {payload['new_metrics'].get('short')}",
        f"- first_signal_tf: {payload['new_metrics'].get('first_signal_tf')}",
        f"- highest_tf_reached: {payload['new_metrics'].get('highest_tf_reached')}",
        f"- exit reasons: {payload['new_metrics'].get('exit_reason')}",
        f"- upgrades: {payload['new_metrics'].get('upgrade_count')} "
        f"(rate={payload['new_metrics'].get('upgrade_rate')})",
        "",
        f"## Best / worst symbol×side (NEW expectancy)",
        f"- best: {best}",
        f"- worst: {worst}",
        "",
        f"## ~2000% additive research figure",
        f"- prior DOGE+BTC combined additive cum ≈ {r2['prior_combined_doge_btc_additive_cum_approx']}",
        f"- this window OLD DOGE+APT additive cum = {r2['this_run_old_doge_apt_additive_cum']}",
        f"- this window NEW global additive cum = {r2['this_run_new_global_additive_cum']}",
        f"- verdict vs sequential global: **{r2['verdict']}**",
        "",
        f"## Tie-break",
        payload["tie_break"],
        "",
        "> Research only. No strategy re-optimization. See DEFINITIONS.md.",
        "",
    ]
    return "\n".join(lines)
