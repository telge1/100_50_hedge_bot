#!/usr/bin/env python3
"""Resume controls + comparison using existing blocker v3 summaries."""

from __future__ import annotations

import json
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.control_selection import select_control_specs
from research.regime_scanner.tem_structure_break.decisive_evaluation import run_v2_then_decisive
from research.regime_scanner.tem_structure_break.decisive_models import DECISIVE_SEMANTICS
from research.regime_scanner.tem_structure_break.eval_common import (
    CoinFrameCache,
    csv_dicts,
    load_blocker_specs,
    median,
    now_iso,
)
from research.regime_scanner.tem_structure_break.generalization_metrics import confusion

ROOT = Path(__file__).resolve().parents[2]
OUT_B = ROOT / "research/backtests/results/tem_decisive_break_27_blockers_v3_20260723"
OUT_C = ROOT / "research/backtests/results/tem_decisive_break_controls_v3_20260723"
OUT_CMP = ROOT / "research/backtests/results/tem_decisive_break_comparison_v3_20260723"
OUT_CASES = ROOT / "research/backtests/results/tem_decisive_break_cases_v3_20260723"


def _truth(v) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def log(msg: str) -> None:
    print(msg, flush=True)


def cohort_line(rows, label):
    n = len(rows)
    return {
        "label": label,
        "n": n,
        "v2_inv_share": sum(1 for r in rows if r.get("v2_final_invalidation_ts")) / n if n else None,
        "v3_dec_share": sum(1 for r in rows if _truth(r.get("has_decisive_break"))) / n if n else None,
        "v3_before_c4": sum(1 for r in rows if _truth(r.get("decisive_before_cycle4"))) / n if n else None,
        "v3_before_c5": sum(1 for r in rows if _truth(r.get("decisive_before_cycle5"))) / n if n else None,
        "v3_before_exp": sum(1 for r in rows if _truth(r.get("decisive_before_explosion"))) / n if n else None,
        "median_hours_v2_to_decisive": median(
            [r.get("hours_v2_to_decisive") for r in rows if _truth(r.get("has_decisive_break"))]
        ),
        "median_lead_decisive_vs_c5": median(
            [r.get("lead_hours_decisive_vs_cycle5") for r in rows if _truth(r.get("has_decisive_break"))]
        ),
        "n_decisive_later_than_v2": sum(1 for r in rows if _truth(r.get("decisive_later_than_v2"))),
        "n_no_decisive": sum(1 for r in rows if not _truth(r.get("has_decisive_break"))),
    }


def main() -> None:
    cache = CoinFrameCache()
    blockers = load_blocker_specs()
    b_sum = csv_dicts(OUT_B / "per_trade_summary.csv")
    for r in b_sum:
        if r.get("has_decisive_break") in ("True", "true", "1"):
            r["has_decisive_break"] = True
        elif r.get("has_decisive_break") in ("False", "false", "0", ""):
            r["has_decisive_break"] = False

    specs, audit = select_control_specs({s.coin for s in blockers})
    for spec in specs:
        frames = cache.get(spec.coin)
        bar = min(max(spec.start_bar, 0), len(frames.frame_5m) - 1)
        spec.entry_price = float(frames.frame_5m.iloc[bar]["close"])
    OUT_C.mkdir(parents=True, exist_ok=True)
    write_csv(OUT_C / "control_selection.csv", audit)

    c_sum, fp_rows = [], []
    for i, spec in enumerate(specs, 1):
        log(f"control [{i}/{len(specs)}] {spec.trade_id}")
        _v2, dec, summary = run_v2_then_decisive(spec, cache, bucket="control")
        c_sum.append(summary)
        if summary.get("has_decisive_break"):
            fp_rows.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "final_pnl": summary.get("final_pnl"),
                    "highest_cycle": summary.get("highest_cycle"),
                    "v2_final_invalidation_ts": summary.get("v2_final_invalidation_ts"),
                    "decisive_confirmation_ts": summary.get("decisive_confirmation_ts"),
                }
            )
    write_csv(OUT_C / "per_trade_summary.csv", c_sum)
    write_csv(OUT_C / "winner_false_positives.csv", fp_rows)
    atomic_write_json(
        OUT_C / "summary.json",
        {
            "generated_at": now_iso(),
            "n": len(c_sum),
            "n_v2_invalidated": sum(1 for r in c_sum if r.get("v2_final_invalidation_ts")),
            "n_decisive": sum(1 for r in c_sum if r.get("has_decisive_break")),
            "fpr_v2": sum(1 for r in c_sum if r.get("v2_final_invalidation_ts")) / max(len(c_sum), 1),
            "fpr_v3": sum(1 for r in c_sum if r.get("has_decisive_break")) / max(len(c_sum), 1),
        },
    )
    (OUT_C / "REPORT.md").write_text("# Decisive Break Controls v3\n", encoding="utf-8")

    for r in b_sum + c_sum:
        r["pred_v2"] = bool(r.get("v2_final_invalidation_ts"))
        r["pred_v3"] = bool(r.get("has_decisive_break"))

    holdout = [r for r in b_sum if r.get("v3_holdout_bucket") == "holdout"]
    dev = [r for r in b_sum if r.get("v3_holdout_bucket") == "development_case"]
    ref = [r for r in b_sum if r.get("v3_holdout_bucket") == "reference_aave"]
    controls = c_sum
    cm_v2 = confusion(holdout, controls, pred_key="pred_v2")
    cm_v3 = confusion(holdout, controls, pred_key="pred_v3")

    comparison = [
        cohort_line(dev, "development_DOT_ATOM_LTC_INJ"),
        cohort_line(ref, "reference_AAVE"),
        cohort_line(holdout, "holdout_blockers"),
        cohort_line(b_sum, "blockers_all_27"),
        cohort_line(controls, "controls"),
    ]
    OUT_CMP.mkdir(parents=True, exist_ok=True)
    write_csv(
        OUT_CMP / "v2_vs_v3.csv",
        [
            {
                "coin": r["coin"],
                "trade_id": r["trade_id"],
                "bucket": r.get("v3_holdout_bucket"),
                "v2_final_invalidation_ts": r.get("v2_final_invalidation_ts"),
                "decisive_confirmation_ts": r.get("decisive_confirmation_ts"),
                "hours_v2_to_decisive": r.get("hours_v2_to_decisive"),
                "decisive_later_than_v2": r.get("decisive_later_than_v2"),
                "decisive_before_cycle4": r.get("decisive_before_cycle4"),
                "decisive_before_cycle5": r.get("decisive_before_cycle5"),
                "decisive_before_explosion": r.get("decisive_before_explosion"),
                "lead_hours_decisive_vs_cycle5": r.get("lead_hours_decisive_vs_cycle5"),
                "decisive_level_type": r.get("decisive_level_type"),
                "decisive_reason": r.get("decisive_reason"),
            }
            for r in b_sum
        ],
    )
    write_csv(OUT_CMP / "development_vs_holdout.csv", comparison)
    write_csv(
        OUT_CMP / "lead_time_comparison.csv",
        [
            {
                "trade_id": r["trade_id"],
                "bucket": r.get("v3_holdout_bucket"),
                "lead_v2_vs_c5": r.get("lead_hours_invalidation_vs_cycle5"),
                "lead_v3_vs_c5": r.get("lead_hours_decisive_vs_cycle5"),
                "hours_v2_to_decisive": r.get("hours_v2_to_decisive"),
            }
            for r in b_sum
        ],
    )
    atomic_write_json(OUT_CMP / "confusion_matrix_v2.json", {"holdout_vs_controls": cm_v2})
    atomic_write_json(OUT_CMP / "confusion_matrix_v3.json", {"holdout_vs_controls": cm_v3})
    atomic_write_json(OUT_CMP / "decisive_semantics.json", DECISIVE_SEMANTICS)
    case_sum = csv_dicts(OUT_CASES / "case_summary.csv") if (OUT_CASES / "case_summary.csv").exists() else []
    h = next(x for x in comparison if x["label"] == "holdout_blockers")
    summary = {
        "generated_at": now_iso(),
        "v2_unchanged": True,
        "confusion_v2_holdout": cm_v2,
        "confusion_v3_holdout": cm_v3,
        "holdout": h,
        "development": next(x for x in comparison if x["label"] == "development_DOT_ATOM_LTC_INJ"),
        "controls": next(x for x in comparison if x["label"] == "controls"),
        "case_table": [
            {
                "coin": r["coin"],
                "v2_inv": r.get("v2_final_invalidation_ts"),
                "decisive": r.get("decisive_confirmation_ts"),
                "delay_h": r.get("hours_v2_to_decisive"),
                "level_type": r.get("decisive_level_type"),
                "before_c5": r.get("decisive_before_cycle5"),
            }
            for r in case_sum
        ],
    }
    atomic_write_json(OUT_CMP / "summary.json", summary)
    (OUT_CMP / "REPORT.md").write_text(
        f"# v2 vs Decisive v3\n\n```json\n{json.dumps(summary, indent=2, default=str)}\n```\n",
        encoding="utf-8",
    )
    log(json.dumps(summary, indent=2, default=str))
    log(f"Wrote {OUT_C}, {OUT_CMP}")


if __name__ == "__main__":
    main()
