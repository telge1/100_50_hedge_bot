#!/usr/bin/env python3
"""Combine blocker + control results into generalization report (frozen v2)."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.eval_common import (
    AAVE_DEV_TRADE_ID,
    csv_dicts,
    now_iso,
    write_semantics_snapshot,
)
from research.regime_scanner.tem_structure_break.frozen_v2 import FROZEN_RULE_ID, frozen_semantics_public
from research.regime_scanner.tem_structure_break.generalization_metrics import (
    build_comparison,
    confusion,
    failure_mode_summary,
    lead_time_distribution,
    split_rows,
)
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BLOCKERS = ROOT / "research/backtests/results/tem_structure_break_27_blockers_v2_20260723"
DEFAULT_CONTROLS = ROOT / "research/backtests/results/tem_structure_break_controls_v2_20260723"
DEFAULT_OUT = ROOT / "research/backtests/results/tem_structure_break_generalization_v2_20260723"
MONITOR_PATH = ROOT / "research/regime_scanner/tem_structure_break/monitor.py"


FORBIDDEN_PATTERNS = [
    ("AAVEUSDT", re.compile(r"AAVEUSDT")),
    ("trade_0006", re.compile(r"continuous\|0006|\"0006\"|'0006'")),
    ("date_2026-01-19", re.compile(r"2026-01-19")),
    ("level_170.86", re.compile(r"170\.86")),
]


def overfitting_checks() -> dict:
    text = MONITOR_PATH.read_text(encoding="utf-8")
    hardcoding = {}
    for name, pat in FORBIDDEN_PATTERNS:
        hardcoding[name] = bool(pat.search(text))
    # AST: no branches on coin/trade_id literals
    tree = ast.parse(text)
    coin_branches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    if "USDT" in comp.value or "continuous|" in comp.value:
                        coin_branches.append(comp.value)
    return {
        "monitor_path": str(MONITOR_PATH),
        "hardcoding_hits": hardcoding,
        "any_hardcoding": any(hardcoding.values()),
        "coin_or_trade_literal_compares": coin_branches,
        "causality_notes": {
            "htf_lookup": "last closed HTF with close_decision <= decision_time",
            "reclaim_window": "next completed 4h only",
            "no_future_cycle_features_in_signal": True,
            "no_outcome_features_in_signal": True,
        },
        "holdout": {
            "development_trade_id": AAVE_DEV_TRADE_ID,
            "evaluation_blockers": "remaining_26",
            "controls": "scanner_blind_selection",
        },
        "frozen_rule_id": FROZEN_RULE_ID,
        "signal_version": SIGNAL_VERSION,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blockers-dir", type=Path, default=DEFAULT_BLOCKERS)
    parser.add_argument("--controls-dir", type=Path, default=DEFAULT_CONTROLS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(out)

    blockers = csv_dicts(args.blockers_dir / "per_trade_summary.csv")
    controls = csv_dicts(args.controls_dir / "per_trade_summary.csv")
    # normalize bool-ish
    for r in blockers + controls:
        for k, v in list(r.items()):
            if v == "":
                r[k] = None

    rows = blockers + controls
    parts = split_rows(rows)
    comparison = build_comparison(parts)
    # prediction = final invalidation present
    for r in rows:
        r["pred_invalidated"] = bool(r.get("final_invalidation_ts"))
        r["pred_at_risk"] = bool(r.get("first_structure_at_risk_ts"))
        r["pred_warning"] = bool(r.get("first_warning_ts"))

    cm_inv = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_invalidated")
    cm_inv_all = confusion(parts["blockers_all"], parts["controls"], pred_key="pred_invalidated")
    cm_risk = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_at_risk")
    cm_warn = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_warning")

    write_csv(out / "comparison.csv", comparison)
    write_csv(out / "lead_time_distribution.csv", lead_time_distribution(rows))
    write_csv(out / "failure_mode_summary.csv", failure_mode_summary(parts["blockers_holdout26"]))
    atomic_write_json(
        out / "confusion_matrix.json",
        {
            "invalidation_holdout26_vs_controls": cm_inv,
            "invalidation_all27_vs_controls": cm_inv_all,
            "at_risk_holdout26_vs_controls": cm_risk,
            "warning_holdout26_vs_controls": cm_warn,
        },
    )
    checks = overfitting_checks()
    atomic_write_json(out / "overfitting_checks.json", checks)
    atomic_write_json(out / "frozen_v2_semantics.json", frozen_semantics_public())

    h = next(c for c in comparison if c["label"] == "blockers_holdout_26")
    c = next(x for x in comparison if x["label"] == "controls_profitable")
    aave = parts["aave_dev"][0] if parts["aave_dev"] else {}

    summary = {
        "generated_at": now_iso(),
        "frozen_rule_id": FROZEN_RULE_ID,
        "signal_version": SIGNAL_VERSION,
        "aave_dev": {
            "trade_id": AAVE_DEV_TRADE_ID,
            "invalidated_ts": aave.get("final_invalidation_ts"),
            "final_state": aave.get("final_state"),
            "lead_inv_vs_c5": aave.get("lead_hours_invalidation_vs_cycle5"),
        },
        "holdout26": h,
        "controls": c,
        "confusion_invalidation_holdout26": cm_inv,
        "overfitting_any_hardcoding": checks["any_hardcoding"],
    }
    atomic_write_json(out / "summary.json", summary)

    verdict_generalizes = (
        (h.get("share_invalidated_before_cycle5") or 0) >= 0.4
        and (cm_inv.get("false_positive_rate") or 1) <= 0.5
        and not checks["any_hardcoding"]
    )
    report = f"""# TEM Structure Break — Generalization (frozen v2)

Generated: `{summary['generated_at']}`
Rule frozen: `{FROZEN_RULE_ID}`

## Holdout protocol

- Development: `{AAVE_DEV_TRADE_ID}` (not counted as independent proof)
- Evaluation blockers: remaining 26
- Controls: scanner-blind profitable TEM flats

## AAVE development (separate)

- Invalidation: `{aave.get('final_invalidation_ts')}`
- State: `{aave.get('final_state')}`
- Lead inv vs C5 (h): `{aave.get('lead_hours_invalidation_vs_cycle5')}`

## Holdout 26 blockers

- n: `{h.get('n')}`
- invalidated: `{h.get('share_invalidated')}`
- before C4: `{h.get('share_invalidated_before_c4')}`
- before C5: `{h.get('share_invalidated_before_c5')}`
- before explosion: `{h.get('share_invalidated_before_explosion')}`
- median lead inv vs C5 (h): `{h.get('median_lead_inv_vs_c5')}`
- reclaim share: `{h.get('share_reclaim')}`
- rebreak share: `{h.get('share_rebreak')}`

## Controls

- n: `{c.get('n')}`
- invalidated (FPR proxy): `{c.get('share_invalidated')}`
- at_risk share: `{c.get('share_at_risk')}`
- reclaim share: `{c.get('share_reclaim')}`

## Confusion (invalidation, holdout26 vs controls)

- precision: `{cm_inv.get('precision')}`
- recall: `{cm_inv.get('recall')}`
- specificity: `{cm_inv.get('specificity')}`
- FPR: `{cm_inv.get('false_positive_rate')}`
- FNR: `{cm_inv.get('false_negative_rate')}`
- TP/FN/FP/TN: `{cm_inv.get('tp')}` / `{cm_inv.get('fn')}` / `{cm_inv.get('fp')}` / `{cm_inv.get('tn')}`

## Overfitting checks

- any AAVE hardcoding in monitor: `{checks['any_hardcoding']}`
- hits: `{json.dumps(checks['hardcoding_hits'])}`

## Provisional verdict

- Heuristic generalizes flag: `{verdict_generalizes}`
- Interpret with FPR and lead-time distributions; no rule changes in this phase.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
