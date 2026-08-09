"""Export walk-forward validation artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_walkforward_validation_db import DEFINITIONS_DOC


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("equity_frames", "coverage"):
                if k == "coverage":
                    # slim coverage for json
                    cov = obj["coverage"]
                    out[k] = {
                        "rows": cov.get("rows"),
                        "per_symbol": {
                            sym: {
                                "testable_start": str(ps["testable_start"]),
                                "testable_end": str(ps["testable_end"]),
                                "note": ps.get("note"),
                                "tfs": {
                                    tf: {
                                        "earliest": str(info["earliest"]),
                                        "latest": str(info["latest"]),
                                        "n": info["n"],
                                    }
                                    for tf, info in (ps.get("tfs") or {}).items()
                                },
                            }
                            for sym, ps in (cov.get("per_symbol") or {}).items()
                        },
                    }
                continue
            out[k] = _jsonable(v)
        return out
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    (out_dir / "DEFINITIONS.md").write_text(
        f"# DEFINITIONS — {payload.get('audit_version')}\n\n```\n{DEFINITIONS_DOC.strip()}\n```\n\n"
        f"DEVELOPMENT_DATA_END: `{payload.get('development_data_end')}`\n"
        f"TRUE_OOS_START: `{payload.get('true_oos_start')}`\n"
        f"TRUE_OOS_STATUS: `{payload.get('true_oos_status')}`\n"
        f"REOPT_CHECK: `{payload.get('reopt_check')}`\n",
        encoding="utf-8",
    )
    paths["DEFINITIONS.md"] = out_dir / "DEFINITIONS.md"

    # coverage.md
    cov = payload.get("coverage") or {}
    lines = [
        "# Coverage — MySQL market_candles",
        "",
        "Testable strategy window is limited by **1m execution coverage**.",
        "",
    ]
    for row in cov.get("rows") or []:
        lines.append(
            f"- **{row['symbol']} {row['timeframe']}**: {row['earliest']} → {row['latest']} (n={row['n_bars']})"
        )
    lines.append("")
    for sym, ps in (cov.get("per_symbol") or {}).items():
        lines.append(
            f"### {sym} testable: `{ps['testable_start']}` → `{ps['testable_end']}`"
        )
        lines.append(f"_{ps.get('note')}_")
        lines.append("")
    lines.append(f"DEVELOPMENT_DATA_END = `{payload.get('development_data_end')}`")
    lines.append(f"TRUE_OOS status = `{payload.get('true_oos_status')}`")
    (out_dir / "coverage.md").write_text("\n".join(lines), encoding="utf-8")
    paths["coverage.md"] = out_dir / "coverage.md"

    for name, key in (
        ("time_block_results.csv", "time_block_results"),
        ("half_split_results.csv", "half_split_results"),
        ("rolling_results.csv", "rolling_results"),
        ("long_short_stability.csv", "long_short_stability"),
        ("tf_stability.csv", "tf_stability"),
        ("p5a_stability.csv", "p5a_stability"),
        ("tier_a_stability.csv", "tier_a_stability"),
        ("cost_stability.csv", "cost_stability"),
        ("drawdown_stability.csv", "drawdown_stability"),
        ("true_oos_results.csv", "true_oos_results"),
    ):
        pd.DataFrame(payload.get(key) or []).to_csv(out_dir / name, index=False)
        paths[name] = out_dir / name

    # equity
    frames = payload.get("equity_frames") or []
    if frames:
        eq = pd.concat(frames, ignore_index=True)
        eq.to_csv(out_dir / "equity_curve.csv", index=False)
        paths["equity_curve.csv"] = out_dir / "equity_curve.csv"

    slim = {k: v for k, v in payload.items() if k != "equity_frames"}
    (out_dir / "summary.json").write_text(
        json.dumps(_jsonable(slim), indent=2, default=str), encoding="utf-8"
    )
    paths["summary.json"] = out_dir / "summary.json"

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    st = payload.get("stability_shares") or {}
    md = [
        f"# Walk-Forward Validation — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"- Walk-forward: **{dec.get('walk_forward')}**",
        f"- P5A: **{dec.get('p5a')}**",
        f"- Tier A: **{dec.get('tier')}**",
        f"- Costs: **{dec.get('costs')}**",
        f"- Reopt check: **{dec.get('reopt_check')}**",
        "",
        f"DEVELOPMENT_DATA_END: `{payload.get('development_data_end')}`",
        f"TRUE_OOS: `{payload.get('true_oos_status')}`",
        "",
        "## Stability shares (quarter blocks, n≥20)",
        f"- Exp>0: `{st.get('positive_expectancy_block_share')}`",
        f"- PF>1: `{st.get('PF_above_1_block_share')}`",
        f"- Cum net>0: `{st.get('positive_net_block_share')}`",
        "",
        "## Answers A–L",
        f"- **A**: true OOS? `{ans.get('A', {}).get('answer')}` → `{ans.get('A', {}).get('status')}`",
        f"- **B**: OOS trades DOGE/BTC/COMB = `{ans.get('B', {}).get('doge')}` / `{ans.get('B', {}).get('btc')}` / `{ans.get('B', {}).get('combined')}`",
        f"- **C**: `{ans.get('C', {}).get('decision')}`",
        f"- **D**: shares above",
        f"- **E**: DOGE stable? `{ans.get('E', {}).get('answer')}`",
        f"- **F**: BTC stable? `{ans.get('F', {}).get('answer')}`",
        f"- **G**: both sides mostly +? `{ans.get('G', {}).get('both_sides_mostly_positive')}`",
        f"- **H**: `{ans.get('H', {}).get('decision')}`",
        f"- **I**: `{ans.get('I', {}).get('decision')}`",
        f"- **J**: `{ans.get('J', {}).get('decision')}`",
        f"- **K**: worst `{ans.get('K', {}).get('worst')}`",
        f"- **L**: paper/replay? `{ans.get('L', {}).get('answer')}` — {ans.get('L', {}).get('rationale')}",
        "",
        "> Research / validation only. No strategy confirmation. See coverage.md.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    paths["summary.md"] = out_dir / "summary.md"
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('walk_forward')}\n{dec.get('p5a')}\n{dec.get('tier')}\n{dec.get('costs')}\n{dec.get('reopt_check')}\n",
        encoding="utf-8",
    )
    return paths
