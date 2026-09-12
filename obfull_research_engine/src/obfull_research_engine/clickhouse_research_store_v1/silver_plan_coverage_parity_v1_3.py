"""Read-only coverage parity analysis for Silver v1.3 epoch/chunk planning."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .coverage_accounting_v2 import (
    interval_duration_ns,
    merge_intervals,
    subtract_intervals,
)
from .epoch_aware_silver_v1_3 import EpochDefinition
from .silver_full_build_v1_3 import (
    BuildConfig,
    BuildPlan,
    ChunkPlan,
    build_epoch_plan,
    load_segment_metadata,
    plan_epoch_chunks,
)

Interval = tuple[int, int]
BUCKET_NS = 100_000_000


def _epoch_interval(epoch: EpochDefinition) -> Interval:
    return (int(epoch.safe_start_ns), int(epoch.safe_end_ns))


def load_audit_safe_intervals(path: Path) -> list[Interval]:
    intervals: list[Interval] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        intervals.append((int(row["start_ns"]), int(row["end_ns"])))
    return merge_intervals(intervals)


def chunk_output_interval(chunk: ChunkPlan) -> Interval:
    return (int(chunk.analysis_start_ns), int(chunk.analysis_end_ns))


def planned_output_union(chunks: Iterable[ChunkPlan]) -> list[Interval]:
    return merge_intervals(chunk_output_interval(chunk) for chunk in chunks)


def compare_interval_sets(
    left: Iterable[Interval],
    right: Iterable[Interval],
    *,
    label_left: str,
    label_right: str,
) -> dict[str, Any]:
    left_m = merge_intervals(left)
    right_m = merge_intervals(right)
    left_only = subtract_intervals(left_m, right_m)
    right_only = subtract_intervals(right_m, left_m)
    shared = subtract_intervals(left_m, left_only)
    return {
        "label_left": label_left,
        "label_right": label_right,
        "left_count": len(left_m),
        "right_count": len(right_m),
        "left_union_ns": interval_duration_ns(left_m),
        "right_union_ns": interval_duration_ns(right_m),
        "shared_union_ns": interval_duration_ns(shared),
        "left_only_union_ns": interval_duration_ns(left_only),
        "right_only_union_ns": interval_duration_ns(right_only),
        "left_only_intervals": left_only[:20],
        "right_only_intervals": right_only[:20],
    }


def classify_excluded_safe_ns(
    safe_union: list[Interval],
    planned_output: list[Interval],
) -> dict[str, Any]:
    excluded = subtract_intervals(safe_union, planned_output)
    warmup_like: list[Interval] = []
    tail_remainder: list[Interval] = []
    short_epoch_total: list[Interval] = []
    for start, end in excluded:
        duration = end - start
        if duration <= 5 * 60 * 1_000_000_000:
            warmup_like.append((start, end))
        elif duration < 15 * 60 * 1_000_000_000:
            short_epoch_total.append((start, end))
        else:
            tail_remainder.append((start, end))
    return {
        "excluded_union_ns": interval_duration_ns(excluded),
        "excluded_interval_count": len(excluded),
        "likely_warmup_skip_ns": interval_duration_ns(warmup_like),
        "likely_short_epoch_ns": interval_duration_ns(short_epoch_total),
        "likely_tail_remainder_ns": interval_duration_ns(tail_remainder),
        "excluded_intervals_sample": excluded[:20],
    }


def bucket_count(intervals: Iterable[Interval]) -> int:
    total = 0
    for start, end in merge_intervals(intervals):
        cursor = ((start + BUCKET_NS - 1) // BUCKET_NS) * BUCKET_NS
        while cursor < end:
            total += 1
            cursor += BUCKET_NS
    return total


def chunk_plan_hash(chunks: Iterable[ChunkPlan]) -> str:
    from .epoch_aware_silver_v1_3 import _hash

    material = [
        {
            "chunk_key": chunk.chunk_key,
            "analysis_start_ns": chunk.analysis_start_ns,
            "analysis_end_ns": chunk.analysis_end_ns,
            "warmup_ns": chunk.warmup_ns,
            "epoch_id": chunk.epoch.epoch_id,
        }
        for chunk in chunks
    ]
    return _hash(material)


def epoch_plan_report(
    client: Any,
    config: BuildConfig,
    *,
    audit_safe_path: Path,
    chunk_market_minutes: int | None = None,
    warmup_minutes: int | None = None,
) -> dict[str, Any]:
    metadata = load_segment_metadata(client, config)
    plan = build_epoch_plan(client, config, metadata)
    chunk_minutes = (
        config.chunk_market_minutes
        if chunk_market_minutes is None
        else chunk_market_minutes
    )
    warmup = config.warmup_minutes if warmup_minutes is None else warmup_minutes
    chunks = plan_epoch_chunks(
        plan.epochs,
        chunk_market_minutes=chunk_minutes,
        warmup_minutes=warmup,
    )
    plan.chunks = chunks

    production_safe = merge_intervals(_epoch_interval(epoch) for epoch in plan.epochs)
    audit_safe = load_audit_safe_intervals(audit_safe_path)
    output_union = planned_output_union(chunks)

    physical_start = min(row["first_event_time_ns"] for row in metadata.values())
    physical_end = max(row["last_event_time_ns"] for row in metadata.values()) + 1
    physical_union = [(physical_start, physical_end)]

    epoch_stats = []
    epochs_with_zero_chunks = 0
    for index, epoch in enumerate(plan.epochs, 1):
        epoch_chunks = [c for c in chunks if c.epoch.epoch_id == epoch.epoch_id]
        safe_duration = int(epoch.safe_end_ns) - int(epoch.safe_start_ns)
        output_duration = sum(
            c.analysis_end_ns - c.analysis_start_ns for c in epoch_chunks
        )
        excluded = safe_duration - output_duration
        if not epoch_chunks:
            epochs_with_zero_chunks += 1
        epoch_stats.append(
            {
                "epoch_index": index,
                "epoch_id": epoch.epoch_id,
                "safe_duration_ns": safe_duration,
                "planned_output_ns": output_duration,
                "excluded_safe_ns": excluded,
                "chunk_count": len(epoch_chunks),
                "anchor_type": epoch.anchor_type,
                "terminating_reason": epoch.terminating_reason,
            }
        )

    excluded_breakdown = classify_excluded_safe_ns(production_safe, output_union)
    coverage_equation = {
        "safe_source_union_ns": interval_duration_ns(production_safe),
        "planned_silver_output_union_ns": interval_duration_ns(output_union),
        "intentionally_excluded_safe_ns": excluded_breakdown["excluded_union_ns"],
        "unresolved_ns": interval_duration_ns(
            subtract_intervals(production_safe, merge_intervals(output_union + subtract_intervals(production_safe, output_union)))
        ),
    }
    coverage_equation["equation_difference_ns"] = (
        coverage_equation["safe_source_union_ns"]
        - coverage_equation["planned_silver_output_union_ns"]
        - coverage_equation["intentionally_excluded_safe_ns"]
        - coverage_equation["unresolved_ns"]
    )

    return {
        "epoch_plan_hash": plan.epoch_plan_hash,
        "chunk_plan_hash": chunk_plan_hash(chunks),
        "epoch_count": len(plan.epochs),
        "gap_count": len(plan.gaps),
        "chunk_count": len(chunks),
        "epochs_with_zero_chunks": epochs_with_zero_chunks,
        "audit_vs_production": compare_interval_sets(
            audit_safe,
            production_safe,
            label_left="audit_safe_v3",
            label_right="production_epochs",
        ),
        "coverage_equation": coverage_equation,
        "excluded_breakdown": excluded_breakdown,
        "physical_union_ns": interval_duration_ns(physical_union),
        "expected_output_buckets": bucket_count(output_union),
        "expected_safe_buckets": bucket_count(production_safe),
        "chunk_market_minutes": chunk_minutes,
        "warmup_minutes": warmup,
        "epoch_stats_sample": sorted(
            epoch_stats, key=lambda row: row["excluded_safe_ns"], reverse=True
        )[:20],
    }
