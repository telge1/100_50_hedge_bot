#!/usr/bin/env python3
"""BT-D44 single-position branch replay driver."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D41 = ROOT / "results/phase_bt_d41_pending_lifecycle_v1"
D42 = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1"
D43 = ROOT / "results/phase_bt_d43_single_order_replay_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d44_single_position_branch_v1"

for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d44_single_position_branch_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from branch_replay_v1 import CONTRACT, compute_trade_counts, replay_branches  # noqa: E402
from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402

REF_1535 = "2026-09-11T15:35:00Z"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    timelines = list(csv.DictReader((D42 / "order_timelines.csv").open(encoding="utf-8")))
    lineage = list(csv.DictReader((D43 / "order_lineage.csv").open(encoding="utf-8")))
    d41_events = list(csv.DictReader((D41 / "lifecycle_events.csv").open(encoding="utf-8")))
    d41_counts = defaultdict(int)
    for e in d41_events:
        d41_counts[e["event_type"]] += 1

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    fill_model = str(config["short_entry"].get("pending_limit_fill_model") or "causal_sell_limit_v2")
    pilot_end = end

    path_a, path_b, exit_rows = replay_branches(
        timelines,
        config=config,
        prices=prices,
        fill_model=fill_model,
        pilot_end_utc=pilot_end,
        lineage=lineage,
    )

    timeline = path_a.events + path_b.events
    timeline.sort(key=lambda r: (r["decision_close_utc"], r["path"], r["seq"]))

    branch_states = []
    for tracker in (path_a, path_b):
        branch_states.append(
            {
                "path": tracker.path,
                "final_state": tracker.state,
                "event_count": len(tracker.events),
            }
        )

    counts = compute_trade_counts(timelines, path_a, path_b, exit_rows, d41_counts=dict(d41_counts))

    ref_tl = next(t for t in timelines if t["decision_close_utc"] == REF_1535)
    ref_events = [
        e
        for e in timeline
        if e.get("order_id") == ref_tl["order_id"] or e.get("decision_close_utc") in (REF_1535, "2026-09-11T15:40:00Z")
    ]
    ref_1535 = {
        "reference_decision_close_utc": REF_1535,
        "order_id": ref_tl["order_id"],
        "pool": f"{ref_tl['pool_lower']}–{ref_tl['pool_upper']}",
        "entry_sl_tp": {
            "entry": ref_tl["planned_entry"],
            "sl": ref_tl["planned_sl"],
            "tp": ref_tl["planned_tp"],
        },
        "chronology_status": ref_tl["chronology_status"],
        "replace_at": ref_tl["cancel_effective_decision_close_utc"],
        "path_a_summary": "UNFILLED_REPLACED at 15:40 — no touch before replace",
        "path_b_summary": "No hypothetical entry — CANCELLED_BEFORE_TOUCH / no causal fill",
        "replay_events": ref_events,
    }

    summary = f"""# BT-D44 single-position branch replay

Contract: `{CONTRACT}`

## Paths
- **A_UNFILLED**: no position block; all {len(timelines)} scanner pending lifecycles
- **B_HYPOTHETICAL**: single position slot; blocks new pending while `HYPOTHETICAL_POSITION_OPEN`

## Key counts
{json.dumps(counts, indent=2)}

## Coverage
- Deterministic single-path B (no exponential fork); `EXECUTION_UNRESOLVED` entries do not assume fill on B
- Equivalent-state merge: one global B tracker (position open vs flat)

See `independent_trade_counts.json`.
"""

    write_csv(OUT / "event_timeline.csv", timeline)
    write_csv(OUT / "branch_states.csv", branch_states)
    write_csv(OUT / "potential_position_exits.csv", exit_rows)
    (OUT / "independent_trade_counts.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    (OUT / "reference_1535.json").write_text(json.dumps(ref_1535, indent=2, default=str), encoding="utf-8")
    (OUT / "summary.md").write_text(summary, encoding="utf-8")

    print(json.dumps({"path_a_events": len(path_a.events), "path_b_events": len(path_b.events), "blocked_b": counts["path_b_scanner_emits_blocked_while_position_open"]}, indent=2))


if __name__ == "__main__":
    main()
