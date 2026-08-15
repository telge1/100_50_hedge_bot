#!/usr/bin/env python3
"""Case studies + 27 blockers + controls + v2/v3 comparison for decisive-break v3."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.control_selection import select_control_specs
from research.regime_scanner.tem_structure_break.decisive_evaluation import (
    run_v2_then_decisive,
)
from research.regime_scanner.tem_structure_break.decisive_models import (
    DECISIVE_RULE_ID,
    DECISIVE_SEMANTICS,
    SIGNAL_VERSION_V3,
)
from research.regime_scanner.tem_structure_break.decisive_root_cause import ROOT_CAUSE_DEV_CASES
from research.regime_scanner.tem_structure_break.eval_common import (
    CoinFrameCache,
    load_blocker_specs,
    load_cycle_map,
    load_explosion_map,
    median,
    now_iso,
)
from research.regime_scanner.tem_structure_break.generalization_metrics import confusion
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

ROOT = Path(__file__).resolve().parents[2]
OUT_CASES = ROOT / "research/backtests/results/tem_decisive_break_cases_v3_20260723"
OUT_B = ROOT / "research/backtests/results/tem_decisive_break_27_blockers_v3_20260723"
OUT_C = ROOT / "research/backtests/results/tem_decisive_break_controls_v3_20260723"
OUT_CMP = ROOT / "research/backtests/results/tem_decisive_break_comparison_v3_20260723"

DEV_CASE_COINS = frozenset({"DOTUSDT", "ATOMUSDT", "LTCUSDT", "INJUSDT"})
REF_CASE_COINS = frozenset({"AAVEUSDT"})
CASE_COINS = sorted(DEV_CASE_COINS | REF_CASE_COINS)


def holdout_bucket_v3(spec) -> str:
    if spec.coin in DEV_CASE_COINS:
        return "development_case"
    if spec.coin in REF_CASE_COINS and spec.cohort == "blocker":
        return "reference_aave"
    if spec.cohort == "control":
        return "control"
    return "holdout"


def log(msg: str) -> None:
    print(msg, flush=True)


def _truth(v) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def write_events(path: Path, trade_id: str, coin: str, dec_rt) -> None:
    rows = []
    for e in dec_rt.events:
        rows.append({"trade_id": trade_id, "coin": coin, **e})
    write_csv(path, rows)


def main() -> None:
    cache = CoinFrameCache()
    cycles = load_cycle_map()
    explosions = load_explosion_map()
    blockers = load_blocker_specs()

    # ---------- case studies ----------
    OUT_CASES.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OUT_CASES / "root_cause.json", ROOT_CAUSE_DEV_CASES)
    atomic_write_json(OUT_CASES / "decisive_semantics.json", DECISIVE_SEMANTICS)
    case_specs = [s for s in blockers if s.coin in CASE_COINS]
    case_sum, case_ev, case_lv = [], [], []
    for i, spec in enumerate(case_specs, 1):
        log(f"case [{i}/{len(case_specs)}] {spec.trade_id}")
        _v2, dec, summary = run_v2_then_decisive(
            spec,
            cache,
            cycles=cycles.get(spec.trade_id, {}),
            explosion=explosions.get(spec.trade_id),
            bucket=holdout_bucket_v3(spec),
        )
        case_sum.append(summary)
        for e in dec.events:
            case_ev.append({"trade_id": spec.trade_id, "coin": spec.coin, **e})
        for h in dec.level_history:
            case_lv.append({"trade_id": spec.trade_id, "coin": spec.coin, **h})
    write_csv(OUT_CASES / "case_summary.csv", case_sum)
    write_csv(OUT_CASES / "decisive_events.csv", case_ev)
    write_csv(OUT_CASES / "level_history.csv", case_lv)
    (OUT_CASES / "REPORT.md").write_text(
        "# Decisive Break Case Studies v3\n\n"
        + "\n".join(
            f"- {r['coin']}: v2_inv={r.get('v2_final_invalidation_ts')} decisive={r.get('decisive_confirmation_ts')} "
            f"delay_h={r.get('hours_v2_to_decisive')} state={r.get('decisive_state')}"
            for r in case_sum
        ),
        encoding="utf-8",
    )

    # ---------- 27 blockers ----------
    OUT_B.mkdir(parents=True, exist_ok=True)
    b_sum, b_ev, b_lv, b_fail = [], [], [], []
    for i, spec in enumerate(blockers, 1):
        log(f"blocker [{i}/{len(blockers)}] {spec.trade_id}")
        # reuse cache; re-run is deterministic (cases already warmed)
        _v2, dec, summary = run_v2_then_decisive(
            spec,
            cache,
            cycles=cycles.get(spec.trade_id, {}),
            explosion=explosions.get(spec.trade_id),
            bucket=holdout_bucket_v3(spec),
        )
        summary["v3_holdout_bucket"] = holdout_bucket_v3(spec)
        b_sum.append(summary)
        for e in dec.events:
            b_ev.append({"trade_id": spec.trade_id, "coin": spec.coin, **e})
        for h in dec.level_history:
            b_lv.append({"trade_id": spec.trade_id, "coin": spec.coin, **h})
        if not summary.get("has_decisive_break"):
            b_fail.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "decisive_state": summary.get("decisive_state"),
                    "failure_reason": "NO_DECISIVE_LEVEL_OR_BREAK",
                    "v2_invalidated": bool(summary.get("v2_final_invalidation_ts")),
                }
            )
    write_csv(OUT_B / "per_trade_summary.csv", b_sum)
    write_csv(OUT_B / "decisive_events.csv", b_ev)
    write_csv(OUT_B / "level_history.csv", b_lv)
    write_csv(OUT_B / "failure_reasons.csv", b_fail)
    atomic_write_json(
        OUT_B / "summary.json",
        {
            "generated_at": now_iso(),
            "n": len(b_sum),
            "n_decisive": sum(1 for r in b_sum if r.get("has_decisive_break")),
            "share_decisive_before_c5": sum(1 for r in b_sum if _truth(r.get("decisive_before_cycle5")))
            / max(len(b_sum), 1),
            "signal_version_v2": SIGNAL_VERSION,
            "signal_version_v3": SIGNAL_VERSION_V3,
            "rule_id": DECISIVE_RULE_ID,
        },
    )
    (OUT_B / "REPORT.md").write_text("# Decisive Break 27 Blockers v3\n", encoding="utf-8")

    # ---------- controls ----------
    OUT_C.mkdir(parents=True, exist_ok=True)
    specs, audit = select_control_specs({s.coin for s in blockers})
    for spec in specs:
        frames = cache.get(spec.coin)
        bar = min(max(spec.start_bar, 0), len(frames.frame_5m) - 1)
        spec.entry_price = float(frames.frame_5m.iloc[bar]["close"])
    write_csv(OUT_C / "control_selection.csv", audit)
    c_sum, fp_rows = [], []
    for i, spec in enumerate(specs, 1):
        log(f"control [{i}/{len(specs)}] {spec.trade_id}")
        _v2, dec, summary = run_v2_then_decisive(spec, cache, bucket="control")
        summary["v3_holdout_bucket"] = "control"
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
                    "would_exit_have_closed_a_winner": summary.get("would_exit_have_closed_a_winner"),
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

    # ---------- comparison ----------
    OUT_CMP.mkdir(parents=True, exist_ok=True)
    for r in b_sum + c_sum:
        r["pred_v2"] = bool(r.get("v2_final_invalidation_ts"))
        r["pred_v3"] = bool(r.get("has_decisive_break"))

    holdout = [r for r in b_sum if r.get("v3_holdout_bucket") == "holdout"]
    dev = [r for r in b_sum if r.get("v3_holdout_bucket") == "development_case"]
    ref = [r for r in b_sum if r.get("v3_holdout_bucket") == "reference_aave"]
    controls = c_sum

    cm_v2 = confusion(holdout, controls, pred_key="pred_v2")
    cm_v3 = confusion(holdout, controls, pred_key="pred_v3")
    # also all27 for reference
    cm_v2_all = confusion(b_sum, controls, pred_key="pred_v2")
    cm_v3_all = confusion(b_sum, controls, pred_key="pred_v3")

    def cohort_line(rows, label):
        n = len(rows)
        return {
            "label": label,
            "n": n,
            "v2_inv_share": sum(1 for r in rows if r.get("v2_final_invalidation_ts")) / n if n else None,
            "v3_dec_share": sum(1 for r in rows if r.get("has_decisive_break")) / n if n else None,
            "v3_before_c4": sum(1 for r in rows if _truth(r.get("decisive_before_cycle4"))) / n if n else None,
            "v3_before_c5": sum(1 for r in rows if _truth(r.get("decisive_before_cycle5"))) / n if n else None,
            "v3_before_exp": sum(1 for r in rows if _truth(r.get("decisive_before_explosion"))) / n if n else None,
            "median_hours_v2_to_decisive": median([r.get("hours_v2_to_decisive") for r in rows if r.get("has_decisive_break")]),
            "median_lead_decisive_vs_c5": median(
                [r.get("lead_hours_decisive_vs_cycle5") for r in rows if r.get("has_decisive_break")]
            ),
            "n_decisive_later_than_v2": sum(1 for r in rows if r.get("decisive_later_than_v2")),
            "n_no_decisive": sum(1 for r in rows if not r.get("has_decisive_break")),
        }

    comparison = [
        cohort_line(dev, "development_DOT_ATOM_LTC_INJ"),
        cohort_line(ref, "reference_AAVE"),
        cohort_line(holdout, "holdout_blockers"),
        cohort_line(b_sum, "blockers_all_27"),
        cohort_line(controls, "controls"),
    ]
    v2_vs_v3 = []
    for r in b_sum:
        v2_vs_v3.append(
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
        )

    write_csv(OUT_CMP / "v2_vs_v3.csv", v2_vs_v3)
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
    atomic_write_json(OUT_CMP / "confusion_matrix_v2.json", {"holdout_vs_controls": cm_v2, "all27_vs_controls": cm_v2_all})
    atomic_write_json(OUT_CMP / "confusion_matrix_v3.json", {"holdout_vs_controls": cm_v3, "all27_vs_controls": cm_v3_all})
    atomic_write_json(OUT_CMP / "decisive_semantics.json", DECISIVE_SEMANTICS)

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
    report = f"""# v2 vs Decisive v3 Comparison

## Holdout confusion

### v2 invalidation
{json.dumps(cm_v2, indent=2)}

### v3 decisive
{json.dumps(cm_v3, indent=2)}

## Holdout shares
{json.dumps(h, indent=2)}

## Case studies
{json.dumps(summary['case_table'], indent=2, default=str)}

v2 semantics unchanged: true
"""
    (OUT_CMP / "REPORT.md").write_text(report, encoding="utf-8")
    log(json.dumps(summary, indent=2, default=str))
    log(f"Wrote {OUT_CASES}, {OUT_B}, {OUT_C}, {OUT_CMP}")


if __name__ == "__main__":
    main()
