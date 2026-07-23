#!/usr/bin/env python3
"""Run frozen v2 structure-break monitor on all 27 TEM end-blockers (research-only)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
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
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/tem_structure_break_27_blockers_v2_20260723"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--shared-cache", type=str, default="", help="unused placeholder")
    args = parser.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(out)

    specs = load_blocker_specs()
    cycles = load_cycle_map()
    explosions = load_explosion_map()
    cache = CoinFrameCache()

    summaries = []
    episodes = []
    state_events = []
    cycle_join = []
    failures = []

    for i, spec in enumerate(specs, 1):
        log(f"[{i}/{len(specs)}] {spec.trade_id}")
        rt, frames = run_spec(spec, cache)
        summary = summarize_trade(
            spec,
            rt,
            frame=frames.frame_5m,
            cycles=cycles.get(spec.trade_id, {}),
            explosion=explosions.get(spec.trade_id),
        )
        summaries.append(summary)
        episodes.extend(extract_episodes(spec, rt))
        for e in rt.events:
            state_events.append(
                {
                    "trade_id": spec.trade_id,
                    "coin": spec.coin,
                    "holdout_bucket": spec.holdout_bucket,
                    **{k: e.get(k) for k in e},
                }
            )
        cycle_join.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "cycle4_ts": summary.get("cycle4_ts"),
                "cycle5_ts": summary.get("cycle5_ts"),
                "mtm_explosion_ts": summary.get("mtm_explosion_ts"),
                "final_invalidation_ts": summary.get("final_invalidation_ts"),
                "invalidated_before_cycle4": summary.get("invalidated_before_cycle4"),
                "invalidated_before_cycle5": summary.get("invalidated_before_cycle5"),
                "invalidated_before_explosion": summary.get("invalidated_before_explosion"),
            }
        )
        failures.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "holdout_bucket": spec.holdout_bucket,
                "final_state": summary.get("final_state"),
                "failure_reason": summary.get("root_cause_if_no_signal"),
                "data_quality_flags": summary.get("data_quality_flags"),
            }
        )

    write_csv(out / "per_trade_summary.csv", summaries)
    write_csv(out / "break_episodes.csv", episodes)
    write_csv(out / "state_events.csv", state_events)
    write_csv(out / "cycle_join.csv", cycle_join)
    write_csv(out / "failure_reasons.csv", failures)

    n = len(summaries)
    inv = sum(1 for s in summaries if s.get("final_invalidation_ts"))
    inv_c5 = sum(1 for s in summaries if s.get("invalidated_before_cycle5") is True)
    inv_c4 = sum(1 for s in summaries if s.get("invalidated_before_cycle4") is True)
    holdout = [s for s in summaries if s.get("holdout_bucket") == "holdout"]
    summary = {
        "generated_at": now_iso(),
        "signal_version": SIGNAL_VERSION,
        "frozen_rule_id": FROZEN_RULE_ID,
        "n_trades": n,
        "n_holdout_26": len(holdout),
        "n_invalidated": inv,
        "n_invalidated_before_cycle4": inv_c4,
        "n_invalidated_before_cycle5": inv_c5,
        "share_invalidated": inv / n if n else None,
        "share_invalidated_before_cycle5": inv_c5 / n if n else None,
        "telemetry_only": True,
    }
    atomic_write_json(out / "summary.json", summary)

    report = f"""# TEM Structure Break — 27 Blockers (frozen v2)

Generated: `{summary['generated_at']}`
Rule: `{FROZEN_RULE_ID}` / `{SIGNAL_VERSION}`

AAVE development case is included in the 27 but must be reported separately in generalization.

## Headline

- Trades: {n}
- Invalidated: {inv} ({summary['share_invalidated']})
- Invalidated before Cycle 4: {inv_c4}
- Invalidated before Cycle 5: {inv_c5}

No rule changes during this run. Telemetry only.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    log(json.dumps(summary, indent=2))
    log(f"Wrote {out}")


if __name__ == "__main__":
    main()
