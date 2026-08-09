"""Write double-check audit artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck import DEFINITIONS_DOC


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

    def wdf(name: str, df: pd.DataFrame) -> Path:
        p = out_dir / name
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            pd.DataFrame().to_csv(p, index=False)
        else:
            df.to_csv(p, index=False)
        paths[name] = p
        return p

    wdf("coverage_audit.csv", pd.DataFrame(payload["coverage_rows"]))
    wdf("trade_reconstruction.csv", payload["trade_reconstruction"])
    wdf("manual_trade_audit.csv", payload["manual_trade_audit"])
    wdf("upgrade_audit.csv", payload["upgrade_audit"])
    wdf("conflict_audit.csv", payload["conflict_audit"])
    wdf("timeout_audit.csv", payload["timeout_audit"])
    if payload.get("impossible_rows") is not None:
        wdf("impossible_cases.csv", payload["impossible_rows"])

    perf_path = out_dir / "performance_recomputed.json"
    perf_path.write_text(
        json.dumps(
            _jsonable(
                {
                    "recomputed": payload["performance_recomputed"],
                    "reference": payload["performance_ref"],
                    "exit_reason_counts": payload["exit_reason_counts"],
                    "equity_compound_mismatch": payload["equity_compound_mismatch"],
                    "duration": payload["duration"],
                }
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    paths["performance_recomputed.json"] = perf_path

    cr = out_dir / "code_review_findings.md"
    cr.write_text(payload["code_review_md"], encoding="utf-8")
    paths["code_review_findings.md"] = cr

    defs = out_dir / "DEFINITIONS.md"
    defs.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["DEFINITIONS.md"] = defs

    summary_obj = {
        "audit_version": payload["audit_version"],
        "primary_decision": payload["primary_decision"],
        "secondary": payload["secondary"],
        "counts": payload["counts"],
        "exit_reason_counts": payload["exit_reason_counts"],
        "performance_recomputed": payload["performance_recomputed"],
        "performance_ref": payload["performance_ref"],
        "independent_replay": payload["independent_replay"],
        "coverage": payload["coverage"],
        "bugs_found": payload["bugs_found"],
        "credible": payload["credible"],
        "duration": payload["duration"],
        "code_review_summary": payload["code_review"].get("summary"),
    }
    sj = out_dir / "summary.json"
    sj.write_text(json.dumps(_jsonable(summary_obj), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary.json"] = sj

    sm = out_dir / "summary.md"
    sm.write_text(_md(payload), encoding="utf-8")
    paths["summary.md"] = sm
    return paths


def _md(p: dict[str, Any]) -> str:
    c = p["counts"]
    perf = p["performance_recomputed"]
    ref = p["performance_ref"]
    sec = p["secondary"]
    lines = [
        f"# Double-Check — {p['audit_version']}",
        "",
        f"## Primary decision",
        f"**{p['primary_decision']}**",
        "",
        f"Credible headline metrics: **{p['credible']}**",
        "",
        "## Secondary",
    ]
    for k, v in sec.items():
        lines.append(f"- {k}: `{v}`")
    lines += [
        "",
        "## Counts",
        f"- trades checked: {c['trades_checked']}",
        f"- lookahead: {c['lookahead_violations']}",
        f"- entry ts/px mismatches: {c['entry_timestamp_mismatch_count']} / {c['entry_price_mismatch_count']}",
        f"- exit time/reason/price mismatches: {c['exit_time_mismatch_count']} / "
        f"{c['exit_reason_mismatch_count']} / {c['exit_price_mismatch_count']}",
        f"- upgrade / retroactive: {c['upgrade_violations']} / {c['retroactive_upgrade_violations']}",
        f"- overlaps / same-ts entry=exit: {c['overlapping_trade_count']} / "
        f"{c['same_timestamp_entry_eq_prev_exit']}",
        f"- fee mismatches: {c['fee_mismatch_count']}",
        f"- timezone violations: {c['timezone_violations']}",
        f"- SL_FIRST same-bar: both={c['same_bar_both_hit_count']} ok={c['correct_sl_first_count']} "
        f"viol={c['sl_first_violations']}",
        f"- false suppressions: {c['false_suppression_count']}",
        "",
        "## Exit reasons (ref recount)",
        str(p["exit_reason_counts"]),
        "",
        "## Performance",
        f"| | ref | recomputed |",
        f"|--|--|--|",
        f"| expectancy | {ref.get('expectancy')} | {perf.get('expectancy')} |",
        f"| PF | {ref.get('profit_factor')} | {perf.get('profit_factor')} |",
        f"| cum additive | {ref.get('cumulative_additive_net')} | {perf.get('cumulative_additive_net')} |",
        f"| maxDD additive | {ref.get('max_drawdown_additive')} | {perf.get('max_drawdown_additive')} |",
        "",
        f"Independent replay: `{sec['independent_replay']}` — {p['independent_replay']}",
        "",
        "## Bugs",
    ]
    for b in p["bugs_found"]:
        lines.append(f"- {b}")
    lines += [
        "",
        "## Credibility",
        (
            "Die ~6476 Trades / ~0.257% Expectancy / PF ~1.47 / ~1666% additive Cum "
            + (
                "bleiben nach dem Double-Check **glaubwürdig**."
                if p["credible"]
                else "sind nach dem Double-Check **nicht uneingeschränkt glaubwürdig** — siehe Primary Decision."
            )
        ),
        "",
        "> Audit only. No re-optimization. MySQL SoT. See DEFINITIONS.md.",
        "",
    ]
    return "\n".join(lines)
