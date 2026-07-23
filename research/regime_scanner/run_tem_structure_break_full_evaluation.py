#!/usr/bin/env python3
"""In-process full evaluation with one shared CoinFrameCache (research-only)."""

from __future__ import annotations

import json
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.control_selection import (
    SELECTION_RULE_ID,
    select_control_specs,
    selection_manifest,
)
from research.regime_scanner.tem_structure_break.eval_common import (
    CoinFrameCache,
    extract_episodes,
    load_blocker_specs,
    load_cycle_map,
    load_explosion_map,
    now_iso,
    run_spec,
    summarize_trade,
    write_semantics_snapshot,
)
from research.regime_scanner.tem_structure_break.frozen_v2 import FROZEN_RULE_ID
from research.regime_scanner.tem_structure_break.generalization_metrics import (
    build_comparison,
    confusion,
    failure_mode_summary,
    lead_time_distribution,
    split_rows,
)
from research.regime_scanner.run_tem_structure_break_generalization import overfitting_checks
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

ROOT = Path(__file__).resolve().parents[2]
OUT_B = ROOT / "research/backtests/results/tem_structure_break_27_blockers_v2_20260723"
OUT_C = ROOT / "research/backtests/results/tem_structure_break_controls_v2_20260723"
OUT_G = ROOT / "research/backtests/results/tem_structure_break_generalization_v2_20260723"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    cache = CoinFrameCache()
    cycles = load_cycle_map()
    explosions = load_explosion_map()

    # --- blockers ---
    OUT_B.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(OUT_B)
    blocker_specs = load_blocker_specs()
    b_sum, b_ep, b_ev, b_cj, b_fail = [], [], [], [], []
    for i, spec in enumerate(blocker_specs, 1):
        log(f"blocker [{i}/{len(blocker_specs)}] {spec.trade_id}")
        rt, frames = run_spec(spec, cache)
        s = summarize_trade(
            spec,
            rt,
            frame=frames.frame_5m,
            cycles=cycles.get(spec.trade_id, {}),
            explosion=explosions.get(spec.trade_id),
        )
        b_sum.append(s)
        b_ep.extend(extract_episodes(spec, rt))
        for e in rt.events:
            b_ev.append({"trade_id": spec.trade_id, "coin": spec.coin, "holdout_bucket": spec.holdout_bucket, **e})
        b_cj.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "cycle4_ts": s.get("cycle4_ts"),
                "cycle5_ts": s.get("cycle5_ts"),
                "mtm_explosion_ts": s.get("mtm_explosion_ts"),
                "final_invalidation_ts": s.get("final_invalidation_ts"),
                "invalidated_before_cycle4": s.get("invalidated_before_cycle4"),
                "invalidated_before_cycle5": s.get("invalidated_before_cycle5"),
                "invalidated_before_explosion": s.get("invalidated_before_explosion"),
            }
        )
        b_fail.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "holdout_bucket": spec.holdout_bucket,
                "final_state": s.get("final_state"),
                "failure_reason": s.get("root_cause_if_no_signal"),
                "data_quality_flags": s.get("data_quality_flags"),
            }
        )
    write_csv(OUT_B / "per_trade_summary.csv", b_sum)
    write_csv(OUT_B / "break_episodes.csv", b_ep)
    write_csv(OUT_B / "state_events.csv", b_ev)
    write_csv(OUT_B / "cycle_join.csv", b_cj)
    write_csv(OUT_B / "failure_reasons.csv", b_fail)
    b_meta = {
        "generated_at": now_iso(),
        "signal_version": SIGNAL_VERSION,
        "frozen_rule_id": FROZEN_RULE_ID,
        "n_trades": len(b_sum),
        "n_invalidated": sum(1 for s in b_sum if s.get("final_invalidation_ts")),
        "n_invalidated_before_cycle5": sum(1 for s in b_sum if s.get("invalidated_before_cycle5") is True),
        "telemetry_only": True,
    }
    atomic_write_json(OUT_B / "summary.json", b_meta)
    (OUT_B / "REPORT.md").write_text(
        f"# 27 Blockers frozen v2\n\n{json.dumps(b_meta, indent=2)}\n", encoding="utf-8"
    )

    # --- controls ---
    OUT_C.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(OUT_C)
    specs, audit = select_control_specs({s.coin for s in blocker_specs})
    for spec in specs:
        frames = cache.get(spec.coin)
        bar = min(max(spec.start_bar, 0), len(frames.frame_5m) - 1)
        spec.entry_price = float(frames.frame_5m.iloc[bar]["close"])
        for a in audit:
            if a["trade_id"] == spec.trade_id:
                a["entry_price"] = spec.entry_price
    write_csv(OUT_C / "control_selection.csv", audit)
    atomic_write_json(OUT_C / "control_selection_rule.json", selection_manifest())

    c_sum, c_ep, c_ev, c_rec = [], [], [], []
    for i, spec in enumerate(specs, 1):
        log(f"control [{i}/{len(specs)}] {spec.trade_id}")
        rt, frames = run_spec(spec, cache)
        s = summarize_trade(spec, rt, frame=frames.frame_5m)
        c_sum.append(s)
        c_ep.extend(extract_episodes(spec, rt))
        for e in rt.events:
            c_ev.append({"trade_id": spec.trade_id, "coin": spec.coin, **e})
        c_rec.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "final_pnl": s.get("final_pnl"),
                "highest_cycle": s.get("highest_cycle"),
                "final_invalidation_ts": s.get("final_invalidation_ts"),
                "profitable_flat_ts": s.get("profitable_flat_ts"),
                "recovered_after_warning": s.get("recovered_after_warning"),
                "recovered_after_break": s.get("recovered_after_break"),
                "recovered_after_invalidation": s.get("recovered_after_invalidation"),
                "would_freeze_have_blocked_recovery": s.get("would_freeze_have_blocked_recovery"),
                "would_exit_have_closed_a_winner": s.get("would_exit_have_closed_a_winner"),
                "max_drawdown_after_signal_pct": s.get("max_drawdown_after_signal_pct"),
            }
        )
    write_csv(OUT_C / "per_trade_summary.csv", c_sum)
    write_csv(OUT_C / "break_episodes.csv", c_ep)
    write_csv(OUT_C / "state_events.csv", c_ev)
    write_csv(OUT_C / "recovery_after_signal.csv", c_rec)
    c_meta = {
        "generated_at": now_iso(),
        "signal_version": SIGNAL_VERSION,
        "frozen_rule_id": FROZEN_RULE_ID,
        "selection_rule_id": SELECTION_RULE_ID,
        "n_controls": len(c_sum),
        "n_invalidated": sum(1 for s in c_sum if s.get("final_invalidation_ts")),
        "n_would_exit_close_winner": sum(1 for s in c_sum if s.get("would_exit_have_closed_a_winner")),
        "telemetry_only": True,
    }
    atomic_write_json(OUT_C / "summary.json", c_meta)
    (OUT_C / "REPORT.md").write_text(
        f"# Controls frozen v2\n\n{json.dumps(c_meta, indent=2)}\n", encoding="utf-8"
    )

    # --- generalization ---
    OUT_G.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(OUT_G)
    rows = b_sum + c_sum
    for r in rows:
        r["pred_invalidated"] = bool(r.get("final_invalidation_ts"))
        r["pred_at_risk"] = bool(r.get("first_structure_at_risk_ts"))
        r["pred_warning"] = bool(r.get("first_warning_ts"))
    parts = split_rows(rows)
    comparison = build_comparison(parts)
    cm_inv = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_invalidated")
    cm_inv_all = confusion(parts["blockers_all"], parts["controls"], pred_key="pred_invalidated")
    cm_risk = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_at_risk")
    cm_warn = confusion(parts["blockers_holdout26"], parts["controls"], pred_key="pred_warning")
    write_csv(OUT_G / "comparison.csv", comparison)
    write_csv(OUT_G / "lead_time_distribution.csv", lead_time_distribution(rows))
    write_csv(OUT_G / "failure_mode_summary.csv", failure_mode_summary(parts["blockers_holdout26"]))
    atomic_write_json(
        OUT_G / "confusion_matrix.json",
        {
            "invalidation_holdout26_vs_controls": cm_inv,
            "invalidation_all27_vs_controls": cm_inv_all,
            "at_risk_holdout26_vs_controls": cm_risk,
            "warning_holdout26_vs_controls": cm_warn,
        },
    )
    checks = overfitting_checks()
    atomic_write_json(OUT_G / "overfitting_checks.json", checks)
    h = next(c for c in comparison if c["label"] == "blockers_holdout_26")
    ctl = next(x for x in comparison if x["label"] == "controls_profitable")
    aave = parts["aave_dev"][0] if parts["aave_dev"] else {}
    g_summary = {
        "generated_at": now_iso(),
        "frozen_rule_id": FROZEN_RULE_ID,
        "aave_dev": {
            "invalidated_ts": aave.get("final_invalidation_ts"),
            "lead_inv_vs_c5": aave.get("lead_hours_invalidation_vs_cycle5"),
        },
        "holdout26": h,
        "controls": ctl,
        "confusion_invalidation_holdout26": cm_inv,
        "overfitting_any_hardcoding": checks["any_hardcoding"],
    }
    atomic_write_json(OUT_G / "summary.json", g_summary)
    (OUT_G / "REPORT.md").write_text(
        f"""# Generalization frozen v2

## Holdout26
{json.dumps(h, indent=2)}

## Controls
{json.dumps(ctl, indent=2)}

## Confusion invalidation
{json.dumps(cm_inv, indent=2)}

## Overfitting
{json.dumps(checks, indent=2)}
""",
        encoding="utf-8",
    )
    log(json.dumps(g_summary, indent=2, default=str))
    log(f"Wrote {OUT_B}, {OUT_C}, {OUT_G}")


if __name__ == "__main__":
    main()
