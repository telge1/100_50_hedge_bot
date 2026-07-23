#!/usr/bin/env python3
"""Run frozen v2 structure-break monitor on profitable TEM controls (research-only)."""

from __future__ import annotations

import argparse
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
    now_iso,
    run_spec,
    summarize_trade,
    write_semantics_snapshot,
)
from research.regime_scanner.tem_structure_break.frozen_v2 import FROZEN_RULE_ID
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/tem_structure_break_controls_v2_20260723"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    write_semantics_snapshot(out)

    blocker_coins = {s.coin for s in load_blocker_specs()}
    specs, audit = select_control_specs(blocker_coins)
    cache = CoinFrameCache()

    # Fill entry prices from candle closes (outcome-blind metadata enrichment only).
    for spec in specs:
        frames = cache.get(spec.coin)
        bar = min(max(spec.start_bar, 0), len(frames.frame_5m) - 1)
        spec.entry_price = float(frames.frame_5m.iloc[bar]["close"])
        for a in audit:
            if a["trade_id"] == spec.trade_id:
                a["entry_price"] = spec.entry_price

    write_csv(out / "control_selection.csv", audit)
    atomic_write_json(out / "control_selection_rule.json", selection_manifest())

    summaries = []
    episodes = []
    state_events = []
    recovery = []

    for i, spec in enumerate(specs, 1):
        log(f"[{i}/{len(specs)}] {spec.trade_id}")
        rt, frames = run_spec(spec, cache)
        summary = summarize_trade(spec, rt, frame=frames.frame_5m, cycles={}, explosion=None)
        summaries.append(summary)
        episodes.extend(extract_episodes(spec, rt))
        for e in rt.events:
            state_events.append({"trade_id": spec.trade_id, "coin": spec.coin, **e})
        recovery.append(
            {
                "trade_id": spec.trade_id,
                "coin": spec.coin,
                "final_pnl": summary.get("final_pnl"),
                "highest_cycle": summary.get("highest_cycle"),
                "first_warning_ts": summary.get("first_warning_ts"),
                "first_break_ts": summary.get("first_break_ts"),
                "final_invalidation_ts": summary.get("final_invalidation_ts"),
                "profitable_flat_ts": summary.get("profitable_flat_ts"),
                "recovered_after_warning": summary.get("recovered_after_warning"),
                "recovered_after_break": summary.get("recovered_after_break"),
                "recovered_after_invalidation": summary.get("recovered_after_invalidation"),
                "time_from_invalidation_to_profitable_flat": summary.get(
                    "time_from_invalidation_to_profitable_flat"
                ),
                "would_freeze_have_blocked_recovery": summary.get(
                    "would_freeze_have_blocked_recovery"
                ),
                "would_exit_have_closed_a_winner": summary.get("would_exit_have_closed_a_winner"),
                "max_drawdown_after_signal_pct": summary.get("max_drawdown_after_signal_pct"),
            }
        )

    write_csv(out / "per_trade_summary.csv", summaries)
    write_csv(out / "break_episodes.csv", episodes)
    write_csv(out / "state_events.csv", state_events)
    write_csv(out / "recovery_after_signal.csv", recovery)

    n = len(summaries)
    inv = sum(1 for s in summaries if s.get("final_invalidation_ts"))
    fp_exit = sum(1 for s in summaries if s.get("would_exit_have_closed_a_winner"))
    summary = {
        "generated_at": now_iso(),
        "signal_version": SIGNAL_VERSION,
        "frozen_rule_id": FROZEN_RULE_ID,
        "selection_rule_id": SELECTION_RULE_ID,
        "n_controls": n,
        "n_invalidated": inv,
        "share_invalidated": inv / n if n else None,
        "n_would_exit_close_winner": fp_exit,
        "share_would_exit_close_winner": fp_exit / n if n else None,
        "telemetry_only": True,
    }
    atomic_write_json(out / "summary.json", summary)
    (out / "REPORT.md").write_text(
        f"""# TEM Structure Break — Profitable Controls (frozen v2)

Generated: `{summary['generated_at']}`
Selection: `{SELECTION_RULE_ID}`
Rule: `{FROZEN_RULE_ID}`

- Controls: {n}
- Invalidated by scanner: {inv} ({summary['share_invalidated']})
- Would-exit-close-winner (diagnostic): {fp_exit}

Selection is scanner-blind and documented in `control_selection_rule.json`.
""",
        encoding="utf-8",
    )
    log(json.dumps(summary, indent=2))
    log(f"Wrote {out}")


if __name__ == "__main__":
    main()
