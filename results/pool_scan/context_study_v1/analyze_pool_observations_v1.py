"""Read-only pool, touch, and sequence description for the fixed 126-case study.

Reads only files already stored under context_study_v1. Does not load markets,
does not call the scanner, and does not change existing result files.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "case_manifest_8pct_2026-01-01_2026-09-25.csv"
ASSIGN = ROOT / "pool_age_distance_8pct_assignments.csv"
CASES = ROOT / "mtf_context_8pct_cases.jsonl"

NEXT_OUT = ROOT / "pool_observation_next_pools_v1.csv"
TOUCH_OUT = ROOT / "pool_observation_touch_followup_v1.csv"
SEQ_OUT = ROOT / "pool_observation_new_pool_order_v1.csv"
METHOD_OUT = ROOT / "pool_observation_analyzer_method_v1.json"


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def near_distance(row: dict) -> tuple[str, float]:
    """Distance of the start close to the edge that faces the close.

    Upper near edge is bottom_price. Lower near edge is top_price.
    Overlap with the pool box is distance 0.
    """
    close = float(row["start_close"])
    bottom = float(row["bottom_price"])
    top = float(row["top_price"])
    if row["side"] == "upper":
        if bottom > close:
            return "above", bottom - close
        if top < close:
            return "below", close - top
        return "overlap", 0.0
    if top < close:
        return "below", close - top
    if bottom > close:
        return "above", bottom - close
    return "overlap", 0.0


def outer_distance(row: dict) -> float:
    close = float(row["start_close"])
    if row["side"] == "upper":
        return (float(row["top_price"]) - close) / close * 100
    return (close - float(row["bottom_price"])) / close * 100


def main() -> None:
    for path in (NEXT_OUT, TOUCH_OUT, SEQ_OUT, METHOD_OUT):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    manifest = list(csv.DictReader(MANIFEST.open()))
    assignments = list(csv.DictReader(ASSIGN.open()))
    cases = [json.loads(line) for line in CASES.open()]
    if len(manifest) != 126 or len(cases) != 126:
        raise SystemExit(f"manifest {len(manifest)} cases {len(cases)}")
    case_ids = {row["case_id"] for row in manifest}
    if {row["case_id"] for row in assignments} != case_ids:
        raise SystemExit("assignment cases differ from the manifest")

    next_rows = []
    by_case_tf: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in assignments:
        by_case_tf[(row["case_id"], row["timeframe"])].append(row)
    for (case_id, timeframe), group in sorted(by_case_tf.items()):
        meta = next(item for item in manifest if item["case_id"] == case_id)
        located = []
        for row in group:
            place, distance = near_distance(row)
            located.append((place, distance, row))
        for place in ("above", "below"):
            side_rows = [item for item in located if item[0] == place]
            if not side_rows:
                next_rows.append({
                    "case_id": case_id,
                    "symbol": meta["symbol"],
                    "retrospective_direction": meta["direction"],
                    "timeframe": timeframe,
                    "place": place,
                    "pool_id": "",
                    "side": "",
                    "near_distance_price": "",
                    "outer_distance_pct": "",
                    "age_hours": "",
                    "available_at": "",
                    "bottom_price": "",
                    "top_price": "",
                    "start_close": group[0]["start_close"],
                    "candidates_on_place": 0,
                    "note": "no start-active pool on this side of the start close",
                })
                continue
            side_rows.sort(key=lambda item: (item[1], item[2]["pool_id"]))
            _place, distance, chosen = side_rows[0]
            next_rows.append({
                "case_id": case_id,
                "symbol": meta["symbol"],
                "retrospective_direction": meta["direction"],
                "timeframe": timeframe,
                "place": place,
                "pool_id": chosen["pool_id"],
                "side": chosen["side"],
                "near_distance_price": distance,
                "outer_distance_pct": outer_distance(chosen),
                "age_hours": chosen["age_hours"],
                "available_at": chosen["available_at"],
                "bottom_price": chosen["bottom_price"],
                "top_price": chosen["top_price"],
                "start_close": chosen["start_close"],
                "candidates_on_place": len(side_rows),
                "note": "nearest start-active pool by near-edge distance; observation only",
            })

    later_break: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    later_loss: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for rec in cases:
        for item in rec["breaks_during"]:
            later_break[(rec["case_id"], item["timeframe"], item["pool_id"])].append(item)
        for item in rec["selection_losses"]:
            later_loss[(rec["case_id"], item["timeframe"], item["pool_id"])].append(item)
    touch_rows = []
    for rec in cases:
        for item in rec["contacts"]:
            key = (rec["case_id"], item["timeframe"], item["pool_id"])
            touch_time = parse(item["first_as_of"])
            breaks_after = [
                row for row in later_break[key]
                if parse(row["break_known_at"]) >= touch_time
            ]
            losses_after = [
                row for row in later_loss[key]
                if parse(row["as_of"]) >= touch_time
            ]
            breaks_after.sort(key=lambda row: parse(row["break_known_at"]))
            losses_after.sort(key=lambda row: parse(row["as_of"]))
            candidates = []
            if breaks_after:
                candidates.append(("break_known", breaks_after[0]["break_known_at"]))
            if losses_after:
                candidates.append(("selection_loss", losses_after[0]["as_of"]))
            candidates.sort(key=lambda pair: parse(pair[1]))
            touch_rows.append({
                "case_id": rec["case_id"],
                "symbol": rec["symbol"],
                "retrospective_direction": rec["retrospective_direction"],
                "timeframe": item["timeframe"],
                "pool_id": item["pool_id"],
                "side": item["side"],
                "touch_time": item["first_as_of"],
                "available_at": item["available_at"],
                "bottom_price": item["bottom_price"],
                "top_price": item["top_price"],
                "next_stored_event_type": "" if not candidates else candidates[0][0],
                "next_stored_event_time": "" if not candidates else candidates[0][1],
                "break_known_after_touch": "" if not breaks_after else breaks_after[0]["break_known_at"],
                "selection_loss_after_touch": "" if not losses_after else f"{losses_after[0]['as_of']}|{losses_after[0]['kind']}",
                "price_reaction_after_touch": "not_available",
                "note": "Later stored break or selection loss only. Candle path away, return, or outside close is not stored for the full cohort.",
            })

    sequence_rows = []
    births = []
    for rec in cases:
        for item in rec["new_pools"]:
            births.append({
                "case_id": rec["case_id"],
                "symbol": rec["symbol"],
                "retrospective_direction": rec["retrospective_direction"],
                "timeframe": item["timeframe"],
                "pool_id": item["pool_id"],
                "side": item["side"],
                "available_at": item["available_at"],
                "bottom_price": item["bottom_price"],
                "top_price": item["top_price"],
            })
    births.sort(key=lambda item: (
        item["case_id"],
        item["timeframe"],
        parse(item["available_at"]),
        item["pool_id"],
    ))
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in births:
        grouped[(event["case_id"], event["timeframe"])].append(event)
    for (case_id, timeframe), group in sorted(grouped.items()):
        meta = next(item for item in manifest if item["case_id"] == case_id)
        for index, event in enumerate(group, start=1):
            previous = group[index - 2] if index > 1 else None
            sequence_rows.append({
                "case_id": case_id,
                "symbol": group[0]["symbol"],
                "retrospective_direction": meta["direction"],
                "timeframe": timeframe,
                "order_in_timeframe": index,
                "pool_id": event["pool_id"],
                "side": event["side"],
                "available_at": event["available_at"],
                "bottom_price": event["bottom_price"],
                "top_price": event["top_price"],
                "previous_pool_id": "" if previous is None else previous["pool_id"],
                "previous_side": "" if previous is None else previous["side"],
                "same_side_as_previous": "" if previous is None else str(previous["side"] == event["side"]).lower(),
                "note": "Birth order inside the case and timeframe. No minimum run length is applied.",
            })

    def write(path: Path, rows: list[dict]) -> None:
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    write(NEXT_OUT, next_rows)
    write(TOUCH_OUT, touch_rows)
    write(SEQ_OUT, sequence_rows)
    METHOD_OUT.write_text(json.dumps({
        "script": Path(__file__).name,
        "reads": [MANIFEST.name, ASSIGN.name, CASES.name],
        "writes": [NEXT_OUT.name, TOUCH_OUT.name, SEQ_OUT.name],
        "nearest_pool": (
            "Among start-active assignments, the pool on the above or below side "
            "of the start close with the smallest absolute distance to its near edge. "
            "Upper near edge is bottom_price. Lower near edge is top_price."
        ),
        "touch_followup": (
            "Later stored events for the same case, timeframe and pool_id. "
            "A price reaction after the touch is marked not_available."
        ),
        "new_pool_order": "new_pool events ordered by available_at inside each case and timeframe.",
        "not_included": "EMA, OB+delta, entries, stops, take-profit, and signal quality.",
        "counts": {
            "cases": 126,
            "nearest_rows": len(next_rows),
            "touch_rows": len(touch_rows),
            "new_pool_rows": len(sequence_rows),
        },
    }, indent=2) + "\n")
    print(
        "nearest", len(next_rows),
        "touch", len(touch_rows),
        "births", len(sequence_rows),
    )


if __name__ == "__main__":
    main()
