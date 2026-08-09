"""Export TP/SL grid artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"trade_results"}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in skip}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    payload["trade_results"].to_csv(out_dir / "trade_results.csv", index=False)
    paths["trade_results"] = out_dir / "trade_results.csv"

    for name, key in (
        ("tpsl_grid_combined.csv", "tpsl_grid_combined"),
        ("tpsl_grid_long.csv", "tpsl_grid_long"),
        ("tpsl_grid_short.csv", "tpsl_grid_short"),
        ("first_touch_matrix.csv", "first_touch_matrix"),
        ("mae_mfe_summary.csv", "mae_mfe_summary"),
        ("holding_time_summary.csv", "holding_time_summary"),
        ("monthly_stability.csv", "monthly_stability"),
        ("signal_strength_diagnostic.csv", "signal_strength_diagnostic"),
        ("tp_first_sensitivity_focus.csv", "tp_first_sensitivity_focus"),
    ):
        p = out_dir / name
        pd.DataFrame(payload[key]).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    best = payload.get("best_insample_diag") or {}
    lines = [
        f"# 15m Failure Fixed TP/SL Grid — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Primary: **{dec.get('primary')}**",
        "",
        f"## Long/Short: **{dec.get('long_short')}**",
        "",
        f"## Fixed SL: **{dec.get('fixed_sl')}**",
        "",
        f"Events: n={payload.get('n_events')}",
        "",
        f"Best in-sample (diagnostic only): TP={best.get('tp_pct')} / SL={best.get('sl_pct')} "
        f"mean_net={best.get('mean_net_return')} PF={best.get('profit_factor')}",
        "",
        "> Not strategy confirmation — APT in-sample candidate only.",
        "",
        "## Focus combos (COMBINED, SL_FIRST, after 0.11% fees)",
        "",
        "| TP | SL | mean_net | median_net | win | PF | maxDD | max_loss_streak |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    focus = {(0.15, 0.25), (0.20, 0.30), (0.25, 0.40), (0.30, 0.50), (0.40, 0.75), (0.50, 1.00)}
    for r in payload["tpsl_grid_combined"]:
        if (r.get("tp_pct"), r.get("sl_pct")) not in focus:
            continue
        lines.append(
            f"| {r.get('tp_pct')} | {r.get('sl_pct')} | {r.get('mean_net_return')} | "
            f"{r.get('median_net_return')} | {r.get('win_rate')} | {r.get('profit_factor')} | "
            f"{r.get('max_drawdown')} | {r.get('max_consecutive_losses')} |"
        )

    # top 5 by mean net
    top = sorted(
        payload["tpsl_grid_combined"],
        key=lambda r: (r.get("mean_net_return") or -999),
        reverse=True,
    )[:5]
    lines += [
        "",
        "## Top 5 combined by mean net (diagnostic)",
        "",
        "| TP | SL | mean_net | PF | tp_rate | sl_rate | maxDD |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in top:
        lines.append(
            f"| {r.get('tp_pct')} | {r.get('sl_pct')} | {r.get('mean_net_return')} | "
            f"{r.get('profit_factor')} | {r.get('tp_rate')} | {r.get('sl_rate')} | "
            f"{r.get('max_drawdown')} |"
        )

    lines += [
        "",
        "## Method",
        "",
        "```",
        str((payload.get("method") or {}).get("entry")),
        "",
        str((payload.get("method") or {}).get("general")),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('long_short')}\n{dec.get('fixed_sl')}\n",
        encoding="utf-8",
    )
    return paths
