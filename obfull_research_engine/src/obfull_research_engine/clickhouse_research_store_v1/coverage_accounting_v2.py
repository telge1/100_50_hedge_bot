"""Streaming BTC Full-OB coverage accounting and cross-segment order audit.

This module is read-only.  It deliberately separates content identity (SHA-256)
from temporal order and performs all accounting in integer nanoseconds.
"""

from __future__ import annotations

import hashlib
import json
import resource
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Iterator

from .gap_semantics_audit import iter_records_stream
from .helpers import iso_to_ns_exact

Interval = tuple[int, int]
RSS_STOP_KB = 1_500 * 1024
MICROSECOND_NS = 1_000


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Return sorted, disjoint half-open intervals, merging adjacency."""
    merged: list[Interval] = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals if int(a) < int(b)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(base: Iterable[Interval], remove: Iterable[Interval]) -> list[Interval]:
    """Subtract half-open intervals after canonical union."""
    bases, removes = merge_intervals(base), merge_intervals(remove)
    out: list[Interval] = []
    j = 0
    for start, end in bases:
        cursor = start
        while j < len(removes) and removes[j][1] <= cursor:
            j += 1
        k = j
        while k < len(removes) and removes[k][0] < end:
            r_start, r_end = removes[k]
            if r_start > cursor:
                out.append((cursor, min(end, r_start)))
            cursor = max(cursor, r_end)
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            out.append((cursor, end))
    return out


def intersect_intervals(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    """Intersect two canonical half-open interval sets."""
    a, b = merge_intervals(left), merge_intervals(right)
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start, end = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if start < end:
            out.append((start, end))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return out


def interval_duration_ns(intervals: Iterable[Interval]) -> int:
    return sum(end - start for start, end in merge_intervals(intervals))


def reconcile_old_accounting(
    *,
    old_physical_hull_ns: int,
    old_safe_sum_ns: int,
    old_blind_sum_ns: int,
    physical_union_ns: int,
    safe_union_ns: int,
    blind_union_ns: int,
    boundary_excluded_union_ns: int,
) -> dict[str, int]:
    """Explain the old residual using corrected union accounting."""
    old_residual_ns = old_physical_hull_ns - old_safe_sum_ns - old_blind_sum_ns
    result = {
        "old_convex_hull_physical_ns": old_physical_hull_ns,
        "old_raw_safe_sum_ns": old_safe_sum_ns,
        "old_signed_blind_sum_ns": old_blind_sum_ns,
        "old_unexplained_residual_ns": old_residual_ns,
        "convex_hull_minus_physical_union_ns": old_physical_hull_ns - physical_union_ns,
        "old_blind_overstatement_vs_true_blind_union_ns": old_blind_sum_ns - blind_union_ns,
        "old_safe_sum_outside_physical_union_ns": old_safe_sum_ns - safe_union_ns,
        "correct_boundary_excluded_union_ns": boundary_excluded_union_ns,
    }
    result["identity_check_ns"] = (
        result["convex_hull_minus_physical_union_ns"]
        + boundary_excluded_union_ns
        - result["old_blind_overstatement_vs_true_blind_union_ns"]
        - result["old_safe_sum_outside_physical_union_ns"]
        - old_residual_ns
    )
    return result


def partition_coverage(
    *,
    physical: Iterable[Interval],
    safe: Iterable[Interval],
    blind: Iterable[Interval],
    unresolved: Iterable[Interval] = (),
) -> dict[str, list[Interval] | int]:
    """Create a disjoint, exhaustive priority partition of physical time.

    Priority is safe, true blind, unresolved, then boundary-excluded.  Every
    category is clipped to the physical union before subtraction.
    """
    physical_u = merge_intervals(physical)
    safe_u = intersect_intervals(physical_u, safe)
    remaining = subtract_intervals(physical_u, safe_u)
    blind_u = intersect_intervals(remaining, blind)
    remaining = subtract_intervals(remaining, blind_u)
    unresolved_u = intersect_intervals(remaining, unresolved)
    boundary_u = subtract_intervals(remaining, unresolved_u)
    durations = {
        "physical_union_ns": interval_duration_ns(physical_u),
        "safe_union_ns": interval_duration_ns(safe_u),
        "blind_union_ns": interval_duration_ns(blind_u),
        "boundary_excluded_union_ns": interval_duration_ns(boundary_u),
        "unresolved_union_ns": interval_duration_ns(unresolved_u),
    }
    durations["equation_difference_ns"] = durations["physical_union_ns"] - sum(
        durations[key]
        for key in (
            "safe_union_ns",
            "blind_union_ns",
            "boundary_excluded_union_ns",
            "unresolved_union_ns",
        )
    )
    return {
        "physical": physical_u,
        "safe": safe_u,
        "blind": blind_u,
        "boundary_excluded": boundary_u,
        "unresolved": unresolved_u,
        **durations,
    }


def segment_temporal_order_key(summary: dict[str, Any]) -> tuple[int, int, int, str]:
    """Collector-semantic order; SHA is only a final deterministic tie-breaker."""
    return (
        int(summary["segment_start_ns"]),
        int(summary["first_archive_time_ns"]),
        int(summary["first_receive_time_ns"]),
        str(summary["segment_sha256"]),
    )


def record_identity_key(segment_sha256: str, record_ordinal: int) -> tuple[str, int]:
    return str(segment_sha256), int(record_ordinal)


def record_apply_order_key(segment_chain_index: int, record_ordinal: int) -> tuple[int, int]:
    return int(segment_chain_index), int(record_ordinal)


def _rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_u_seq(record: dict[str, Any]) -> tuple[int | None, int | None]:
    payload = record.get("original_payload") if isinstance(record.get("original_payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    u = record.get("u") if record.get("u") is not None else data.get("u")
    seq = record.get("seq") if record.get("seq") is not None else data.get("seq")
    return (int(u) if u is not None else None, int(seq) if seq is not None else None)


def scan_segment(
    path: Path,
    manifest: dict[str, Any],
    *,
    retain_payload_hashes: bool,
) -> tuple[dict[str, Any], set[str]]:
    """Stream one segment and retain only its compact order/continuity summary."""
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    min_event = max_event = None
    anchors: Counter[str] = Counter()
    payload_hashes: set[str] = set()
    count = 0
    for record, ordinal in iter_records_stream(path):
        count += 1
        if first is None:
            first = record
        last = record
        event_ns = record.get("event_time_ns")
        if event_ns is not None:
            value = int(event_ns)
            min_event = value if min_event is None else min(min_event, value)
            max_event = value if max_event is None else max(max_event, value)
        message_type = str(record.get("message_type") or "")
        payload = record.get("original_payload") if isinstance(record.get("original_payload"), dict) else {}
        if message_type == "snapshot":
            anchors["exchange_snapshot"] += 1
        elif message_type == "checkpoint":
            anchors[str(payload.get("checkpoint_reason") or "checkpoint_unknown")] += 1
        if retain_payload_hashes and record.get("payload_sha256"):
            payload_hashes.add(str(record["payload_sha256"]))
    if first is None or last is None:
        raise RuntimeError(f"empty segment: {path}")
    first_u, first_seq = _record_u_seq(first)
    last_u, last_seq = _record_u_seq(last)
    actual_sha = _file_sha256(path)
    expected_sha = str(manifest.get("segment_sha256") or "")
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(f"segment SHA mismatch: {path}")
    summary = {
        "path": str(path),
        "file_name": path.name,
        "utc_hour": manifest.get("utc_hour"),
        "segment_start_ns": iso_to_ns_exact(str(manifest["segment_start"])),
        "segment_end_ns": iso_to_ns_exact(str(manifest["segment_end"])),
        "manifest_first_event_ns": iso_to_ns_exact(str(manifest["first_event_time"])),
        "manifest_last_event_ns": iso_to_ns_exact(str(manifest["last_event_time"])),
        "first_record_event_ns": int(first.get("event_time_ns") or 0),
        "last_record_event_ns": int(last.get("event_time_ns") or 0),
        "min_record_event_ns": int(min_event or 0),
        "max_record_event_ns": int(max_event or 0),
        "first_receive_time_ns": int(first.get("receive_time_ns") or 0),
        "last_receive_time_ns": int(last.get("receive_time_ns") or 0),
        "first_archive_time_ns": int(first.get("archive_time_ns") or first.get("receive_time_ns") or 0),
        "last_archive_time_ns": int(last.get("archive_time_ns") or last.get("receive_time_ns") or 0),
        "first_record_type": first.get("message_type"),
        "last_record_type": last.get("message_type"),
        "first_u": first_u,
        "last_u": last_u,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "archive_instance_id": manifest.get("archive_instance_id"),
        "collector_instance_id": manifest.get("collector_instance_id"),
        "completion_status": manifest.get("completion_status"),
        "message_count_manifest": int(manifest.get("message_count") or 0),
        "record_count_scanned": count,
        "segment_sha256": actual_sha,
        "anchor_counts": dict(anchors),
    }
    return summary, payload_hashes


def _interval_rows(intervals: list[Interval], category: str) -> list[dict[str, Any]]:
    return [
        {
            "category": category,
            "start_ns": start,
            "end_ns": end,
            "duration_ns": end - start,
        }
        for start, end in intervals
    ]


def _boundary_rows(intervals: list[Interval], physical: list[Interval]) -> list[dict[str, Any]]:
    physical_starts = {start for start, _ in physical}
    physical_ends = {end for _, end in physical}
    rows = _interval_rows(intervals, "boundary_excluded")
    for row in rows:
        if row["start_ns"] in physical_starts:
            row["cause"] = "SEGMENT_PREFIX_BEFORE_SAFE_ANCHOR"
        elif row["end_ns"] in physical_ends:
            row["cause"] = "SEGMENT_SUFFIX_AFTER_LAST_SAFE_RECORD"
        else:
            row["cause"] = "INTRA_SEGMENT_NONBLIND_BOUNDARY"
    return rows


def _derive_epoch_intervals(
    epoch_path: Path,
) -> tuple[list[Interval], list[Interval], list[dict[str, Any]], str]:
    epochs: list[dict[str, Any]] = []
    with epoch_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                epochs.append(json.loads(line))
    epochs.sort(key=lambda row: (int(row["safe_start_ns"]), int(row["safe_end_ns"])))
    if not epochs:
        raise RuntimeError("no replay epochs")
    safe = [(int(row["safe_start_ns"]), int(row["safe_end_ns"])) for row in epochs]
    blind: list[Interval] = []
    cursor = int(epochs[0]["safe_end_ns"])
    previous = epochs[0]
    for row in epochs[1:]:
        start = int(row["safe_start_ns"])
        if start > cursor and previous.get("terminating_reason") in {"gap_marker", "delta_u_gap"}:
            blind.append((cursor, start))
        if int(row["safe_end_ns"]) >= cursor:
            cursor = int(row["safe_end_ns"])
            previous = row
    cohort_end_hour = max(str(row["epoch_id"])[:20] for row in epochs)
    return safe, blind, epochs, cohort_end_hour


def normalize_segment_end_precision(
    epochs: list[dict[str, Any]], segment_last_event_ns: Iterable[int]
) -> tuple[list[Interval], int]:
    """Repair v2's float-to-ns drift at ends explicitly sourced from manifests.

    ``segment_end`` epochs semantically end at the last physical record.  The
    old helper converted a microsecond ``datetime`` through float seconds,
    causing sub-microsecond errors.  No other endpoint is snapped.
    """
    exact_ends = sorted(set(int(value) for value in segment_last_event_ns))
    normalized: list[Interval] = []
    corrected = 0
    for epoch in epochs:
        start, end = int(epoch["safe_start_ns"]), int(epoch["safe_end_ns"])
        if epoch.get("terminating_reason") == "segment_end" and exact_ends:
            index = bisect_left(exact_ends, end)
            candidates = exact_ends[max(0, index - 1) : min(len(exact_ends), index + 1)]
            nearest = min(candidates, key=lambda value: abs(value - end))
            if abs(nearest - end) < MICROSECOND_NS:
                corrected += nearest != end
                end = nearest
        normalized.append((start, end))
    return normalized, corrected


def derive_blind_intervals(
    epochs: list[dict[str, Any]], normalized_safe: list[Interval]
) -> list[Interval]:
    blind: list[Interval] = []
    cursor = normalized_safe[0][1]
    previous = epochs[0]
    for epoch, (start, end) in zip(epochs[1:], normalized_safe[1:]):
        if start > cursor and previous.get("terminating_reason") in {"gap_marker", "delta_u_gap"}:
            blind.append((cursor, start))
        if end >= cursor:
            cursor = end
            previous = epoch
    return blind


def _build_overlap_reports(
    groups: dict[str, list[dict[str, Any]]],
    payload_hashes: dict[str, set[str]],
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for utc_hour, summaries in sorted(groups.items()):
        if len(summaries) < 2:
            continue
        ordered = sorted(summaries, key=segment_temporal_order_key)
        pairs = []
        for left, right in combinations(ordered, 2):
            archive_start = max(left["first_archive_time_ns"], right["first_archive_time_ns"])
            archive_end = min(left["last_archive_time_ns"], right["last_archive_time_ns"])
            event_start = max(left["min_record_event_ns"], right["min_record_event_ns"])
            event_end = min(left["max_record_event_ns"], right["max_record_event_ns"])
            duplicates = payload_hashes[left["segment_sha256"]] & payload_hashes[right["segment_sha256"]]
            pairs.append(
                {
                    "left_segment_sha256": left["segment_sha256"],
                    "right_segment_sha256": right["segment_sha256"],
                    "causal_archive_overlap_ns": max(0, archive_end - archive_start),
                    "event_range_overlap_ns": max(0, event_end - event_start),
                    "left_records_in_causal_overlap": 0 if archive_start >= archive_end else None,
                    "right_records_in_causal_overlap": 0 if archive_start >= archive_end else None,
                    "duplicate_original_payload_count": len(duplicates),
                    "left_last_u_seq": [left["last_u"], left["last_seq"]],
                    "right_first_u_seq": [right["first_u"], right["first_seq"]],
                    "collector_restart": left["archive_instance_id"] != right["archive_instance_id"],
                }
            )
        reports.append(
            {
                "utc_hour": utc_hour,
                "segment_count": len(ordered),
                "segments": ordered,
                "pairs": pairs,
                "resolution": (
                    "KEEP_ALL_AS_CAUSALLY_SEQUENTIAL_SEGMENT_SHARDS"
                    if all(pair["causal_archive_overlap_ns"] == 0 for pair in pairs)
                    else "STOP_OVERLAPPING_CAUSAL_SEGMENTS_REQUIRE_RESOLUTION"
                ),
                "canonical_sha_chain": [row["segment_sha256"] for row in ordered],
            }
        )
    return reports


def _seconds(ns: int) -> str:
    whole, fraction = divmod(int(ns), 1_000_000_000)
    return f"{whole}.{fraction:09d}"


def run_coverage_accounting_v2(
    *,
    archive_root: Path,
    run_dir: Path,
    progress_every: int = 10,
) -> dict[str, Any]:
    """Run the bounded one-worker audit and write the four requested artifacts."""
    started = time.monotonic()
    epoch_path = run_dir / "btc_replay_epochs_v1.jsonl"
    prior_gap_path = run_dir / "btc_full_ob_gap_semantics_audit.json"
    detail_path = run_dir / "detail_parity_report.json"
    _, _, epochs, cohort_end_hour = _derive_epoch_intervals(epoch_path)

    discovered: list[tuple[Path, dict[str, Any]]] = []
    hour_counts: Counter[str] = Counter()
    for path in (archive_root / "BTCUSDT").rglob("*_full_ob_continuous_raw_archive_v1.ndjson.zst"):
        manifest_path = Path(str(path) + ".manifest.json")
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hour = str(manifest.get("utc_hour") or "")
        if hour <= cohort_end_hour:
            discovered.append((path, manifest))
            hour_counts[hour] += 1
    repeated_hours = {hour for hour, count in hour_counts.items() if count > 1}

    summaries: list[dict[str, Any]] = []
    payload_hashes: dict[str, set[str]] = {}
    for index, (path, manifest) in enumerate(discovered, 1):
        summary, hashes = scan_segment(
            path,
            manifest,
            retain_payload_hashes=str(manifest.get("utc_hour")) in repeated_hours,
        )
        summaries.append(summary)
        if hashes:
            payload_hashes[summary["segment_sha256"]] = hashes
        if index % progress_every == 0 or index == len(discovered):
            print(f"progress segments={index}/{len(discovered)} peak_rss_kb={_rss_kb()}", flush=True)
        if _rss_kb() > RSS_STOP_KB:
            raise RuntimeError(f"STOP_RSS_LIMIT_EXCEEDED: peak_rss_kb={_rss_kb()}")

    canonical = sorted(summaries, key=segment_temporal_order_key)
    for index, row in enumerate(canonical):
        row["segment_chain_index"] = index
        row["segment_temporal_order_key"] = list(segment_temporal_order_key(row))
    sha_sorted = sorted(summaries, key=lambda row: row["segment_sha256"])
    sha_changes_order = [r["segment_sha256"] for r in sha_sorted] != [
        r["segment_sha256"] for r in canonical
    ]

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summaries:
        groups[str(row["utc_hour"])].append(row)
    overlap_reports = _build_overlap_reports(groups, payload_hashes)
    unresolved_order = any(
        row["resolution"] == "STOP_OVERLAPPING_CAUSAL_SEGMENTS_REQUIRE_RESOLUTION"
        for row in overlap_reports
    )

    safe_candidates, corrected_segment_ends = normalize_segment_end_precision(
        epochs, (row["last_record_event_ns"] for row in canonical)
    )
    blind_candidates = derive_blind_intervals(epochs, safe_candidates)
    physical_candidates = [
        (row["first_record_event_ns"], row["last_record_event_ns"]) for row in canonical
    ]
    partition = partition_coverage(
        physical=physical_candidates,
        safe=safe_candidates,
        blind=blind_candidates,
    )
    if int(partition["equation_difference_ns"]) != 0:
        verdict = "STOP_BTC_FULL_OB_COVERAGE_ACCOUNTING_MISMATCH"
    elif unresolved_order:
        verdict = "STOP_BTC_FULL_OB_SEGMENT_ORDER_UNRESOLVED"
    else:
        verdict = "BTC_FULL_OB_COVERAGE_ACCOUNTING_CORRECTED"

    prior_gap = json.loads(prior_gap_path.read_text(encoding="utf-8"))
    old_physical_ns = (
        max(row["manifest_last_event_ns"] for row in canonical)
        - min(row["manifest_first_event_ns"] for row in canonical)
    )
    old_safe_ns = sum(end - start for start, end in safe_candidates)
    old_blind_ns = int(prior_gap["blind_window_stats_ms"]["total_ms"]) * 1_000_000
    reconciliation = reconcile_old_accounting(
        old_physical_hull_ns=old_physical_ns,
        old_safe_sum_ns=old_safe_ns,
        old_blind_sum_ns=old_blind_ns,
        physical_union_ns=int(partition["physical_union_ns"]),
        safe_union_ns=int(partition["safe_union_ns"]),
        blind_union_ns=int(partition["blind_union_ns"]),
        boundary_excluded_union_ns=int(partition["boundary_excluded_union_ns"]),
    )

    parity = json.loads(detail_path.read_text(encoding="utf-8"))
    states = parity["regressions"]["states_100ms"]
    episode1 = {
        "level_changes_reference": parity["level_change_parity"]["reference_rows"],
        "level_changes_actual": parity["level_change_parity"]["clickhouse_rows"],
        "reference_lc_hash": parity["level_change_parity"]["reference_hash"],
        "actual_lc_hash": parity["level_change_parity"]["clickhouse_hash"],
        "states_100ms": states["compared_buckets"],
        "status": states["status"],
    }
    contract = {
        "verdict": (
            "STOP_BTC_FULL_OB_SEGMENT_ORDER_UNRESOLVED"
            if unresolved_order
            else "BTC_FULL_OB_SEGMENT_ORDER_PROVEN"
        ),
        "segment_identity_key": "segment_sha256 (content identity/integrity only)",
        "segment_temporal_order_key": (
            "(manifest.segment_start_ns, actual_first_archive_time_ns, "
            "actual_first_receive_time_ns, segment_sha256_tie_breaker)"
        ),
        "record_identity_key": "(segment_sha256, record_ordinal)",
        "record_apply_order_key": "(canonical_segment_chain_index, record_ordinal)",
        "event_bucket_time_key": "event_time_ns; causal emission may not reorder delta application",
        "rules": [
            "SHA-256 never establishes temporal order.",
            "record_ordinal is scoped to one segment.",
            "same UTC-hour shards are ordered by collector archive/receive chronology.",
            "causally overlapping non-identical segments are a hard stop.",
            "collector restart changes provenance, not temporal sort priority.",
        ],
        "current_code_findings": {
            "segment_discovery": "gap_semantics_audit.list_closed_btc_segments: filename glob then corrected sort",
            "current_audit_sort": "(manifest.first_event_time, archive_instance_id)",
            "record_iteration": "gap_semantics_audit.iter_records_stream: physical NDJSON line order",
            "silver_replay_sort": "silver_replay.sort_bronze_source_order: UNSAFE multi-segment SHA-primary sort",
            "silver_query_sort": "silver_builder.load_bronze_window: UNSAFE multi-segment SHA-primary sort",
            "safe_interval_builder": "reanchor_replay_order_audit.compute_epochs_and_safe_intervals_v2",
            "epoch_builder": "reanchor_replay_order_audit.compute_epochs_and_safe_intervals_v2",
        },
        "cohort_end_hour": cohort_end_hour,
        "segment_count": len(canonical),
        "utc_hour_count": len(groups),
        "sha_sort_changes_temporal_order": sha_changes_order,
        "canonical_segments": canonical,
        "repeated_utc_hours": overlap_reports,
    }

    accounting = {
        "verdict": verdict,
        "coverage_equation": {
            key: partition[key]
            for key in (
                "physical_union_ns",
                "safe_union_ns",
                "blind_union_ns",
                "boundary_excluded_union_ns",
                "unresolved_union_ns",
                "equation_difference_ns",
            )
        },
        "coverage_seconds_exact": {
            key.replace("_ns", "_s"): _seconds(int(partition[key]))
            for key in (
                "physical_union_ns",
                "safe_union_ns",
                "blind_union_ns",
                "boundary_excluded_union_ns",
                "unresolved_union_ns",
            )
        },
        "safe_pct": round(
            100 * int(partition["safe_union_ns"]) / int(partition["physical_union_ns"]), 9
        ),
        "interval_counts": {
            "physical": len(partition["physical"]),
            "safe": len(partition["safe"]),
            "blind": len(partition["blind"]),
            "boundary_excluded": len(partition["boundary_excluded"]),
            "unresolved": len(partition["unresolved"]),
        },
        "old_489_second_reconciliation": reconciliation,
        "boundary_excluded_intervals": _boundary_rows(
            partition["boundary_excluded"], partition["physical"]
        ),
        "blind_intervals": _interval_rows(partition["blind"], "true_blind"),
        "regressions": {
            "safe_intervals_disjoint_sorted": partition["safe"] == merge_intervals(partition["safe"]),
            "no_negative_duration": all(
                start < end
                for category in ("physical", "safe", "blind", "boundary_excluded", "unresolved")
                for start, end in partition[category]
            ),
            "coverage_equation_exact": partition["equation_difference_ns"] == 0,
            "sha_does_not_define_temporal_order": sha_changes_order,
            "duplicate_hours_controlled": not unresolved_order,
            "segment_end_float_drift_corrections": corrected_segment_ends,
            "episode1": episode1,
        },
        "audit_meta": {
            "elapsed_s": round(time.monotonic() - started, 3),
            "peak_rss_kb": _rss_kb(),
            "worker_count": 1,
            "streaming": True,
            "segment_count": len(canonical),
        },
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "btc_coverage_accounting_v2.json").write_text(
        json.dumps(accounting, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "btc_segment_order_contract_v1.json").write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )
    with (run_dir / "btc_full_ob_safe_intervals_v3.jsonl").open("w", encoding="utf-8") as out:
        for index, (start, end) in enumerate(partition["safe"], 1):
            out.write(
                json.dumps(
                    {
                        "safe_interval_id": index,
                        "start_ns": start,
                        "end_ns": end,
                        "duration_ns": end - start,
                        "interval_semantics": "[start_ns,end_ns)",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    safe_v3_sha256 = _file_sha256(run_dir / "btc_full_ob_safe_intervals_v3.jsonl")
    accounting["regressions"]["safe_intervals_v3_sha256"] = safe_v3_sha256
    contract["canonical_segment_chain_sha256"] = hashlib.sha256(
        "\n".join(row["segment_sha256"] for row in canonical).encode()
    ).hexdigest()
    (run_dir / "btc_coverage_accounting_v2.json").write_text(
        json.dumps(accounting, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "btc_segment_order_contract_v1.json").write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )
    md = f"""# BTC Coverage Accounting v2

**Verdict:** `{verdict}`

## Exakte disjunkte Coverage-Partition

`physical_union_ns = safe_union_ns + blind_union_ns + boundary_excluded_union_ns + unresolved_union_ns`

- physical_union: {partition['physical_union_ns']} ns ({_seconds(int(partition['physical_union_ns']))} s)
- safe_union: {partition['safe_union_ns']} ns ({_seconds(int(partition['safe_union_ns']))} s)
- true_blind_union: {partition['blind_union_ns']} ns ({_seconds(int(partition['blind_union_ns']))} s)
- boundary_excluded_union: {partition['boundary_excluded_union_ns']} ns ({_seconds(int(partition['boundary_excluded_union_ns']))} s)
- unresolved_union: {partition['unresolved_union_ns']} ns ({_seconds(int(partition['unresolved_union_ns']))} s)
- Differenz: **{partition['equation_difference_ns']} ns**
- Safe Coverage: **{accounting['safe_pct']} %**

## Erklärung der alten ~489 Sekunden

- Alte konvexe Hülle minus physische Segment-Union: {reconciliation['convex_hull_minus_physical_union_ns']} ns.
- Alte Blindzeit-Überzeichnung gegenüber disjunkter Blind-Union: {reconciliation['old_blind_overstatement_vs_true_blind_union_ns']} ns.
- Korrekte Boundary-Exclusion innerhalb der physischen Union: {reconciliation['correct_boundary_excluded_union_ns']} ns.
- Alte Safe-Summe außerhalb der physischen Union: {reconciliation['old_safe_sum_outside_physical_union_ns']} ns.
- Alte Restdifferenz: {reconciliation['old_unexplained_residual_ns']} ns.
- Reconciliation-Differenz: **{reconciliation['identity_check_ns']} ns**.

Die alte Blindzeit war kein Union-Wert: negative Out-of-order-Zeitdifferenzen und der
falsche 590,801-s-Segmentstart-Alarm wurden vorzeichenbehaftet summiert.

## Segment- und Record-Vertrag

- Segmentidentität: `segment_sha256` (nur Inhalt/Integrität).
- Segmentzeit: `(segment_start_ns, first_archive_time_ns, first_receive_time_ns, sha_tie_breaker)`.
- Recordidentität: `(segment_sha256, record_ordinal)`.
- Delta-Apply: `(canonical_segment_chain_index, record_ordinal)`.
- Bucket-Zeit: `event_time_ns`, ohne Delta-Reordering.
- SHA-Sortierung verändert die tatsächliche Segmentfolge: **{sha_changes_order}**.
- Mehrfachstunden: {len(overlap_reports)}; alle kontrolliert aufgelöst: **{not unresolved_order}**.

## Episode 1

- Level-Changes: {episode1['level_changes_actual']}/{episode1['level_changes_reference']}
- LC-Hash identisch: {episode1['actual_lc_hash'] == episode1['reference_lc_hash']}
- states_100ms: {episode1['states_100ms']}/600
- Status: `{episode1['status']}`

## Ressourcen

- Laufzeit: {accounting['audit_meta']['elapsed_s']} s
- Peak RSS: {accounting['audit_meta']['peak_rss_kb']} KiB
- Ein Worker, Segment-Streaming, Fortschritt je zehn Segmente.
"""
    (run_dir / "BTC_COVERAGE_ACCOUNTING_V2.md").write_text(md, encoding="utf-8")
    return {"accounting": accounting, "contract": contract}
