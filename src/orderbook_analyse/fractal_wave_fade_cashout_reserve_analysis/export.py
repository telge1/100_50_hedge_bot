"""Write cashout/reserve analysis artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        v = float(x)
        return None if v != v else v
    if hasattr(x, "item"):
        try:
            return _jsonable(x.item())
        except Exception:
            pass
    if isinstance(x, float) and x != x:
        return None
    return x


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def wdf(name: str, df: pd.DataFrame | None) -> None:
        p = out_dir / name
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            pd.DataFrame().to_csv(p, index=False)
        else:
            df.to_csv(p, index=False)
        paths[name] = p

    wdf("cashout_comparison.csv", payload["comparison"])
    wdf("equity_paths.csv", payload["equity_paths"])
    wdf("drawdown_comparison.csv", payload["drawdown_comparison"])
    wdf("sl_streaks.csv", payload["sl_streaks"])
    wdf("losing_streaks.csv", payload["losing_streaks"])
    wdf("streak_distribution.csv", payload["streak_distribution"])
    wdf("worst_trade_blocks.csv", payload["worst_trade_blocks"])
    wdf("worst_sl_streak_cashout_impact.csv", payload["worst_sl_streak_cashout_impact"])

    (out_dir / "DEFINITIONS.md").write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["DEFINITIONS.md"] = out_dir / "DEFINITIONS.md"

    summary_obj = {
        "audit_version": payload["audit_version"],
        "controls": payload["controls"],
        "parity_0pct_vs_equity_after_100": payload["parity_0pct_vs_equity_after_100"],
        "max_consecutive_sl": payload["sl_streak"]["max_length"],
        "worst_sl_streak": payload["sl_streak"]["worst"],
        "max_consecutive_losing_trades": payload["losing_streak"]["max_length"],
        "worst_losing_streak": payload["losing_streak"]["worst"],
        "sl_by_symbol": payload["sl_by_symbol"],
        "sl_by_side": payload["sl_by_side"],
        "sl_by_tf": payload["sl_by_tf"],
        "comparison": payload["comparison"].to_dict(orient="records"),
        "worst_sl_streak_cashout_impact": payload["worst_sl_streak_cashout_impact"].to_dict(
            orient="records"
        )
        if payload["worst_sl_streak_cashout_impact"] is not None
        and not payload["worst_sl_streak_cashout_impact"].empty
        else [],
        "interpretation": payload["interpretation"],
    }
    # strip huge trade_ids list duplication in json if needed — keep it for clarity
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary_obj), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary.json"] = p

    p = out_dir / "summary.md"
    p.write_text(_md(payload), encoding="utf-8")
    paths["summary.md"] = p
    return paths


def _md(p: dict[str, Any]) -> str:
    c = p["controls"]
    w = p["sl_streak"]["worst"] or {}
    lines = [
        f"# Cashout / Reserve Analysis — {p['audit_version']}",
        "",
        f"Trades: **{c['n_trades']}** · TP **{c['tp']}** · SL **{c['sl']}**",
        f"Expectancy **{c['expectancy']:.6f}** · PF **{c['profit_factor']:.6f}** · "
        f"additive MaxDD **{c['max_drawdown_additive']:.4f}**",
        f"0% parity vs equity_after_100: **{p['parity_0pct_vs_equity_after_100']}**",
        "",
        f"## Streaks",
        f"- max consecutive SL: **{p['sl_streak']['max_length']}**",
        f"- max consecutive losing trades: **{p['losing_streak']['max_length']}**",
        f"- worst SL streak: {w.get('start_time')} → {w.get('end_time')} · "
        f"n={w.get('length')} · sum_net={w.get('sum_net_return_pct')}",
        f"- SL by symbol: {p['sl_by_symbol']}",
        f"- SL by side: {p['sl_by_side']}",
        f"- SL by first_tf: {p['sl_by_tf']}",
        "",
        "## Cashout comparison",
        "",
        "| Rate | End Active | Reserve | Total | Active MaxDD% | Total MaxDD% | Covers DD |",
        "|------|------------|---------|-------|---------------|--------------|-----------|",
    ]
    for _, r in p["comparison"].iterrows():
        lines.append(
            f"| {int(r['cashout_rate_pct'])}% | {r['end_active']:.2f} | {r['end_reserve']:.2f} | "
            f"{r['end_total_wealth']:.2f} | {r['active_max_dd_pct']:.2f} | "
            f"{r['total_max_dd_pct']:.2f} | {r['RESERVE_COVERS_MAX_DD']} |"
        )
    lines += [
        "",
        "## Interpretation",
        p["interpretation"],
        "",
        "> No re-optimization. Capital-path analysis only. See DEFINITIONS.md.",
        "",
    ]
    return "\n".join(lines)
