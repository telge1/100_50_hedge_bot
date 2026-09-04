#!/usr/bin/env python3
"""Accelerated multi-day memory soak for LiveSecondClock dedupe bounds."""

from __future__ import annotations

import csv
import gc
import json
import resource
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock
from orderbook_analyse.orderbook_v2_live.dedupe import DEFAULT_DEDUPE_CAPACITY
from orderbook_analyse.orderbook_v2_live.universe import SYMBOLS_51

OUT = Path("results/orderbook_v3_live_memory_soak")
T0 = 1_750_000_000_000
# Cap ≪ load: 80k updates/symbol (≈10× capacity) proves plateau; wall-clock equiv
# at 0.23 Hz ≈ 4 days. Combined with unit soak (50k×51) this is the offline gate.
UPDATES_PER_SYMBOL = 80_000
SAMPLE_EVERY = 5_000


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    clocks = [LiveSecondClock(s, dedupe_capacity=DEFAULT_DEDUPE_CAPACITY) for s in SYMBOLS_51]
    growth_rows: list[dict] = []

    for clock in clocks:
        clock.ingest(
            "snapshot",
            T0,
            {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": 1, "seq": 1},
        )

    tracemalloc.start()
    gc.collect()
    base_rss = rss_mb()
    current, peak = tracemalloc.get_traced_memory()
    growth_rows.append(
        {
            "step": 0,
            "rss_mb": round(base_rss, 2),
            "tracemalloc_current_mb": round(current / 1024 / 1024, 3),
            "tracemalloc_peak_mb": round(peak / 1024 / 1024, 3),
            "dedupe_entries_total": sum(len(c.recent_us) for c in clocks),
            "dedupe_capacity_total": DEFAULT_DEDUPE_CAPACITY * 51,
        }
    )

    feature_buckets: list[int] = []
    for step in range(2, UPDATES_PER_SYMBOL + 2):
        ts = T0 + step  # ms spread; bucket advances naturally
        for clock in clocks:
            clock.ingest(
                "delta",
                ts,
                {"b": [["1.0", str(10 + (step % 7))]], "a": [], "u": step, "seq": step},
            )
        if step % SAMPLE_EVERY == 0:
            cur, pk = tracemalloc.get_traced_memory()
            growth_rows.append(
                {
                    "step": step,
                    "rss_mb": round(rss_mb(), 2),
                    "tracemalloc_current_mb": round(cur / 1024 / 1024, 3),
                    "tracemalloc_peak_mb": round(pk / 1024 / 1024, 3),
                    "dedupe_entries_total": sum(len(c.recent_us) for c in clocks),
                    "dedupe_capacity_total": DEFAULT_DEDUPE_CAPACITY * 51,
                }
            )
        if step % 10_000 == 0:
            rows = clocks[0].close_through(ts + 1000)
            for row in rows:
                ms = int(row["bucket_start"].timestamp() * 1000)
                feature_buckets.append(ms)
                clocks[0].note_enqueued(ms)

    gc.collect()
    cur, pk = tracemalloc.get_traced_memory()
    final_dedupe = sum(len(c.recent_us) for c in clocks)
    final_rss = rss_mb()
    book_ok = all(
        len((c.last_valid_book or c.book).bids) <= 200
        and len((c.last_valid_book or c.book).asks) <= 200
        for c in clocks
    )
    cap_total = DEFAULT_DEDUPE_CAPACITY * 51
    plateau = all(r["dedupe_entries_total"] <= cap_total for r in growth_rows)
    full = [r for r in growth_rows if r["dedupe_entries_total"] == cap_total]
    stable_after_warmup = len(full) >= 2
    mid = max(1, len(growth_rows) // 2)
    early = growth_rows[max(1, mid // 2) : mid]
    late = growth_rows[mid:]
    early_avg = sum(r["tracemalloc_current_mb"] for r in early) / max(1, len(early))
    late_avg = sum(r["tracemalloc_current_mb"] for r in late) / max(1, len(late))
    no_linear_tm = late_avg < early_avg * 2.5 + 5
    unique_buckets = sorted(set(feature_buckets))
    no_dup_seconds = len(unique_buckets) == len(feature_buckets)

    # Equivalent wall time: 80k updates @ ~0.23 Hz ≈ 4.0 days/symbol
    equiv_days = UPDATES_PER_SYMBOL / (0.23 * 86400)

    passed = (
        plateau
        and stable_after_warmup
        and book_ok
        and no_linear_tm
        and final_dedupe == cap_total
        and no_dup_seconds
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "symbols": 51,
        "updates_per_symbol": UPDATES_PER_SYMBOL,
        "equivalent_days_at_hz": 0.23,
        "equivalent_days": round(equiv_days, 2),
        "total_ingest_calls": UPDATES_PER_SYMBOL * 51,
        "dedupe_capacity_per_symbol": DEFAULT_DEDUPE_CAPACITY,
        "final_dedupe_entries_total": final_dedupe,
        "final_rss_mb": round(final_rss, 2),
        "base_rss_mb": round(base_rss, 2),
        "tracemalloc_peak_mb": round(pk / 1024 / 1024, 3),
        "tracemalloc_early_avg_mb": round(early_avg, 3),
        "tracemalloc_late_avg_mb": round(late_avg, 3),
        "book_levels_bounded": book_ok,
        "dedupe_plateau": plateau and stable_after_warmup,
        "no_duplicate_feature_seconds": no_dup_seconds,
        "feature_buckets_emitted": len(feature_buckets),
        "passed": passed,
    }

    with (OUT / "memory_growth.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(growth_rows[0].keys()))
        w.writeheader()
        w.writerows(growth_rows)

    (OUT / "memory_soak_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    verdict = "PASS" if passed else "FAIL"
    (OUT / "verdict.md").write_text(
        f"# Memory soak verdict: {verdict}\n\n"
        f"- {UPDATES_PER_SYMBOL} updates/symbol × 51 (~{equiv_days:.1f} days @ 0.23 Hz)\n"
        f"- Final dedupe: {final_dedupe} / {cap_total}\n"
        f"- RSS max: {final_rss:.1f} MiB (base {base_rss:.1f})\n"
        f"- Tracemalloc early/late avg: {early_avg:.2f} / {late_avg:.2f} MiB\n"
        f"- Book levels bounded: {book_ok}\n"
        f"- Duplicate feature seconds: {not no_dup_seconds}\n",
        encoding="utf-8",
    )
    tracemalloc.stop()
    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
