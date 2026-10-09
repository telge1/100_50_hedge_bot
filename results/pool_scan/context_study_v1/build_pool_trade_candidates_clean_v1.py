"""Build a clean, non-overlapping pool candidate shortlist.

Input:
- pool_trade_candidates_v1.csv

Output:
- pool_trade_candidates_clean_v1.csv
- pool_trade_candidates_clean_summary_v1.csv
- pool_trade_candidates_clean_method_v1.json

Rules:
- keep only unique pool candidates
- keep only candidates that hit TP1 or TP2
- close each candidate at the first TP hit
- remove overlaps inside each case/timeframe by greedy time ordering
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "pool_trade_candidates_v1.csv"
OUT_ROWS = ROOT / "pool_trade_candidates_clean_v1.csv"
OUT_SUMMARY = ROOT / "pool_trade_candidates_clean_summary_v1.csv"
OUT_METHOD = ROOT / "pool_trade_candidates_clean_method_v1.json"


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def target_exit(row: dict) -> tuple[str, str]:
    """Return exit time and exit reason from the first hit target."""
    hits = []
    if row["tp1_hit"] == "true" and row["tp1_hit_time"]:
        hits.append(("tp1", row["tp1_hit_time"]))
    if row["tp2_hit"] == "true" and row["tp2_hit_time"]:
        hits.append(("tp2", row["tp2_hit_time"]))
    if not hits:
        return "", ""
    reason, stamp = min(hits, key=lambda item: parse(item[1]))
    return stamp, reason


def exit_price(row: dict, reason: str) -> str:
    if reason == "tp1":
        if row["hypothetical_direction"] == "short":
            return row["tp1_top_price"]
        return row["tp1_bottom_price"]
    if reason == "tp2":
        if row["hypothetical_direction"] == "short":
            return row["tp2_top_price"]
        return row["tp2_bottom_price"]
    return ""


def main() -> None:
    for path in (OUT_ROWS, OUT_SUMMARY, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")
    rows = list(csv.DictReader(INPUT.open()))
    if not rows:
        raise SystemExit("input is empty")

    # Deduplicate exact candidate identity first.
    unique: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (row["case_id"], row["timeframe"], row["pool_id"], row["hypothetical_direction"])
        if key not in unique:
            unique[key] = row
        else:
            old = unique[key]
            old_score = float(old["mfe_pct"]) - float(old["mae_before_mfe_pct"])
            new_score = float(row["mfe_pct"]) - float(row["mae_before_mfe_pct"])
            if new_score > old_score:
                unique[key] = row

    enriched = []
    for row in unique.values():
        exit_time, exit_reason = target_exit(row)
        if not exit_time:
            continue
        enriched.append(
            {
                **row,
                "exit_time": exit_time,
                "exit_reason": exit_reason,
                "exit_price": exit_price(row, exit_reason),
                "bars_held": (
                    int((parse(exit_time) - parse(row["entry_time"])).total_seconds() // 60)
                    if exit_time
                    else ""
                ),
                "score": float(row["mfe_pct"]) - float(row["mae_before_mfe_pct"]),
            }
        )

    # Greedy non-overlap per case + timeframe, ordered by entry time.
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in enriched:
        grouped[(row["case_id"], row["timeframe"])].append(row)

    kept = []
    dropped_overlaps = 0
    dropped_without_exit = len(unique) - len(enriched)
    for key, group in grouped.items():
        group.sort(key=lambda row: (parse(row["entry_time"]), -row["score"], -float(row["mfe_pct"])))
        last_exit = None
        for row in group:
            entry = parse(row["entry_time"])
            exit_at = parse(row["exit_time"])
            if last_exit is not None and entry <= last_exit:
                dropped_overlaps += 1
                continue
            kept.append(row)
            last_exit = exit_at

    kept.sort(key=lambda row: (
        row["case_id"],
        row["timeframe"],
        parse(row["entry_time"]),
        row["pool_id"],
    ))

    fieldnames = [
        "case_id",
        "symbol",
        "timeframe",
        "retrospective_direction",
        "pool_id",
        "side",
        "hypothetical_direction",
        "entry_time",
        "exit_time",
        "exit_reason",
        "entry_price",
        "exit_price",
        "bars_held",
        "mfe_pct",
        "mae_before_mfe_pct",
        "absolute_mae_pct",
        "tp1_pool_id",
        "tp1_hit_time",
        "tp2_pool_id",
        "tp2_hit_time",
        "score",
        "note",
    ]

    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in kept:
            writer.writerow({name: row.get(name, "") for name in fieldnames})

    summary = Counter()
    by_tf_dir = defaultdict(Counter)
    for row in kept:
        by_tf_dir[(row["timeframe"], row["hypothetical_direction"])]["n"] += 1
        by_tf_dir[(row["timeframe"], row["hypothetical_direction"])]["tp1"] += int(row["tp1_hit"] == "true")
        by_tf_dir[(row["timeframe"], row["hypothetical_direction"])]["tp2"] += int(row["tp2_hit"] == "true")
        by_tf_dir[(row["timeframe"], row["hypothetical_direction"])]["mfe_sum"] += float(row["mfe_pct"])
        by_tf_dir[(row["timeframe"], row["hypothetical_direction"])]["mae_sum"] += float(row["mae_before_mfe_pct"])

    summary_rows = []
    for timeframe in ("4h", "30m", "15m"):
        for direction in ("long", "short"):
            bucket = by_tf_dir[(timeframe, direction)]
            n = bucket["n"]
            if not n:
                continue
            summary_rows.append(
                {
                    "timeframe": timeframe,
                    "direction": direction,
                    "candidates": n,
                    "tp1_hit": bucket["tp1"],
                    "tp2_hit": bucket["tp2"],
                    "tp1_hit_rate": bucket["tp1"] / n,
                    "tp2_hit_rate": bucket["tp2"] / n,
                    "avg_mfe_pct": bucket["mfe_sum"] / n,
                    "avg_mae_before_mfe_pct": bucket["mae_sum"] / n,
                }
            )

    with OUT_SUMMARY.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    OUT_METHOD.write_text(
        json.dumps(
            {
                "script": Path(__file__).name,
                "input": INPUT.name,
                "rules": [
                    "dedupe by case_id + timeframe + pool_id + hypothetical_direction",
                    "keep only candidates with a TP hit",
                    "close at the first TP hit",
                    "remove overlaps inside each case/timeframe with greedy entry ordering",
                ],
                "dropped_without_exit": dropped_without_exit,
                "dropped_overlaps": dropped_overlaps,
                "kept": len(kept),
            },
            indent=2,
        )
        + "\n"
    )
    print("kept", len(kept), "dropped_without_exit", dropped_without_exit, "dropped_overlaps", dropped_overlaps)


if __name__ == "__main__":
    main()
