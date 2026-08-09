"""Write cashout+reimbursement artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis import DEFINITIONS_DOC


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
        if df is None or df.empty:
            pd.DataFrame().to_csv(p, index=False)
        else:
            df.to_csv(p, index=False)
        paths[name] = p

    wdf("cashout_reimbursement_matrix.csv", payload["matrix"])
    wdf("equity_paths.csv", payload["equity_paths"])
    wdf("loss_reimbursements.csv", payload["loss_reimbursements"])
    wdf("worst_10_sl_streak_detail.csv", payload["worst_10_sl_streak_detail"])
    wdf("reserve_depletion_statistics.csv", payload["reserve_depletion_statistics"])
    wdf(
        "comparison_cashout_only_vs_reimbursement.csv",
        payload["comparison_cashout_only_vs_reimbursement"],
    )
    wdf("worst_sl_streak_impact.csv", payload["worst_sl_streak_impact"])

    (out_dir / "DEFINITIONS.md").write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["DEFINITIONS.md"] = out_dir / "DEFINITIONS.md"

    # primary 100% coverage slice for summary
    m = payload["matrix"]
    primary = m[(m["coverage_rate_pct"] == 100) & (m["reimburse_mode"] == "ALL_NEGATIVE")]

    summary_obj = {
        "audit_version": payload["audit_version"],
        "controls": payload["controls"],
        "parity_0pct": payload["parity_0pct"],
        "primary_100_coverage": primary.to_dict(orient="records"),
        "worst_sl_streak_impact": payload["worst_sl_streak_impact"].to_dict(orient="records"),
        "comparison": payload["comparison_cashout_only_vs_reimbursement"].to_dict(orient="records"),
        "interpretation": payload["interpretation"],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary_obj), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary.json"] = p

    p = out_dir / "summary.md"
    p.write_text(_md(payload, primary), encoding="utf-8")
    paths["summary.md"] = p
    return paths


def _md(payload: dict[str, Any], primary: pd.DataFrame) -> str:
    c = payload["controls"]
    w = c["worst_sl_streak"]
    lines = [
        f"# Cashout + Reimbursement — {payload['audit_version']}",
        "",
        f"Trades **{c['n_trades']}** · TP {c['tp']} · SL {c['sl']}",
        f"max consecutive SL **{c['max_consecutive_sl']}** · "
        f"max losing **{c['max_consecutive_losing_trades']}**",
        f"0% parity: **{payload['parity_0pct']}**",
        "",
        f"Worst SL streak: {w['start_time']} → {w['end_time']} · n={w['length']} · "
        f"sum_net={w['sum_net_return_pct']}",
        "",
        "## Primary matrix (ALL_NEGATIVE, 100% coverage)",
        "",
        "| Cashout | End Active | Reserve | Total | Active DD% | Total DD% | Fully covered | Partial | Reserve→0 |",
        "|---------|------------|---------|-------|------------|-----------|---------------|---------|-----------|",
    ]
    for _, r in primary.sort_values("cashout_rate_pct").iterrows():
        lines.append(
            f"| {int(r['cashout_rate_pct'])}% | {r['end_active']:.4g} | {r['end_reserve']:.4g} | "
            f"{r['end_total_wealth']:.4g} | {r['active_max_dd_pct']:.2f} | {r['total_max_dd_pct']:.2f} | "
            f"{r['fully_reimbursed']} | {r['partially_reimbursed']} | {r['reserve_hit_zero_events']} |"
        )
    lines += [
        "",
        "## Interpretation",
        payload["interpretation"],
        "",
        "> No strategy change. See DEFINITIONS.md.",
        "",
    ]
    return "\n".join(lines)
