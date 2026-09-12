"""Read-only coverage parity analysis for Silver v1.3 epoch/chunk planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .coverage_accounting_v2 import (
    intersect_intervals,
    interval_duration_ns,
    merge_intervals,
    partition_coverage,
    subtract_intervals,
)
from .epoch_aware_silver_v1_3 import EpochDefinition
from .silver_full_build_v1_3 import (
    BuildConfig,
    ChunkPlan,
    build_epoch_plan,
    load_segment_metadata,
    plan_epoch_chunks,
)

Interval = tuple[int, int]
BUCKET_NS = 100_000_000


def _epoch_interval(epoch: EpochDefinition) -> Interval:
    return (int(epoch.safe_start_ns), int(epoch.safe_end_ns))


def physical_union_from_metadata(metadata: dict[int, dict[str, Any]]) -> list[Interval]:
    """Per-segment physical intervals from Bronze segment metadata."""
    return merge_intervals(
        (
            int(row["first_event_time_ns"]),
            int(row["last_event_time_ns"]) + 1,
        )
        for row in metadata.values()
    )


def load_audit_coverage_artifacts(
    coverage_path: Path,
) -> dict[str, Any]:
    """Load audit blind/boundary intervals and stored coverage equation."""
    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    blind = [
        (int(row["start_ns"]), int(row["end_ns"]))
        for row in payload.get("blind_intervals", [])
    ]
    boundary = [
        (int(row["start_ns"]), int(row["end_ns"]))
        for row in payload.get("boundary_excluded_intervals", [])
    ]
    return {
        "coverage_equation": dict(payload.get("coverage_equation", {})),
        "blind_intervals": blind,
        "boundary_excluded_intervals": boundary,
    }


def production_safe_union(epochs: Iterable[EpochDefinition]) -> list[Interval]:
    return merge_intervals(_epoch_interval(epoch) for epoch in epochs)


def production_epoch_gaps(epochs: Sequence[EpochDefinition]) -> list[Interval]:
    gaps: list[Interval] = []
    ordered = list(epochs)
    for left, right in zip(ordered, ordered[1:]):
        if int(right.safe_start_ns) > int(left.safe_end_ns):
            gaps.append((int(left.safe_end_ns), int(right.safe_start_ns)))
    return gaps


def audit_inter_safe_gaps(audit_safe: Iterable[Interval]) -> list[Interval]:
    merged = merge_intervals(audit_safe)
    return [
        (left_end, right_start)
        for (_, left_end), (right_start, _) in zip(merged, merged[1:])
        if right_start > left_end
    ]


def _interval_detail_rows(
    intervals: Iterable[Interval],
    *,
    category: str,
) -> list[dict[str, int | str]]:
    return [
        {
            "category": category,
            "start_ns": start,
            "end_ns": end,
            "duration_ns": end - start,
        }
        for start, end in intervals
    ]


def _epoch_containing_ns(
    epochs: Sequence[EpochDefinition], ns: int
) -> EpochDefinition | None:
    point = int(ns)
    for epoch in epochs:
        if int(epoch.safe_start_ns) <= point < int(epoch.safe_end_ns):
            return epoch
    return None


def _epoch_covering_interval(
    epochs: Sequence[EpochDefinition], interval: Interval
) -> EpochDefinition | None:
    start, end = interval
    for epoch in epochs:
        if (
            int(epoch.safe_start_ns) <= int(start)
            and int(end) <= int(epoch.safe_end_ns)
        ):
            return epoch
    return None


def decompose_production_only_against_audit(
    *,
    production_only: Iterable[Interval],
    audit_blind: Iterable[Interval],
    audit_boundary: Iterable[Interval],
    production_epochs: Sequence[EpochDefinition],
) -> dict[str, Any]:
    """Disjoint partition of production_safe − audit_safe against audit categories."""
    only_m = merge_intervals(production_only)
    blind_m = merge_intervals(audit_blind)
    boundary_m = merge_intervals(audit_boundary)
    overlap_blind = intersect_intervals(only_m, blind_m)
    remaining = subtract_intervals(only_m, blind_m)
    overlap_boundary = intersect_intervals(remaining, boundary_m)
    outside_both = subtract_intervals(remaining, boundary_m)

    def _enrich(intervals: list[Interval], *, cause: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for start, end in intervals:
            epoch = _epoch_covering_interval(production_epochs, (start, end))
            row: dict[str, Any] = {
                "start_ns": start,
                "end_ns": end,
                "duration_ns": end - start,
                "prior_audit_classification": cause,
            }
            if epoch is None:
                row["continuity_proof"] = "STOP_NO_CONTAINING_PRODUCTION_EPOCH"
                rows.append(row)
                continue
            row.update(
                {
                    "production_epoch_id": epoch.epoch_id,
                    "anchor_type": epoch.anchor_type,
                    "anchor_provenance": epoch.anchor_provenance,
                    "epoch_terminating_reason": epoch.terminating_reason,
                    "continuity_proof": (
                        "PRODUCTION_EPOCH_CONTAINS_INTERVAL_WITH_DELTA_CONTINUITY"
                    ),
                }
            )
            if cause == "AUDIT_INTER_SAFE_MICROGAP_BRIDGED_BY_DISCOVER_EPOCHS":
                row["prior_audit_classification_detail"] = (
                    "Audit v3 split consecutive safe intervals at segment_end "
                    "float-to-ns drift; discover_epochs() continued delta "
                    "application inside one production epoch."
                )
            elif cause == "AUDIT_BLIND_OVERLAP_RECLASSIFIED_AS_PRODUCTION_SAFE":
                row["prior_audit_classification_detail"] = (
                    "Interval overlapped audit blind but lies inside a production "
                    "epoch with proven delta continuity; audit blind label is "
                    "superseded for Silver v1.3."
                )
            elif cause == "AUDIT_BOUNDARY_OVERLAP_RECLASSIFIED_AS_PRODUCTION_SAFE":
                row["prior_audit_classification_detail"] = (
                    "Interval overlapped audit boundary_excluded but lies inside a "
                    "production epoch with proven delta continuity."
                )
            elif cause == "BOUNDED_INPUT_SCAN_END_EXTENSION":
                row["prior_audit_classification_detail"] = (
                    "Single-ns scan_end extension at last Bronze event + 1."
                )
            rows.append(row)
        return rows

    outside_rows = _enrich(
        outside_both,
        cause="AUDIT_INTER_SAFE_MICROGAP_BRIDGED_BY_DISCOVER_EPOCHS",
    )
    for row in outside_rows:
        if int(row["duration_ns"]) == 1:
            row["prior_audit_classification"] = "BOUNDED_INPUT_SCAN_END_EXTENSION"
            row["prior_audit_classification_detail"] = (
                "Single-ns scan_end extension at last Bronze event + 1."
            )

    blind_rows = _enrich(
        overlap_blind,
        cause="AUDIT_BLIND_OVERLAP_RECLASSIFIED_AS_PRODUCTION_SAFE",
    )
    boundary_rows = _enrich(
        overlap_boundary,
        cause="AUDIT_BOUNDARY_OVERLAP_RECLASSIFIED_AS_PRODUCTION_SAFE",
    )

    def _bucket(category: str, intervals: list[Interval], rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "category": category,
            "interval_count": len(intervals),
            "union_ns": interval_duration_ns(intervals),
            "intervals": rows,
        }

    partition_exact_ns = (
        interval_duration_ns(overlap_blind)
        + interval_duration_ns(overlap_boundary)
        + interval_duration_ns(outside_both)
        - interval_duration_ns(only_m)
    )
    continuity_stops = [
        row
        for bucket in (blind_rows, boundary_rows, outside_rows)
        for row in bucket
        if str(row.get("continuity_proof", "")).startswith("STOP_")
    ]
    return {
        "production_only_union_ns": interval_duration_ns(only_m),
        "partition_exact_ns": partition_exact_ns,
        "overlap_audit_blind": _bucket(
            "production_only ∩ audit_blind", overlap_blind, blind_rows
        ),
        "overlap_audit_boundary": _bucket(
            "production_only ∩ audit_boundary_excluded",
            overlap_boundary,
            boundary_rows,
        ),
        "outside_audit_blind_and_boundary": _bucket(
            "production_only outside audit blind and boundary",
            outside_both,
            outside_rows,
        ),
        "continuity_stop_count": len(continuity_stops),
        "continuity_stops": continuity_stops,
    }


def production_coverage_contract(
    *,
    physical: Iterable[Interval],
    production_safe: Iterable[Interval],
    production_epochs: Sequence[EpochDefinition],
) -> dict[str, Any]:
    """Authoritative production partition on Bronze physical evidence."""
    true_blind = production_epoch_gaps(production_epochs)
    safe_m = merge_intervals(production_safe)
    partition = partition_coverage(
        physical=physical,
        safe=safe_m,
        blind=true_blind,
    )
    safe_full_ns = interval_duration_ns(safe_m)
    safe_within_physical_ns = int(partition["safe_union_ns"])
    return {
        "production_physical_union_ns": int(partition["physical_union_ns"]),
        "production_safe_union_ns": safe_full_ns,
        "production_safe_within_physical_ns": safe_within_physical_ns,
        "production_safe_outside_physical_ns": safe_full_ns - safe_within_physical_ns,
        "production_true_blind_union_ns": int(partition["blind_union_ns"]),
        "production_boundary_union_ns": int(partition["boundary_excluded_union_ns"]),
        "production_unresolved_union_ns": int(partition["unresolved_union_ns"]),
        "physical_partition_equation_difference_ns": int(
            partition["equation_difference_ns"]
        ),
        "production_true_blind_interval_count": len(partition["blind"]),
        "production_boundary_interval_count": len(partition["boundary_excluded"]),
        "physical_partition_balances": partition["equation_difference_ns"] == 0,
        "silver_authoritative_equation_ns": (
            int(partition["physical_union_ns"])
            - safe_within_physical_ns
            - int(partition["blind_union_ns"])
            - int(partition["boundary_excluded_union_ns"])
            - int(partition["unresolved_union_ns"])
        ),
    }


def reconcile_audit_production_safe(
    *,
    audit_safe: Iterable[Interval],
    production_safe: Iterable[Interval],
    production_epochs: Sequence[EpochDefinition],
) -> dict[str, Any]:
    """Explain production_safe − audit_safe_v3 as bridged inter-safe micro-gaps."""
    audit_m = merge_intervals(audit_safe)
    production_m = merge_intervals(production_safe)
    production_only = subtract_intervals(production_m, audit_m)
    gaps = audit_inter_safe_gaps(audit_m)
    bridged_gaps = intersect_intervals(gaps, production_only)
    not_bridged_gaps = subtract_intervals(gaps, production_only)
    epoch_gaps = production_epoch_gaps(production_epochs)
    scan_end_extension = subtract_intervals(production_only, bridged_gaps)
    return {
        "audit_inter_safe_gap_count": len(gaps),
        "audit_inter_safe_gap_union_ns": interval_duration_ns(gaps),
        "bridged_gap_count": len(bridged_gaps),
        "bridged_gap_union_ns": interval_duration_ns(bridged_gaps),
        "not_bridged_gap_count": len(not_bridged_gaps),
        "not_bridged_gap_union_ns": interval_duration_ns(not_bridged_gaps),
        "production_only_interval_count": len(production_only),
        "production_only_union_ns": interval_duration_ns(production_only),
        "scan_end_extension_ns": interval_duration_ns(scan_end_extension),
        "production_epoch_gap_count": len(epoch_gaps),
        "production_epoch_gap_union_ns": interval_duration_ns(epoch_gaps),
        "not_bridged_equals_production_epoch_gaps": not_bridged_gaps == epoch_gaps,
        "gap_partition_exact_ns": (
            interval_duration_ns(bridged_gaps) + interval_duration_ns(not_bridged_gaps)
            - interval_duration_ns(gaps)
        ),
        "production_only_equals_bridged_plus_scan_end_ns": (
            interval_duration_ns(production_only)
            - interval_duration_ns(bridged_gaps)
            - interval_duration_ns(scan_end_extension)
        ),
    }


def silver_plan_coverage_equation(
    production_safe: Iterable[Interval],
    planned_output: Iterable[Interval],
) -> dict[str, int]:
    safe_m = merge_intervals(production_safe)
    output_m = merge_intervals(planned_output)
    excluded = classify_excluded_safe_ns(safe_m, output_m)
    unresolved = subtract_intervals(
        safe_m,
        merge_intervals(output_m + subtract_intervals(safe_m, output_m)),
    )
    equation = {
        "safe_source_union_ns": interval_duration_ns(safe_m),
        "planned_silver_output_union_ns": interval_duration_ns(output_m),
        "intentionally_excluded_safe_ns": int(excluded["excluded_union_ns"]),
        "unresolved_ns": interval_duration_ns(unresolved),
    }
    equation["equation_difference_ns"] = (
        equation["safe_source_union_ns"]
        - equation["planned_silver_output_union_ns"]
        - equation["intentionally_excluded_safe_ns"]
        - equation["unresolved_ns"]
    )
    return equation


def full_numeric_audit(
    client: Any,
    config: BuildConfig,
    *,
    audit_safe_path: Path,
    audit_coverage_path: Path,
    chunk_market_minutes: int | None = None,
    warmup_minutes: int | None = None,
) -> dict[str, Any]:
    """Canonical read-only numeric audit for Silver v1.3 planning."""
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
    audit_safe = load_audit_safe_intervals(audit_safe_path)
    audit_cov = load_audit_coverage_artifacts(audit_coverage_path)
    physical = physical_union_from_metadata(metadata)
    production_safe = production_safe_union(plan.epochs)
    output_union = planned_output_union(chunks)
    audit_partition = partition_coverage(
        physical=physical,
        safe=audit_safe,
        blind=audit_cov["blind_intervals"],
    )
    production_only = subtract_intervals(production_safe, audit_safe)
    production_only_decomposition = decompose_production_only_against_audit(
        production_only=production_only,
        audit_blind=audit_cov["blind_intervals"],
        audit_boundary=audit_cov["boundary_excluded_intervals"],
        production_epochs=plan.epochs,
    )
    production_contract = production_coverage_contract(
        physical=physical,
        production_safe=production_safe,
        production_epochs=plan.epochs,
    )
    excluded_breakdown = classify_excluded_safe_ns(production_safe, output_union)
    epoch_stats = []
    epochs_with_zero_chunks = 0
    for index, epoch in enumerate(plan.epochs, 1):
        epoch_chunks = [c for c in chunks if c.epoch.epoch_id == epoch.epoch_id]
        safe_duration = int(epoch.safe_end_ns) - int(epoch.safe_start_ns)
        output_duration = sum(
            c.analysis_end_ns - c.analysis_start_ns for c in epoch_chunks
        )
        if not epoch_chunks:
            epochs_with_zero_chunks += 1
        epoch_stats.append(
            {
                "epoch_index": index,
                "epoch_id": epoch.epoch_id,
                "safe_duration_ns": safe_duration,
                "planned_output_ns": output_duration,
                "excluded_safe_ns": safe_duration - output_duration,
                "chunk_count": len(epoch_chunks),
                "anchor_type": epoch.anchor_type,
                "terminating_reason": epoch.terminating_reason,
            }
        )
    return {
        "physical_union_ns": interval_duration_ns(physical),
        "audit_safe_union_ns": interval_duration_ns(audit_safe),
        "production_safe_union_ns": interval_duration_ns(production_safe),
        "planned_silver_output_union_ns": interval_duration_ns(output_union),
        "audit_blind_union_ns": interval_duration_ns(audit_cov["blind_intervals"]),
        "audit_boundary_excluded_union_ns": interval_duration_ns(
            audit_cov["boundary_excluded_intervals"]
        ),
        "intentionally_excluded_safe_ns": silver_plan_coverage_equation(
            production_safe, output_union
        )["intentionally_excluded_safe_ns"],
        "expected_bucket_count": bucket_count(output_union),
        "expected_safe_bucket_count": bucket_count(production_safe),
        "epoch_plan_hash": plan.epoch_plan_hash,
        "chunk_plan_hash": chunk_plan_hash(chunks),
        "epoch_count": len(plan.epochs),
        "gap_count": len(plan.gaps),
        "chunk_count": len(chunks),
        "audit_physical_partition": {
            key: audit_partition[key]
            for key in (
                "physical_union_ns",
                "safe_union_ns",
                "blind_union_ns",
                "boundary_excluded_union_ns",
                "unresolved_union_ns",
                "equation_difference_ns",
            )
        },
        "silver_plan_coverage_equation": silver_plan_coverage_equation(
            production_safe, output_union
        ),
        "audit_vs_production": compare_interval_sets(
            audit_safe,
            production_safe,
            label_left="audit_safe_v3",
            label_right="production_epochs",
        ),
        "audit_production_reconciliation": reconcile_audit_production_safe(
            audit_safe=audit_safe,
            production_safe=production_safe,
            production_epochs=plan.epochs,
        ),
        "production_only_decomposition": production_only_decomposition,
        "production_coverage_contract": production_contract,
        "stored_audit_coverage_equation": audit_cov["coverage_equation"],
        "chunk_market_minutes": chunk_minutes,
        "warmup_minutes": warmup,
        "epochs_with_zero_chunks": epochs_with_zero_chunks,
        "excluded_breakdown": excluded_breakdown,
        "epoch_stats_sample": sorted(
            epoch_stats, key=lambda row: row["excluded_safe_ns"], reverse=True
        )[:20],
    }


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
    audit_coverage_path: Path | None = None,
    chunk_market_minutes: int | None = None,
    warmup_minutes: int | None = None,
) -> dict[str, Any]:
    audit_coverage_path = audit_coverage_path or audit_safe_path.with_name(
        "btc_coverage_accounting_v2.json"
    )
    audit = full_numeric_audit(
        client,
        config,
        audit_safe_path=audit_safe_path,
        audit_coverage_path=audit_coverage_path,
        chunk_market_minutes=chunk_market_minutes,
        warmup_minutes=warmup_minutes,
    )
    return {
        **audit,
        "coverage_equation": audit["silver_plan_coverage_equation"],
        "expected_output_buckets": audit["expected_bucket_count"],
        "expected_safe_buckets": audit["expected_safe_bucket_count"],
    }
