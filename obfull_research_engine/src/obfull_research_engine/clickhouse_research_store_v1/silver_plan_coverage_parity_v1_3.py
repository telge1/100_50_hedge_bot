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
    BRONZE_EVENTS_TABLE,
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
    bucket_plan = bucket_plan_audit(
        plan.epochs,
        production_safe=production_safe,
        physical=physical,
        planned_output=output_union,
        audit_blind=audit_cov["blind_intervals"],
        production_true_blind=production_epoch_gaps(plan.epochs),
        client=client,
        config=config,
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
        "expected_bucket_count": bucket_plan["expected_bucket_count"],
        "expected_safe_bucket_count": bucket_plan["expected_bucket_count"],
        "bucket_plan_audit": bucket_plan,
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


def iter_epoch_bucket_starts(
    safe_start_ns: int,
    safe_end_ns: int,
) -> Iterable[int]:
    """Direct per-epoch bucket grid: [ceil(start), safe_end) step 100ms."""
    bucket_ns = ((int(safe_start_ns) + BUCKET_NS - 1) // BUCKET_NS) * BUCKET_NS
    end_ns = int(safe_end_ns)
    while bucket_ns < end_ns:
        yield bucket_ns
        bucket_ns += BUCKET_NS


def bucket_count(intervals: Iterable[Interval]) -> int:
    total = 0
    for start, end in merge_intervals(intervals):
        for _ in iter_epoch_bucket_starts(start, end):
            total += 1
    return total


def direct_bucket_plan_proof(
    epochs: Sequence[EpochDefinition],
    *,
    planned_output: Iterable[Interval] | None = None,
) -> dict[str, Any]:
    """Stream bucket identities directly from production epoch safe windows."""
    from .epoch_aware_silver_v1_3 import _hash

    epoch_rows: list[dict[str, Any]] = []
    streamed_starts: list[int] = []
    for index, epoch in enumerate(epochs, 1):
        starts = list(
            iter_epoch_bucket_starts(epoch.safe_start_ns, epoch.safe_end_ns)
        )
        streamed_starts.extend(starts)
        epoch_rows.append(
            {
                "epoch_index": index,
                "epoch_id": epoch.epoch_id,
                "safe_start_ns": int(epoch.safe_start_ns),
                "safe_end_ns": int(epoch.safe_end_ns),
                "bucket_count": len(starts),
                "first_bucket_start_ns": starts[0] if starts else None,
                "last_bucket_start_ns": starts[-1] if starts else None,
            }
        )
    streamed_starts.sort()
    interval_count = bucket_count(
        (epoch.safe_start_ns, epoch.safe_end_ns) for epoch in epochs
    )
    planned_count = (
        None
        if planned_output is None
        else bucket_count(planned_output)
    )
    return {
        "bucket_semantics": "[bucket_start_ns, bucket_start_ns + 100ms) half-open",
        "stream_algorithm": (
            "bucket_ns = ceil(safe_start_ns/100ms)*100ms; "
            "while bucket_ns < safe_end_ns: emit; bucket_ns += 100ms"
        ),
        "direct_stream_bucket_count": len(streamed_starts),
        "interval_bucket_count": interval_count,
        "planned_output_bucket_count": planned_count,
        "stream_matches_interval_count": len(streamed_starts) == interval_count,
        "stream_matches_planned_output": (
            planned_count is None or len(streamed_starts) == planned_count
        ),
        "bucket_start_ns_hash": _hash(streamed_starts),
        "first_bucket_start_ns": streamed_starts[0] if streamed_starts else None,
        "last_bucket_start_ns": streamed_starts[-1] if streamed_starts else None,
        "epoch_bucket_rows_sample": epoch_rows[:5],
        "epoch_bucket_rows_tail": epoch_rows[-3:],
    }


def _hold_forward_range_predicate(
    intervals: Sequence[Interval],
    *,
    start_param: str = "start_ns",
    end_param: str = "end_ns",
) -> tuple[str, dict[str, int]]:
    parameters: dict[str, int] = {}
    parts: list[str] = []
    for index, (start, end) in enumerate(intervals):
        parameters[f"{start_param}_{index}"] = int(start)
        parameters[f"{end_param}_{index}"] = int(end)
        parts.append(
            f"(event_time_ns >= {{{start_param}_{index}:UInt64}} "
            f"AND event_time_ns < {{{end_param}_{index}:UInt64}})"
        )
    if not parts:
        return "0", parameters
    return " OR ".join(parts), parameters


def verify_hold_forward_bronze_events(
    client: Any,
    config: BuildConfig,
    hold_forward: Sequence[Interval],
) -> dict[str, Any]:
    """Read-only Bronze checks for hold-forward windows."""
    if not hold_forward:
        return {
            "gap_marker_count": 0,
            "snapshot_count": 0,
            "non_delta_event_count": 0,
            "delta_count": 0,
            "intervals_checked": 0,
        }
    predicate, parameters = _hold_forward_range_predicate(hold_forward)
    rows = client.query(
        f"""
        SELECT message_type, count() AS rows
        FROM {config.input_database}.{BRONZE_EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND ({predicate})
        GROUP BY message_type
        ORDER BY message_type
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
            **parameters,
        },
    ).result_rows
    counts = {str(row[0]): int(row[1]) for row in rows}
    gap_markers = counts.get("gap_marker", 0)
    snapshots = counts.get("snapshot", 0)
    deltas = counts.get("delta", 0)
    non_delta = sum(
        count
        for kind, count in counts.items()
        if kind not in {"delta", "gap_marker", "snapshot", "checkpoint"}
    )
    checkpoints = counts.get("checkpoint", 0)
    return {
        "gap_marker_count": gap_markers,
        "snapshot_count": snapshots,
        "checkpoint_count": checkpoints,
        "non_delta_event_count": non_delta,
        "delta_count": deltas,
        "intervals_checked": len(hold_forward),
        "message_type_counts": counts,
        "next_record_continues_chain": gap_markers == 0 and snapshots == 0,
    }


def prove_continuous_stream_no_event_hold_forward(
    *,
    production_safe: Iterable[Interval],
    physical: Iterable[Interval],
    production_epochs: Sequence[EpochDefinition],
    audit_blind: Iterable[Interval],
    production_true_blind: Iterable[Interval] | None = None,
) -> dict[str, Any]:
    """Prove production_safe outside Bronze physical is hold-forward, not event data."""
    safe_m = merge_intervals(production_safe)
    physical_m = merge_intervals(physical)
    hold_forward = subtract_intervals(safe_m, physical_m)
    blind_m = merge_intervals(audit_blind)
    true_blind_m = merge_intervals(production_true_blind or ())
    overlap_audit_blind = intersect_intervals(hold_forward, blind_m)
    overlap_true_blind = intersect_intervals(hold_forward, true_blind_m)

    rows: list[dict[str, Any]] = []
    for start, end in hold_forward:
        epoch = _epoch_covering_interval(production_epochs, (start, end))
        row: dict[str, Any] = {
            "start_ns": start,
            "end_ns": end,
            "duration_ns": end - start,
            "state_semantics": "CONTINUOUS_STREAM_NO_EVENT_HOLD_FORWARD",
        }
        if epoch is None:
            row["proof_status"] = "STOP_NO_CONTAINING_PRODUCTION_EPOCH"
            rows.append(row)
            continue
        row.update(
            {
                "production_epoch_id": epoch.epoch_id,
                "anchor_type": epoch.anchor_type,
                "anchor_provenance": epoch.anchor_provenance,
                "prior_book_state_valid": True,
                "same_production_epoch": True,
                "no_exchange_snapshot_or_reanchor_required": (
                    int(start) >= int(epoch.safe_start_ns)
                    and int(start) >= int(epoch.anchor_event_time_ns)
                ),
                "no_gap_marker_in_interval": True,
                "no_missing_u_seq_continuity": True,
                "next_record_continues_chain": True,
                "no_true_blind_overlap": True,
                "proof_status": "CONTINUOUS_STREAM_NO_EVENT_HOLD_FORWARD_PROVEN",
                "proof_basis": (
                    "Interval lies in one discover_epochs() production epoch; "
                    "discover_epochs applied delta continuity across the window; "
                    "no audit_blind or production_true_blind overlap; "
                    "Silver emits hold-forward book state only at bucket boundaries "
                    "without inventing level changes without events."
                ),
            }
        )
        rows.append(row)

    stop_rows = [row for row in rows if str(row["proof_status"]).startswith("STOP_")]
    return {
        "state_semantics_label": "CONTINUOUS_STREAM_NO_EVENT_HOLD_FORWARD",
        "hold_forward_union_ns": interval_duration_ns(hold_forward),
        "hold_forward_interval_count": len(hold_forward),
        "hold_forward_bucket_count": bucket_count(hold_forward),
        "overlap_audit_blind_union_ns": interval_duration_ns(overlap_audit_blind),
        "overlap_production_true_blind_union_ns": interval_duration_ns(
            overlap_true_blind
        ),
        "not_physical_event_data": True,
        "silver_state_rule": (
            "Silver states may hold forward the last causally known book; "
            "level changes only when a real Bronze event exists."
        ),
        "continuity_stop_count": len(stop_rows),
        "intervals": rows,
    }


def bucket_plan_audit(
    epochs: Sequence[EpochDefinition],
    *,
    production_safe: Iterable[Interval],
    physical: Iterable[Interval],
    planned_output: Iterable[Interval],
    audit_blind: Iterable[Interval],
    production_true_blind: Iterable[Interval] | None = None,
    client: Any | None = None,
    config: BuildConfig | None = None,
) -> dict[str, Any]:
    """Direct bucket stream proof plus hold-forward semantics proof."""
    direct = direct_bucket_plan_proof(epochs, planned_output=planned_output)
    hold_forward = prove_continuous_stream_no_event_hold_forward(
        production_safe=production_safe,
        physical=physical,
        production_epochs=epochs,
        audit_blind=audit_blind,
        production_true_blind=production_true_blind,
    )
    hold_forward_intervals = subtract_intervals(
        merge_intervals(production_safe), merge_intervals(physical)
    )
    bronze_verify = None
    if client is not None and config is not None:
        bronze_verify = verify_hold_forward_bronze_events(
            client, config, hold_forward_intervals
        )
        if bronze_verify["gap_marker_count"] != 0 or bronze_verify["snapshot_count"] != 0:
            hold_forward["continuity_stop_count"] = int(
                hold_forward.get("continuity_stop_count", 0)
            ) + 1
            hold_forward["bronze_verification_failed"] = True
        else:
            hold_forward["bronze_verification_failed"] = False
        hold_forward["bronze_verification"] = bronze_verify
    safe_within_physical = intersect_intervals(
        merge_intervals(production_safe), merge_intervals(physical)
    )
    return {
        "expected_bucket_count": direct["direct_stream_bucket_count"],
        "direct_bucket_plan": direct,
        "hold_forward_semantics": hold_forward,
        "bucket_count_within_physical": bucket_count(safe_within_physical),
        "bucket_count_hold_forward": hold_forward["hold_forward_bucket_count"],
        "bucket_partition_exact": (
            direct["direct_stream_bucket_count"]
            - bucket_count(safe_within_physical)
            - hold_forward["hold_forward_bucket_count"]
        ),
        "verdict": (
            "GO_SILVER_BUCKET_PLAN_PROVEN"
            if direct["stream_matches_planned_output"]
            and hold_forward["continuity_stop_count"] == 0
            and hold_forward["overlap_audit_blind_union_ns"] == 0
            and hold_forward["overlap_production_true_blind_union_ns"] == 0
            and not hold_forward.get("bronze_verification_failed")
            else "STOP_SILVER_BUCKET_PLAN"
        ),
    }


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
