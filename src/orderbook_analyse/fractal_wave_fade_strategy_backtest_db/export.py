"""Export strategy backtest artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import DEFINITIONS_DOC


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    variants = payload.get("variants") or {}

    defs = out_dir / "DEFINITIONS.md"
    defs.write_text(
        f"# DEFINITIONS — {payload.get('audit_version')}\n\n```\n{DEFINITIONS_DOC.strip()}\n```\n\n"
        f"Coverage: {payload.get('coverage_note')}\n",
        encoding="utf-8",
    )
    paths["DEFINITIONS.md"] = defs

    # Primary trade log = COMBINED primary
    primary = variants.get("PRIMARY_COMBINED") or {}
    trade_log = pd.DataFrame(primary.get("trades") or [])
    p = out_dir / "trade_log.csv"
    trade_log.to_csv(p, index=False)
    paths["trade_log.csv"] = p

    # equity optional
    eq = primary.get("equity")
    if isinstance(eq, pd.DataFrame) and not eq.empty:
        ep = out_dir / "equity_curve.csv"
        eq.to_csv(ep, index=False)
        paths["equity_curve.csv"] = ep

    # monthly
    monthly_rows = []
    for key in ("PRIMARY_DOGE", "PRIMARY_BTC"):
        monthly_rows.extend((variants.get(key) or {}).get("monthly") or [])
    pd.DataFrame(monthly_rows).to_csv(out_dir / "monthly_performance.csv", index=False)
    paths["monthly_performance.csv"] = out_dir / "monthly_performance.csv"

    # strategy comparison
    comp = []
    for label, v in variants.items():
        row = dict(v.get("summary") or {})
        row["variant"] = label
        cfg = v.get("config") or {}
        row.update({f"cfg_{k}": cfg[k] for k in cfg})
        comp.append(row)
    pd.DataFrame(comp).to_csv(out_dir / "strategy_comparison.csv", index=False)
    paths["strategy_comparison.csv"] = out_dir / "strategy_comparison.csv"

    # long short
    ls = []
    for key in ("PRIMARY_DOGE", "PRIMARY_BTC", "PRIMARY_COMBINED"):
        for r in (variants.get(key) or {}).get("by_side") or []:
            ls.append({**r, "run": key})
    pd.DataFrame(ls).to_csv(out_dir / "long_short_summary.csv", index=False)
    paths["long_short_summary.csv"] = out_dir / "long_short_summary.csv"

    # first / highest tf
    ft, ht = [], []
    for key in ("PRIMARY_DOGE", "PRIMARY_BTC", "PRIMARY_COMBINED"):
        for r in (variants.get(key) or {}).get("first_tf") or []:
            ft.append({**r, "run": key})
        for r in (variants.get(key) or {}).get("highest_tf") or []:
            ht.append({**r, "run": key})
    pd.DataFrame(ft).to_csv(out_dir / "first_signal_tf_summary.csv", index=False)
    pd.DataFrame(ht).to_csv(out_dir / "highest_tf_summary.csv", index=False)
    paths["first_signal_tf_summary.csv"] = out_dir / "first_signal_tf_summary.csv"
    paths["highest_tf_summary.csv"] = out_dir / "highest_tf_summary.csv"

    # drawdowns
    dd_rows = []
    for key in ("PRIMARY_DOGE", "PRIMARY_BTC", "PRIMARY_COMBINED"):
        for r in (variants.get(key) or {}).get("drawdowns") or []:
            dd_rows.append({**r, "run": key})
    pd.DataFrame(dd_rows).to_csv(out_dir / "drawdown_summary.csv", index=False)
    paths["drawdown_summary.csv"] = out_dir / "drawdown_summary.csv"

    # funnel
    funnel = []
    for label, v in variants.items():
        for r in v.get("funnels") or []:
            funnel.append(r)
    pd.DataFrame(funnel).to_csv(out_dir / "signal_funnel.csv", index=False)
    paths["signal_funnel.csv"] = out_dir / "signal_funnel.csv"

    # cost sensitivity
    cost = []
    for label in (
        "PRIMARY_DOGE",
        "PRIMARY_BTC",
        "PRIMARY_COMBINED",
        "FEE_013_DOGE",
        "FEE_013_BTC",
        "FEE_013_COMBINED",
        "FEE_015_DOGE",
        "FEE_015_BTC",
        "FEE_015_COMBINED",
    ):
        s = (variants.get(label) or {}).get("summary") or {}
        if s:
            cost.append(
                {
                    "variant": label,
                    "fee_pct": (variants.get(label) or {}).get("config", {}).get("fee_pct"),
                    "trades": s.get("trades"),
                    "expectancy": s.get("expectancy"),
                    "profit_factor": s.get("profit_factor"),
                    "cumulative_net": s.get("cumulative_net"),
                    "max_drawdown": s.get("max_drawdown"),
                    "win_rate": s.get("win_rate"),
                }
            )
    pd.DataFrame(cost).to_csv(out_dir / "cost_sensitivity.csv", index=False)
    paths["cost_sensitivity.csv"] = out_dir / "cost_sensitivity.csv"

    # slim summary.json (drop full trade lists / equity frames)
    slim_variants = {}
    for label, v in variants.items():
        slim_variants[label] = {
            "summary": v.get("summary"),
            "config": v.get("config"),
            "by_side": v.get("by_side"),
            "first_tf": v.get("first_tf"),
            "highest_tf": v.get("highest_tf"),
            "monthly_meta": v.get("monthly_meta"),
            "yearly": v.get("yearly"),
            "half": v.get("half"),
            "funnels": v.get("funnels"),
            "overlap": v.get("overlap"),
            "drawdowns": v.get("drawdowns"),
            "n_trades": len(v.get("trades") or []),
        }
    slim = {
        "audit_version": payload.get("audit_version"),
        "coverage_note": payload.get("coverage_note"),
        "decisions": payload.get("decisions"),
        "answers": payload.get("answers"),
        "variants": slim_variants,
    }
    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(slim), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    b = ans.get("B") or {}
    lines = [
        f"# Wave-Fade Full Strategy Backtest — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"- P5A: **{dec.get('p5a')}**",
        f"- Conflict: **{dec.get('conflict')}**",
        f"- Tier: **{dec.get('tier')}**",
        f"- 4h: **{dec.get('four_h')}**",
        "",
        f"Coverage: {payload.get('coverage_note')}",
        "",
        "## Answers A–K",
        f"- **A**: positive@0.11%? `{ans.get('A', {}).get('answer')}` → {ans.get('A', {}).get('decision')}",
        f"- **B DOGE**: exp={b.get('DOGE', {}).get('expectancy')} PF={b.get('DOGE', {}).get('PF')} "
        f"maxDD={b.get('DOGE', {}).get('maxDD')} t/mo={b.get('DOGE', {}).get('trades_per_month')}",
        f"- **B BTC**: exp={b.get('BTC', {}).get('expectancy')} PF={b.get('BTC', {}).get('PF')} "
        f"maxDD={b.get('BTC', {}).get('maxDD')} t/mo={b.get('BTC', {}).get('trades_per_month')}",
        f"- **B COMBINED**: exp={b.get('COMBINED', {}).get('expectancy')} PF={b.get('COMBINED', {}).get('PF')} "
        f"maxDD={b.get('COMBINED', {}).get('maxDD')}",
        f"- **C**: DOGE `{ans.get('C', {}).get('doge_positive')}` / BTC `{ans.get('C', {}).get('btc_positive')}`",
        f"- **E**: {ans.get('E', {}).get('decision')}",
        f"- **F**: {ans.get('F', {}).get('decision')}",
        f"- **G**: {ans.get('G', {}).get('decision')}",
        f"- **H**: {ans.get('H', {}).get('decision')}",
        f"- **K**: edge@0.13%? `{ans.get('K', {}).get('fee_013_combined_positive')}`",
        "",
        "> Research only. MySQL SoT. Not a live strategy confirmation.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = out_dir / "summary.md"
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('p5a')}\n{dec.get('conflict')}\n{dec.get('tier')}\n{dec.get('four_h')}\n",
        encoding="utf-8",
    )
    return paths
