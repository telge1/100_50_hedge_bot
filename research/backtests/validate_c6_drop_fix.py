"""Validate C6-DROP starts after stale split completion fix."""

from __future__ import annotations

import json
from pathlib import Path

from research.backtests.test_dcos_stale_split_completion_fix import _run_to_c5_sr

MILD_A_CONFIG = Path("research/backtests/configs/dcos_mild_qty/variant_a.json")
C6_DROP_STARTS = [250, 3500, 4500, 4750, 5500, 6500, 7250, 7500, 8000, 8250, 9750, 23000]


def main() -> None:
    rows: list[dict] = []
    for start in C6_DROP_STARTS:
        outcome = _run_to_c5_sr(start, scaling_config_path=MILD_A_CONFIG)
        rows.append(
            {
                "start_index": start,
                "c5_sr_seen": outcome["c5_sr_seen"],
                "c6_placed": outcome["has_c6_in_intent_log"],
                "c6_on_fill_or_tick": outcome["c6_la_on_fill_or_tick"],
                "c5_complete": outcome["c5_complete"],
                "fix_applied": bool(outcome["fix_events"]),
                "next_required": outcome["next_required_purpose"],
            }
        )

    placed = sum(1 for row in rows if row["c6_placed"])
    fixed = sum(1 for row in rows if row["fix_applied"])
    out = {"starts": rows, "c6_placed_count": placed, "fix_applied_count": fixed}
    out_path = Path("research/backtests/results/c6_drop_fix_validation_12starts.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(out_path)
    print(f"C6 placed: {placed}/{len(rows)}")
    print(f"Fix applied: {fixed}/{len(rows)}")
    for row in rows:
        if not row["c6_placed"]:
            print(f"  MISSING C6: start={row['start_index']}")


if __name__ == "__main__":
    main()
