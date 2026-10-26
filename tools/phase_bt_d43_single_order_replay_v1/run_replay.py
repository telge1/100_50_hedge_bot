#!/usr/bin/env python3
"""BT-D43 single-order causal replay driver."""

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
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d43_single_order_replay_v1"

for p in (
    ROOT / "tools/phase_bt_d43_single_order_replay_v1",
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
):
    sys.path.insert(0, str(p))
from fill_chronology_v1 import activation_contract_note  # noqa: E402
from single_order_replay_v1 import (  # noqa: E402
    CONTRACT,
    branch_record_from_timeline,
    compare_baseline_counts,
    replay_from_d41_events,
)

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
    events = list(csv.DictReader((D41 / "lifecycle_events.csv").open(encoding="utf-8")))
    timelines = list(csv.DictReader((D42 / "order_timelines.csv").open(encoding="utf-8")))
    touch_all = list(csv.DictReader((D42 / "touch_evidence.csv").open(encoding="utf-8")))
    timelines_by_order = {r["order_id"]: r for r in timelines}

    ctx = replay_from_d41_events(events, timelines_by_order)

    branch_rows = [branch_record_from_timeline(t) for t in timelines]
    touch_by_order: list[dict[str, Any]] = []
    per_order_touch = defaultdict(list)
    for t in touch_all:
        per_order_touch[t["order_id"]].append(t)
    for oid, touches in sorted(per_order_touch.items()):
        touch_by_order.append(
            {
                "order_id": oid,
                "touch_interval_count": len(touches),
                "first_touch_bar_5m": touches[0]["bar_utc"] if touches else "",
                "bars": "|".join(x["bar_utc"] for x in touches),
            }
        )

    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    d41_counts = defaultdict(int)
    for e in events:
        d41_counts[e["event_type"]] += 1
    d42_status = defaultdict(int)
    for t in timelines:
        d42_status[t["chronology_status"]] += 1

    counts = compare_baseline_counts(
        d26_setups=len(setups_doc["setups"]),
        d41=dict(d41_counts),
        d42={
            "touch_rows": len(touch_all),
            "EXECUTION_POSSIBLE": d42_status.get("EXECUTION_POSSIBLE", 0),
            "EXECUTION_UNRESOLVED": d42_status.get("EXECUTION_UNRESOLVED", 0),
            "CANCELLED_BEFORE_TOUCH": d42_status.get("CANCELLED_BEFORE_TOUCH", 0),
        },
        distinct_order_ids=len(timelines),
        lineage_rows=len(ctx.lineage),
    )

    ref_oid = next(t["order_id"] for t in timelines if t["decision_close_utc"] == REF_1535)
    ref_transitions = [r for r in ctx.transitions if r.get("decision_close_utc") == REF_1535 or r.get("active_order_id") == ref_oid][:20]
    ref_1535 = {
        "reference_decision_close_utc": REF_1535,
        "order_id": ref_oid,
        "activation_contract": activation_contract_note(),
        "bar_T_plus_5m_semantics": (
            "SSOT key T+5m labels the 5m candle whose OHLC is evaluated at that decision close "
            "(period ending at that timestamp). Activation uses bar_utc >= pending_active_from; "
            "no execution in originating decision candle."
        ),
        "timeline_row": timelines_by_order[ref_oid],
        "touch_intervals": per_order_touch.get(ref_oid, []),
        "replay_transitions_subset": ref_transitions,
        "replacement_at_15_40": {
            "note": "15:40 high 0.08671 < entry 0.08681 — CANCELLED_BEFORE_TOUCH; replace effective same close.",
            "chronology": timelines_by_order[ref_oid]["chronology_status"],
        },
    }
    if (D42 / "reference_1535.json").exists():
        ref_1535["bt_d42_reference"] = json.loads((D42 / "reference_1535.json").read_text(encoding="utf-8"))

    hyp_branch_count = sum(1 for b in branch_rows if b["branch_2_hypothetical_fill_blocks_new_pending"])
    unresolved_branch_count = sum(
        1 for b in branch_rows if b["chronology_status"] == "EXECUTION_UNRESOLVED"
    )

    summary = f"""# BT-D43 single-order causal replay

Contract: `{CONTRACT}`

## Single-order invariant
- `max_simultaneous_pending` observed: **{ctx.max_simultaneous_pending}** (must be ≤ 1)

## Count comparison (different denominators — do not merge)
{json.dumps(counts, indent=2)}

## Branch accounting
- Orders with hypothetical-fill branch blocking new pending: **{hyp_branch_count}**
- `EXECUTION_UNRESOLVED` segment count: **{unresolved_branch_count}**
- Independent completed trade count: **NOT_DETERMINABLE** (no SL/TP exit model in BT-D43)

## Activation
{activation_contract_note()}

## Reference 15:35
See `reference_1535.json`.
"""

    write_csv(OUT / "state_transitions.csv", ctx.transitions)
    write_csv(OUT / "order_lineage.csv", ctx.lineage)
    write_csv(OUT / "touch_by_order.csv", touch_by_order)
    write_csv(OUT / "branch_outcomes.csv", branch_rows)
    (OUT / "reference_1535.json").write_text(json.dumps(ref_1535, indent=2, default=str), encoding="utf-8")
    (OUT / "summary.md").write_text(summary, encoding="utf-8")

    print(
        json.dumps(
            {
                "transitions": len(ctx.transitions),
                "lineage": len(ctx.lineage),
                "max_pending": ctx.max_simultaneous_pending,
                "hyp_branch": hyp_branch_count,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
