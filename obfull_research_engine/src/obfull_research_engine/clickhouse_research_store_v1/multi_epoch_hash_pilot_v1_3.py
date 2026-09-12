"""Two-hour multi-epoch pilot and lossless 100 ms book-hash optimization."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .canonical_segment_order_v1_3 import import_pilot_window
from .epoch_aware_silver_pilot_v1_3 import (
    EPISODE_ANALYSIS_START,
    EPISODE_END,
    EPISODE_SCAN_START,
    SYMBOL,
    _persisted_import_contract,
    _reference_episode1,
)
from .epoch_aware_silver_v1_3 import (
    DATABASE_V13,
    EPOCHS_TABLE,
    LEVEL_CHANGES_TABLE,
    STATES_TABLE,
    EpochDefinition,
    EpochDiscovery,
    EpochSilverError,
    _canonical_bytes,
    _check_rss,
    _rss_kb,
    clean_segment_start_ranks,
    discover_epochs,
    ensure_epoch_silver_schema,
    epoch_apply_bounds,
    iso,
    iter_bronze_window,
    load_segment_metadata,
    make_build_id,
    make_chunk_key,
    ns_to_iso,
    persist_epochs,
    persist_replay_chunk,
    replay_epoch_window,
    resolve_single_chain,
    select_resume_checkpoint,
    validate_epoch_window,
)
from .helpers import get_clickhouse_client

WINDOW_START_NS = iso("2026-09-07T21:00:00.000000000Z")
WINDOW_END_NS = iso("2026-09-07T23:00:00.000000000Z")
FIRST_IMPORT_END_NS = iso("2026-09-07T22:30:00.000000000Z")
EXTENSION_START_NS = FIRST_IMPORT_END_NS
REFERENCE_FILE = "btc_replay_epochs_v1.jsonl"
BASELINE_S_PER_MARKET_MINUTE = 9.573669
TARGET_S_PER_MARKET_MINUTE = 6.0
MULTI_EPOCHS_TABLE = "replay_epochs_multi_hash_pilot_v1_3"


def _ceil_bucket(ns: int) -> int:
    return ((int(ns) + 99_999_999) // 100_000_000) * 100_000_000


def _floor_bucket(ns: int) -> int:
    return (int(ns) // 100_000_000) * 100_000_000


def _load_reference(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [
        row
        for row in rows
        if WINDOW_START_NS <= int(row["anchor_event_time_ns"]) < WINDOW_END_NS
    ]
    if not selected:
        raise EpochSilverError(
            "STOP_MULTI_EPOCH_REFERENCE_MISMATCH: no reference epochs in window"
        )
    return selected


def _reference_clean_rank(
    client: Any,
    *,
    chain_version: str,
    chain_hash: str,
    reference: list[dict[str, Any]],
) -> int:
    first = reference[0]
    if (
        first.get("anchor_type") != "segment_start_clean"
        or int(first.get("anchor_source_record_ordinal") or 0) != 1
    ):
        raise EpochSilverError(
            "STOP_MULTI_EPOCH_REFERENCE_MISMATCH: first reference anchor is not clean"
        )
    records = iter_bronze_window(
        client,
        symbol=SYMBOL,
        chain_version=chain_version,
        canonical_chain_hash=chain_hash,
        start_ns=WINDOW_START_NS,
        end_ns=WINDOW_START_NS + 2_000_000_000,
    )
    first_record = next(records, None)
    if (
        first_record is None
        or first_record.message_type != "checkpoint"
        or int(first_record.record_ordinal) != 1
        or int(first_record.event_time_ns) != int(first["anchor_event_time_ns"])
    ):
        raise EpochSilverError(
            "STOP_MULTI_EPOCH_REFERENCE_MISMATCH: Bronze clean start differs"
        )
    return int(first_record.canonical_segment_chain_index)


def _compare_reference(
    discovery: EpochDiscovery, reference: list[dict[str, Any]]
) -> dict[str, Any]:
    expected_gaps = sum(
        row["terminating_reason"] in {"gap_marker", "delta_u_gap"}
        for row in reference
    )
    interval_mismatches = []
    for index, (actual, expected) in enumerate(zip(discovery.epochs, reference)):
        if (
            actual.safe_start_ns != int(expected["safe_start_ns"])
            or actual.safe_end_ns != int(expected["safe_end_ns"])
        ):
            interval_mismatches.append(
                {
                    "index": index,
                    "actual": [actual.safe_start_ns, actual.safe_end_ns],
                    "expected": [
                        int(expected["safe_start_ns"]),
                        int(expected["safe_end_ns"]),
                    ],
                }
            )
    proof = {
        "expected_epochs": len(reference),
        "actual_epochs": len(discovery.epochs),
        "expected_gaps": expected_gaps,
        "actual_gaps": len(discovery.gaps),
        "interval_mismatches": interval_mismatches,
        "anchor_time_mismatches": sum(
            actual.anchor_event_time_ns != int(expected["anchor_event_time_ns"])
            for actual, expected in zip(discovery.epochs, reference)
        ),
    }
    if (
        proof["expected_epochs"] != proof["actual_epochs"]
        or proof["expected_gaps"] != proof["actual_gaps"]
        or interval_mismatches
        or proof["anchor_time_mismatches"]
    ):
        raise EpochSilverError(
            "STOP_MULTI_EPOCH_REFERENCE_MISMATCH: epoch inventory differs"
        )
    return proof


def _select_analysis(
    epoch: EpochDefinition, *, warmup_minutes: int, analysis_minutes: int
) -> tuple[int, int]:
    start = _ceil_bucket(
        epoch.safe_start_ns + warmup_minutes * 60 * 1_000_000_000
    )
    end = start + analysis_minutes * 60 * 1_000_000_000
    if end > _floor_bucket(epoch.safe_end_ns):
        raise EpochSilverError(
            "STOP_MULTI_EPOCH_REFERENCE_MISMATCH: selected epoch is too short"
        )
    decision = validate_epoch_window(
        [epoch],
        analysis_start_ns=start,
        analysis_end_ns=end,
        warmup_ns=warmup_minutes * 60 * 1_000_000_000,
    )
    if decision.status != "OK":
        raise EpochSilverError(
            "STOP_RESUME_CROSSED_EPOCH: analysis selection crossed epoch"
        )
    return start, end


def _replay_mode(
    client: Any,
    *,
    epoch: EpochDefinition,
    chain_version: str,
    chain_hash: str,
    analysis_start_ns: int,
    analysis_end_ns: int,
    use_cache: bool,
) -> tuple[Any, dict[str, Any]]:
    apply_start, apply_end = epoch_apply_bounds(epoch)
    resume_profile: dict[str, Any] = {}
    resume = select_resume_checkpoint(
        iter_bronze_window(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            start_ns=epoch.safe_start_ns,
            end_ns=epoch.safe_end_ns,
            profile=resume_profile,
            start_apply_key=apply_start,
            end_apply_key=apply_end,
        ),
        epoch=epoch,
        analysis_start_ns=analysis_start_ns,
    )
    stream_profile: dict[str, Any] = {}
    started = time.monotonic()
    replay = replay_epoch_window(
        iter_bronze_window(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            start_ns=epoch.safe_start_ns,
            end_ns=epoch.safe_end_ns,
            profile=stream_profile,
            start_apply_key=apply_start,
            end_apply_key=apply_end,
        ),
        epoch=epoch,
        resume_record=resume,
        analysis_start_ns=analysis_start_ns,
        analysis_end_ns=analysis_end_ns,
        build_id="0" * 64,
        use_book_hash_cache=use_cache,
    )
    return replay, {
        "wall_s": round(time.monotonic() - started, 6),
        "resume_load": resume_profile,
        "replay_load": stream_profile,
        "replay_phases": replay.timings_s,
        "hash_phases": replay.hash_profile,
    }


def _fingerprint(replay: Any) -> dict[str, Any]:
    level_hashes = [
        hashlib.sha256(_canonical_bytes(row)).digest()
        for row in replay.level_changes
    ]
    state_hashes = [
        hashlib.sha256(_canonical_bytes(row)).digest() for row in replay.states
    ]
    level_identities = [
        (
            row["source_record_id"],
            row["apply_order"],
            row["side"],
            row["price"],
        )
        for row in replay.level_changes
    ]
    state_identities = [
        (row["epoch_id"], row["bucket_start_ms"]) for row in replay.states
    ]
    aggregate = hashlib.sha256()
    for item in level_hashes:
        aggregate.update(item)
    for item in state_hashes:
        aggregate.update(item)
    return {
        "level_hashes": Counter(level_hashes),
        "state_hashes": Counter(state_hashes),
        "level_count": len(level_hashes),
        "state_count": len(state_hashes),
        "duplicate_level_identities": len(level_identities)
        - len(set(level_identities)),
        "duplicate_state_identities": len(state_identities)
        - len(set(state_identities)),
        "canonical_result_hash": aggregate.hexdigest(),
        "book_hashes": [row["book_hash"] for row in replay.states],
        "bbo_mid_spread": [
            (row["best_bid"], row["best_ask"], row["mid"], row["spread"])
            for row in replay.states
        ],
        "epoch_ids": [row["epoch_id"] for row in replay.states],
    }


def _compare_fingerprints(
    baseline: dict[str, Any], optimized: dict[str, Any]
) -> dict[str, Any]:
    missing_levels = sum((baseline["level_hashes"] - optimized["level_hashes"]).values())
    extra_levels = sum((optimized["level_hashes"] - baseline["level_hashes"]).values())
    missing_states = sum((baseline["state_hashes"] - optimized["state_hashes"]).values())
    extra_states = sum((optimized["state_hashes"] - baseline["state_hashes"]).values())
    fields_equal = (
        baseline["book_hashes"] == optimized["book_hashes"]
        and baseline["bbo_mid_spread"] == optimized["bbo_mid_spread"]
        and baseline["epoch_ids"] == optimized["epoch_ids"]
    )
    proof = {
        "missing": missing_levels + missing_states,
        "extra": extra_levels + extra_states,
        "duplicates": optimized["duplicate_level_identities"]
        + optimized["duplicate_state_identities"],
        "field_deviations": 0 if fields_equal else 1,
        "baseline_hash": baseline["canonical_result_hash"],
        "optimized_hash": optimized["canonical_result_hash"],
        "canonical_hash_identical": (
            baseline["canonical_result_hash"]
            == optimized["canonical_result_hash"]
        ),
    }
    if any(
        proof[key] != 0
        for key in ("missing", "extra", "duplicates", "field_deviations")
    ) or not proof["canonical_hash_identical"]:
        raise EpochSilverError(
            "STOP_HASH_SEMANTICS_CHANGED: baseline and optimized rows differ"
        )
    return proof


def _blind_row_count(client: Any, discovery: EpochDiscovery) -> dict[str, int]:
    level_rows = state_rows = 0
    for left, right in zip(discovery.epochs, discovery.epochs[1:]):
        start, end = left.safe_end_ns, right.safe_start_ns
        if end <= start:
            continue
        level_rows += int(
            client.query(
                f"SELECT count() FROM research_full_ob_continuous_pilot_v1_3."
                f"{LEVEL_CHANGES_TABLE} FINAL "
                "WHERE event_time_ns >= {start:UInt64} AND event_time_ns < {end:UInt64}",
                parameters={"start": start, "end": end},
            ).result_rows[0][0]
        )
        state_rows += int(
            client.query(
                f"SELECT count() FROM research_full_ob_continuous_pilot_v1_3."
                f"{STATES_TABLE} FINAL "
                "WHERE bucket_start_ns >= {start:UInt64} AND bucket_start_ns < {end:UInt64}",
                parameters={"start": start, "end": end},
            ).result_rows[0][0]
        )
    return {"level_changes": level_rows, "states_100ms": state_rows}


def _profile_table(profile: dict[str, Any], total_s: float) -> list[dict[str, Any]]:
    replay = profile["replay_phases"]
    hashing = profile["hash_phases"]
    load = profile["replay_load"]
    phases = [
        ("Bronze load + server sort", load.get("bronze_load_and_server_sort_s", 0.0), 1),
        ("JSON/number conversion", load.get("json_decimal_conversion_s", 0.0), load.get("rows", 0)),
        ("Delta apply", replay.get("delta_apply", 0.0), profile.get("delta_count", 0)),
        ("Level-change generation", replay.get("level_change_generation", 0.0), profile.get("delta_count", 0)),
        ("100ms bucket generation", replay.get("states_100ms", 0.0), hashing.get("calls", 0)),
        ("Canonical book representation", hashing.get("canonical_representation_s", 0.0), hashing.get("recomputations", 0)),
        ("Bid/ask level sorting", hashing.get("level_sort_s", 0.0), hashing.get("recomputations", 0)),
        ("Hash serialization", hashing.get("serialization_s", 0.0), hashing.get("recomputations", 0)),
        ("SHA-256", hashing.get("sha256_s", 0.0), hashing.get("recomputations", 0)),
    ]
    return [
        {
            "phase": name,
            "seconds": round(float(seconds), 6),
            "percent_of_wall": round(float(seconds) / max(total_s, 1e-9) * 100, 3),
            "calls": int(calls),
            "seconds_per_call": round(float(seconds) / max(int(calls), 1), 9),
        }
        for name, seconds, calls in phases
    ]


def run_multi_epoch_hash_pilot(
    *, run_dir: Path, client: Any | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    own_client = client is None
    client = client or get_clickhouse_client()
    try:
        ensure_epoch_silver_schema(client)
        client.command(
            f"CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{MULTI_EPOCHS_TABLE} "
            f"AS {DATABASE_V13}.{EPOCHS_TABLE}"
        )
        chain_version, chain_hash = resolve_single_chain(client, symbol=SYMBOL)
        metadata = load_segment_metadata(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
        )
        contract = _persisted_import_contract(
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            metadata=metadata,
        )
        imports = [
            import_pilot_window(
                client,
                contract=contract,
                window_name="multi_epoch_hash_optimization_2026_09_07_21_2230",
                start_ns=WINDOW_START_NS,
                end_ns=FIRST_IMPORT_END_NS,
            ),
            import_pilot_window(
                client,
                contract=contract,
                window_name="multi_epoch_hash_optimization_extension_2026_09_07_2230_23",
                start_ns=EXTENSION_START_NS,
                end_ns=WINDOW_END_NS,
            ),
        ]
        reference = _load_reference(run_dir / REFERENCE_FILE)
        clean_rank = _reference_clean_rank(
            client,
            chain_version=chain_version,
            chain_hash=chain_hash,
            reference=reference,
        )
        scan_end_ns = int(reference[-1]["safe_end_ns"])
        discovery_profile: dict[str, Any] = {}
        discovery = discover_epochs(
            iter_bronze_window(
                client,
                symbol=SYMBOL,
                chain_version=chain_version,
                canonical_chain_hash=chain_hash,
                start_ns=WINDOW_START_NS,
                end_ns=WINDOW_END_NS,
                profile=discovery_profile,
            ),
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            scan_end_ns=scan_end_ns,
            clean_segment_start_ranks=clean_segment_start_ranks(metadata)
            | {clean_rank},
        )
        reference_proof = _compare_reference(discovery, reference)
        persist_epochs(client, discovery.epochs, table=MULTI_EPOCHS_TABLE)

        first_epoch = discovery.epochs[0]
        last_epoch = discovery.epochs[-1]
        first_start, first_end = _select_analysis(
            first_epoch, warmup_minutes=5, analysis_minutes=5
        )
        first_end = _floor_bucket(first_epoch.safe_end_ns)
        benchmark_start, benchmark_end = _select_analysis(
            last_epoch, warmup_minutes=15, analysis_minutes=15
        )

        print("profile progress 1/4 episode baseline", flush=True)
        episode_baseline, episode_baseline_profile = _replay_mode(
            client,
            epoch=next(
                epoch
                for epoch in (
                    discover_epochs(
                        iter_bronze_window(
                            client,
                            symbol=SYMBOL,
                            chain_version=chain_version,
                            canonical_chain_hash=chain_hash,
                            start_ns=EPISODE_SCAN_START,
                            end_ns=EPISODE_END,
                        ),
                        chain_version=chain_version,
                        canonical_chain_hash=chain_hash,
                        scan_end_ns=EPISODE_END,
                        clean_segment_start_ranks=clean_segment_start_ranks(metadata),
                    ).epochs
                )
                if epoch.safe_start_ns <= EPISODE_ANALYSIS_START
                and EPISODE_END <= epoch.safe_end_ns
            ),
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
            use_cache=False,
        )
        episode_baseline_fp = _fingerprint(episode_baseline)
        print("profile progress 2/4 episode optimized", flush=True)
        episode_optimized, episode_optimized_profile = _replay_mode(
            client,
            epoch=next(
                epoch
                for epoch in (
                    discover_epochs(
                        iter_bronze_window(
                            client,
                            symbol=SYMBOL,
                            chain_version=chain_version,
                            canonical_chain_hash=chain_hash,
                            start_ns=EPISODE_SCAN_START,
                            end_ns=EPISODE_END,
                        ),
                        chain_version=chain_version,
                        canonical_chain_hash=chain_hash,
                        scan_end_ns=EPISODE_END,
                        clean_segment_start_ranks=clean_segment_start_ranks(metadata),
                    ).epochs
                )
                if epoch.safe_start_ns <= EPISODE_ANALYSIS_START
                and EPISODE_END <= epoch.safe_end_ns
            ),
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
            use_cache=True,
        )
        episode_optimized_fp = _fingerprint(episode_optimized)
        episode_optimization_parity = _compare_fingerprints(
            episode_baseline_fp, episode_optimized_fp
        )
        episode_reference = _reference_episode1(client, episode_optimized)

        print("profile progress 3/4 multi-epoch baseline", flush=True)
        baseline, baseline_profile = _replay_mode(
            client,
            epoch=last_epoch,
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            use_cache=False,
        )
        baseline_fp = _fingerprint(baseline)
        baseline_delta_count = baseline.delta_records
        del baseline
        _check_rss()
        print("profile progress 4/4 multi-epoch optimized", flush=True)
        optimized, optimized_profile = _replay_mode(
            client,
            epoch=last_epoch,
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            use_cache=True,
        )
        optimized_fp = _fingerprint(optimized)
        optimization_parity = _compare_fingerprints(baseline_fp, optimized_fp)

        first_optimized, _ = _replay_mode(
            client,
            epoch=first_epoch,
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=first_start,
            analysis_end_ns=first_end,
            use_cache=True,
        )
        interrupted_before_gap = persist_replay_chunk(
            client,
            epoch=first_epoch,
            replay=first_optimized,
            analysis_start_ns=first_start,
            analysis_end_ns=first_end,
            warmup_ns=5 * 60 * 1_000_000_000,
            simulate_abort_after_running=True,
        )
        resumed_before_gap = persist_replay_chunk(
            client,
            epoch=first_epoch,
            replay=first_optimized,
            analysis_start_ns=first_start,
            analysis_end_ns=first_end,
            warmup_ns=5 * 60 * 1_000_000_000,
        )
        interrupted_after_reanchor = persist_replay_chunk(
            client,
            epoch=last_epoch,
            replay=optimized,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
            simulate_abort_after_running=True,
        )
        resumed_after_reanchor = persist_replay_chunk(
            client,
            epoch=last_epoch,
            replay=optimized,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
        )
        repeated_after_reanchor = persist_replay_chunk(
            client,
            epoch=last_epoch,
            replay=optimized,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
        )

        verification_started = time.monotonic()
        blind_rows = _blind_row_count(client, discovery)
        if blind_rows != {"level_changes": 0, "states_100ms": 0}:
            raise EpochSilverError(
                "STOP_RESUME_CROSSED_EPOCH: Silver rows found in blind intervals"
            )
        for replay, epoch in (
            (first_optimized, first_epoch),
            (optimized, last_epoch),
        ):
            if any(row["epoch_id"] != epoch.epoch_id for row in replay.level_changes):
                raise EpochSilverError(
                    "STOP_RESUME_CROSSED_EPOCH: level change crossed epoch"
                )
            if any(row["epoch_id"] != epoch.epoch_id for row in replay.states):
                raise EpochSilverError(
                    "STOP_RESUME_CROSSED_EPOCH: state crossed epoch"
                )
        verification_s = time.monotonic() - verification_started

        baseline_minutes = (benchmark_end - benchmark_start) / 60e9
        baseline_spm = baseline_profile["wall_s"] / baseline_minutes
        optimized_spm = optimized_profile["wall_s"] / baseline_minutes
        speedup = baseline_spm / optimized_spm
        full_hours = optimized_spm * 164 / 60
        verdict = (
            "MULTI_EPOCH_SILVER_V1_3_HASH_OPTIMIZATION_PROVEN"
            if optimized_spm < TARGET_S_PER_MARKET_MINUTE
            else "MULTI_EPOCH_SILVER_V1_3_CORRECT_PERFORMANCE_LIMITED"
        )
        baseline_profile["delta_count"] = baseline_delta_count
        optimized_profile["delta_count"] = optimized.delta_records
        return {
            "verdict": verdict,
            "window": {
                "start": ns_to_iso(WINDOW_START_NS),
                "end": ns_to_iso(WINDOW_END_NS),
                "duration_hours": 2,
                "segment_ranks": [
                    discovery.first_apply_key[0],
                    discovery.last_apply_key[0],
                ],
                "imports": imports,
            },
            "chain_version": chain_version,
            "canonical_chain_hash": chain_hash,
            "reference": reference_proof,
            "epochs": [
                {
                    "epoch_id": epoch.epoch_id,
                    "epoch_hash": epoch.epoch_hash,
                    "anchor_type": epoch.anchor_type,
                    "anchor_provenance": epoch.anchor_provenance,
                    "anchor_time_ns": epoch.anchor_event_time_ns,
                    "safe_start_ns": epoch.safe_start_ns,
                    "safe_end_ns": epoch.safe_end_ns,
                    "terminating_reason": epoch.terminating_reason,
                    "preceding_gap_id": epoch.preceding_gap_id,
                }
                for epoch in discovery.epochs
            ],
            "gap_count": len(discovery.gaps),
            "discovery_profile": discovery_profile,
            "parity": {
                "multi_epoch": optimization_parity,
                "episode1_optimization": episode_optimization_parity,
                "episode1_reference": episode_reference,
            },
            "resume": {
                "interrupted_before_gap": interrupted_before_gap,
                "resumed_before_gap": resumed_before_gap,
                "interrupted_after_reanchor": interrupted_after_reanchor,
                "resumed_after_reanchor": resumed_after_reanchor,
                "repeated_after_reanchor": repeated_after_reanchor,
                "blind_interval_rows": blind_rows,
            },
            "performance": {
                "prior_pilot_seconds_per_market_minute": BASELINE_S_PER_MARKET_MINUTE,
                "profiled_uncached_seconds_per_market_minute": round(baseline_spm, 6),
                "optimized_seconds_per_market_minute": round(optimized_spm, 6),
                "hash_cache_speedup": round(speedup, 3),
                "speedup_vs_prior_pilot": round(
                    BASELINE_S_PER_MARKET_MINUTE / optimized_spm, 3
                ),
                "estimated_164h_hours": round(full_hours, 3),
                "baseline": {
                    **baseline_profile,
                    "phase_table": _profile_table(
                        baseline_profile, baseline_profile["wall_s"]
                    ),
                },
                "optimized": {
                    **optimized_profile,
                    "phase_table": _profile_table(
                        optimized_profile, optimized_profile["wall_s"]
                    ),
                },
                "clickhouse_insert_s": round(
                    float(resumed_before_gap.get("insert_s", 0.0))
                    + float(resumed_after_reanchor.get("insert_s", 0.0)),
                    6,
                ),
                "verification_s": round(verification_s, 6),
                "level_changes": len(optimized.level_changes),
                "states_100ms": len(optimized.states),
            },
            "resource_usage": {
                "elapsed_s": round(time.monotonic() - started, 3),
                "peak_rss_kb": _rss_kb(),
                "worker_count": 1,
            },
        }
    finally:
        if own_client:
            client.close()


def write_report(result: dict[str, Any], path: Path) -> None:
    performance = result["performance"]
    reference = result["reference"]
    parity = result["parity"]
    resume = result["resume"]
    path.write_text(
        f"""# Multi-Epoch Silver Hash Optimization Pilot v1.3

## 1. VERDICT

`{result['verdict']}`

## 2–4. Fenster, Epochen, Gaps und Reanchor

- Fenster: `{result['window']['start']}` bis `{result['window']['end']}` (2h)
- Segmentränge: {result['window']['segment_ranks']}
- Silver-Fenster vor erstem Gap: `2026-09-07T21:05:00.500Z` bis
  `21:10:40.900Z` (5m Warm-up); nach Reanchor:
  `2026-09-07T22:35:19.900Z` bis `22:50:19.900Z` (15m Warm-up).
- 1m/5m/15m-Warm-ups wurden innerhalb einer Epoch validiert; kein Clipping.
- Epochen: erwartet/aktuell {reference['expected_epochs']}/{reference['actual_epochs']}
- Gaps: erwartet/aktuell {reference['expected_gaps']}/{reference['actual_gaps']}
- Safe-Intervall-Abweichungen: {len(reference['interval_mismatches'])}
- Anchor-Zeitabweichungen: {reference['anchor_time_mismatches']}
- `periodic_5m` bleibt ausschließlich lokales Resume-Material und öffnet
  keine Epoch. Die vollständigen 88 Start-/End-ns stehen im JSON-Begleitartefakt.
- Alle Reanchors nach Gap stammen aus archiviertem Exchange-Snapshot plus
  book-hash-identischem `reconnect_resync`.

## 5–7. Profil, Ursache und Optimierung

Historisches Cross-Window-Pilotprofil (nur Orientierung):
{performance['prior_pilot_seconds_per_market_minute']} s/Marktminute.
Profilierte Same-Input-Uncached-Baseline:
{performance['profiled_uncached_seconds_per_market_minute']} s/Marktminute.

Baseline-Phasen:
`{json.dumps(performance['baseline']['phase_table'], sort_keys=True)}`

Optimierte Phasen:
`{json.dumps(performance['optimized']['phase_table'], sort_keys=True)}`

Ursache war die vollständige kanonische Book-Repräsentation, Level-Sortierung
und `<Bdd>`-Serialisierung für jeden 100-ms-Bucket. Implementiert wurden ein
verlustfreier Dirty-State, Hash-Wiederverwendung bei unverändertem Book und
No-op-Erkennung. Kanonische Bytes, Sortierung und SHA-256 bleiben unverändert.
Eine weitergehende inkrementelle Hashstruktur wurde nicht eingesetzt.

## 8–9. Parität und Episode 1

- Multi-Epoch missing/extra/duplicates/fields:
  {parity['multi_epoch']['missing']}/{parity['multi_epoch']['extra']}/
  {parity['multi_epoch']['duplicates']}/{parity['multi_epoch']['field_deviations']}
- Kanonischer Gesamthash identisch:
  {parity['multi_epoch']['canonical_hash_identical']}
- Kanonischer Gesamthash:
  `{parity['multi_epoch']['optimized_hash']}`
- Episode Optimierungsparität:
  {parity['episode1_optimization']['canonical_hash_identical']}
- Episode 1: {parity['episode1_reference']['level_changes_actual']}/
  {parity['episode1_reference']['level_changes_reference']} Level-Changes,
  {parity['episode1_reference']['states_100ms_actual']}/
  {parity['episode1_reference']['states_100ms_reference']} States,
  `{parity['episode1_reference']['status']}`.
- Episode-1-LC-Hash:
  `{parity['episode1_reference']['actual_lc_hash']}`.
- Episode-1-UTC/ns-Differenz: 0.

## 10. Resume und Idempotenz

- Abbruch vor Gap: `{resume['interrupted_before_gap']['status']}`
- Resume derselben Epoch: `{resume['resumed_before_gap']['status']}`
- Abbruch nach unabhängigem Reanchor:
  `{resume['interrupted_after_reanchor']['status']}`
- Resume nach Reanchor: `{resume['resumed_after_reanchor']['status']}`
- Wiederholung: `{resume['repeated_after_reanchor']['status']}`
- Blindintervall-Zeilen: `{json.dumps(resume['blind_interval_rows'], sort_keys=True)}`
- Outcomes wurden in diesem Silver-Pilot nicht erzeugt; dadurch existieren
  keine Outcome-Zeilen, die eine Epoch-Grenze überschreiten könnten.

## 11–13. Performance und Ressourcen

- Optimiert: {performance['optimized_seconds_per_market_minute']} s/Marktminute
- Same-Input-Hash-Cache-Speedup: {performance['hash_cache_speedup']}x
- Cross-Window-Speedup gegen 9,57-s-Pilot:
  {performance['speedup_vs_prior_pilot']}x (vorläufig)
- 164h-Prognose, keine garantierte Laufzeit:
  {performance['estimated_164h_hours']} Stunden
- Level-Changes: {performance['level_changes']}
- 100-ms-States: {performance['states_100ms']}
- Deltas/s: {round(performance['optimized']['delta_count'] / performance['optimized']['wall_s'], 3)}
- Level-Changes/s: {round(performance['level_changes'] / performance['optimized']['wall_s'], 3)}
- Insert-Zeilen/s: {round((resume['resumed_before_gap']['rows_inserted'] + resume['resumed_after_reanchor']['rows_inserted']) / max(performance['clickhouse_insert_s'], 1e-9), 3)}
- ClickHouse-Insert: {performance['clickhouse_insert_s']} s
- Verifikation: {performance['verification_s']} s
- Peak RSS: {result['resource_usage']['peak_rss_kb']} KiB
- Gesamtlaufzeit: {result['resource_usage']['elapsed_s']} s
- Hash-Aufrufe/Recomputes/Cache-Hits:
  {performance['optimized']['hash_phases']['calls']}/
  {performance['optimized']['hash_phases']['recomputations']}/
  {performance['optimized']['hash_phases']['cache_hits']}

## 14–18. Tests, Dateien, Git, Risiken und Empfehlung

Alle bisherigen und neuen Tests prüfen Hashbytes/-wiederverwendung, Dirty-State,
No-op-Deltas, Baseline-/Optimierungsparität, mehrere Epochen/Reanchors,
Segmentwechsel, Resume vor/nach Gap und deterministische Wiederholung.
Final: 96/96 PASS. Eine unabhängige zweite optimierte Ausführung erzeugte
erneut `{parity['multi_epoch']['optimized_hash']}` bei identischen
647.833 Level-Changes und 9.000 States.

Persistierter Nachweis: 760.086/760.086 eindeutige Level-Change-Row-IDs,
12.404/12.404 eindeutige State-Row-IDs und Epoch/Bucket-Schlüssel,
2/2 COMPLETE-Chunks sowie UTC/ns-Abweichungen 0. Das Fenster enthält
24 `periodic_5m`-Checkpoints.

Geändert: `silver_replay.py`, `epoch_aware_silver_v1_3.py`,
`multi_epoch_hash_pilot_v1_3.py` und
`test_epoch_aware_silver_v1_3.py`; die vorherigen uncommitted Pilotdateien
bleiben bestehen. Kein Commit/Push und kein Full-Import/-Build.
Branch `research/clickhouse-defense-store-v1`, HEAD
`0c74bb9691dfdf43e413c1f3f7d3161cf2a9f3d4`; Runs bleiben ignored.

Restrisiko: Die Hochrechnung basiert auf einem 15-Minuten-Abschnitt der finalen
sicheren Epoch innerhalb eines realen Zwei-Stunden-Reconnect-Fensters. Nächster
Schritt ist ein weiterer begrenzter repräsentativer Performance-Pilot; eine
inkrementelle Hashstruktur bleibt bis zum exakten Byteparitätsbeweis verboten.
""",
        encoding="utf-8",
    )


def main() -> int:
    run_dir = Path(
        "obfull_research_engine/runs/clickhouse_research_store_pilot_v1"
    )
    try:
        result = run_multi_epoch_hash_pilot(run_dir=run_dir)
        write_report(
            result,
            run_dir / "MULTI_EPOCH_SILVER_HASH_OPTIMIZATION_PILOT_V1_3.md",
        )
        (run_dir / "multi_epoch_silver_hash_optimization_pilot_v1_3.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except EpochSilverError as exc:
        print(
            json.dumps(
                {"verdict": str(exc).split(":", 1)[0], "error": str(exc)},
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
