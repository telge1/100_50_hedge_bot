"""Bounded ClickHouse pilot for the epoch-aware v1.3 Silver builder."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import DATABASE as LEGACY_DATABASE
from .canonical_segment_order_v1_3 import DATABASE_V13, import_pilot_window
from .detail_parity import canonical_lc_row, hash_canonical_lcs
from .epoch_aware_silver_v1_3 import (
    CHUNKS_TABLE,
    EPOCHS_TABLE,
    LEVEL_CHANGES_TABLE,
    STATES_TABLE,
    EpochDefinition,
    EpochDiscovery,
    EpochSilverError,
    clean_segment_start_ranks,
    discover_epochs,
    epoch_apply_bounds,
    ensure_epoch_silver_schema,
    iso,
    iter_bronze_window,
    load_segment_metadata,
    ns_to_iso,
    persist_epochs,
    persist_replay_chunk,
    replay_epoch_window,
    resolve_single_chain,
    select_resume_checkpoint,
    validate_epoch_window,
    _check_rss,
    _rss_kb,
)
from .helpers import get_clickhouse_client

SYMBOL = "BTCUSDT"
EPISODE_SCAN_START = iso("2026-09-06T20:00:00.000000000Z")
EPISODE_PREFIX_END = iso("2026-09-06T20:14:59.873000000Z")
EPISODE_ANALYSIS_START = iso("2026-09-06T20:19:00.000000000Z")
EPISODE_END = iso("2026-09-06T20:20:00.000000000Z")
RECONNECT_SCAN_START = iso("2026-09-07T21:10:40.000000000Z")
RECONNECT_SCAN_END = iso("2026-09-07T21:10:45.000000000Z")
CROSS_SCAN_START = iso("2026-09-10T09:54:30.000000000Z")
CROSS_SCAN_END = iso("2026-09-10T10:00:00.000000000Z")
BENCHMARK_SCAN_START = iso("2026-09-06T07:00:00.000000000Z")
BENCHMARK_SCAN_END = iso("2026-09-06T08:00:00.000000000Z")
WARMUPS_S = (60, 300, 900, 1800)
BASELINE_SECONDS_PER_MARKET_MINUTE = 25.0


def _persisted_import_contract(
    *,
    chain_version: str,
    canonical_chain_hash: str,
    metadata: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Adapt persisted canonical metadata to the bounded Bronze importer."""
    return {
        "chain_version": chain_version,
        "canonical_chain_hash": canonical_chain_hash,
        "segments": [
            {"symbol": SYMBOL, **metadata[rank]} for rank in sorted(metadata)
        ],
    }


def _discover(
    client: Any,
    *,
    chain_version: str,
    chain_hash: str,
    clean_ranks: set[int],
    start_ns: int,
    end_ns: int,
) -> tuple[EpochDiscovery, float]:
    started = time.monotonic()
    result = discover_epochs(
        iter_bronze_window(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            start_ns=start_ns,
            end_ns=end_ns,
        ),
        chain_version=chain_version,
        canonical_chain_hash=chain_hash,
        scan_end_ns=end_ns,
        clean_segment_start_ranks=clean_ranks,
    )
    return result, time.monotonic() - started


def _warmup_matrix(
    epochs: list[EpochDefinition],
    *,
    analysis_start_ns: int,
    analysis_end_ns: int,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for seconds in WARMUPS_S:
        decision = validate_epoch_window(
            epochs,
            analysis_start_ns=analysis_start_ns,
            analysis_end_ns=analysis_end_ns,
            warmup_ns=seconds * 1_000_000_000,
        )
        out[f"{seconds // 60}m"] = {
            "status": decision.status,
            "epoch_id": decision.epoch.epoch_id if decision.epoch else None,
            "reason": decision.reason,
        }
    return out


def _find_epoch(
    epochs: list[EpochDefinition],
    *,
    analysis_start_ns: int,
    analysis_end_ns: int,
    warmup_ns: int,
) -> EpochDefinition:
    decision = validate_epoch_window(
        epochs,
        analysis_start_ns=analysis_start_ns,
        analysis_end_ns=analysis_end_ns,
        warmup_ns=warmup_ns,
    )
    if decision.status != "OK" or decision.epoch is None:
        raise EpochSilverError(
            "STOP_EPOCH_BOUNDARY_VIOLATION: requested analysis is not epoch-contained"
        )
    return decision.epoch


def _select_benchmark(
    epochs: list[EpochDefinition],
) -> tuple[EpochDefinition, int, int]:
    """Select earliest epoch containing 15m warm-up plus 15m analysis."""
    warmup_ns = 15 * 60 * 1_000_000_000
    analysis_ns = 15 * 60 * 1_000_000_000
    for epoch in epochs:
        start = ((epoch.safe_start_ns + warmup_ns + 99_999_999) // 100_000_000) * 100_000_000
        end = start + analysis_ns
        if end <= epoch.safe_end_ns:
            return epoch, start, end
    raise EpochSilverError(
        "STOP_SILVER_PERFORMANCE_REGRESSION: no deterministic 15-minute safe benchmark"
    )


def _replay(
    client: Any,
    *,
    epoch: EpochDefinition,
    chain_version: str,
    chain_hash: str,
    analysis_start_ns: int,
    analysis_end_ns: int,
) -> tuple[Any, float]:
    apply_start, apply_end = epoch_apply_bounds(epoch)
    resume = select_resume_checkpoint(
        iter_bronze_window(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            start_ns=epoch.safe_start_ns,
            end_ns=epoch.safe_end_ns,
            start_apply_key=apply_start,
            end_apply_key=apply_end,
        ),
        epoch=epoch,
        analysis_start_ns=analysis_start_ns,
    )
    build_id = "0" * 64
    started = time.monotonic()
    replay = replay_epoch_window(
        iter_bronze_window(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            start_ns=epoch.safe_start_ns,
            end_ns=epoch.safe_end_ns,
            start_apply_key=apply_start,
            end_apply_key=apply_end,
        ),
        epoch=epoch,
        resume_record=resume,
        analysis_start_ns=analysis_start_ns,
        analysis_end_ns=analysis_end_ns,
        build_id=build_id,
    )
    return replay, time.monotonic() - started


def _reference_episode1(client: Any, replay: Any) -> dict[str, Any]:
    def text(value: Any) -> str:
        return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)

    hash_started = time.monotonic()
    actual_rows = [
        canonical_lc_row(
            event_time_ns=row["event_time_ns"],
            update_id=row["update_id"],
            seq=row["seq"],
            side=row["side"],
            price=row["price"],
            old_size=row["old_size"],
            new_size=row["new_size"],
            change_type=row["change_type"],
        )
        for row in replay.level_changes
    ]
    actual_hash = hash_canonical_lcs(actual_rows)
    reference_rows = client.query(
        f"""
        SELECT
          event_time_ns, update_id, seq, side, price,
          old_size, new_size, change_type
        FROM {LEGACY_DATABASE}.ob_level_changes_pilot_v1_2 FINAL
        ORDER BY event_time_ns, update_id, seq, side, price,
                 old_size, new_size, change_type
        """
    ).result_rows
    canonical_reference = [
        canonical_lc_row(
            event_time_ns=int(row[0]),
            update_id=int(row[1]),
            seq=int(row[2]),
            side=text(row[3]),
            price=float(row[4]),
            old_size=float(row[5]),
            new_size=float(row[6]),
            change_type=text(row[7]),
        )
        for row in reference_rows
    ]
    reference_hash = hash_canonical_lcs(canonical_reference)
    reference_states = client.query(
        f"""
        SELECT bucket_start_ms, best_bid, best_ask, mid, book_hash
        FROM {LEGACY_DATABASE}.ob_metrics_100ms_pilot_v1_2 FINAL
        ORDER BY bucket_start_ms
        """
    ).result_rows
    actual_states = [
        (
            int(row["bucket_start_ms"]),
            row["best_bid"],
            row["best_ask"],
            row["mid"],
            row["book_hash"],
        )
        for row in replay.states
    ]
    normalized_reference_states = [
        (
            int(row[0]),
            row[1],
            row[2],
            row[3],
            row[4].decode() if isinstance(row[4], bytes) else str(row[4]),
        )
        for row in reference_states
    ]
    proof = {
        "level_changes_actual": len(actual_rows),
        "level_changes_reference": len(canonical_reference),
        "actual_lc_hash": actual_hash,
        "reference_lc_hash": reference_hash,
        "states_100ms_actual": len(actual_states),
        "states_100ms_reference": len(normalized_reference_states),
        "states_exact": actual_states == normalized_reference_states,
        "hashing_s": round(time.monotonic() - hash_started, 6),
    }
    proof["status"] = (
        "PARITY_EXACT"
        if proof["level_changes_actual"] == proof["level_changes_reference"] == 30_939
        and actual_hash == reference_hash
        and proof["states_100ms_actual"] == proof["states_100ms_reference"] == 600
        and proof["states_exact"]
        else "STOP_EPISODE1_PARITY_MISMATCH"
    )
    if proof["status"] != "PARITY_EXACT":
        raise EpochSilverError(str(proof["status"]))
    return proof


def _verify_persisted_chunk(client: Any, chunk_key: str) -> dict[str, int]:
    level = client.query(
        f"SELECT count() FROM {DATABASE_V13}.{LEVEL_CHANGES_TABLE} FINAL "
        "WHERE chunk_key = {key:String}",
        parameters={"key": chunk_key},
    ).result_rows[0][0]
    states = client.query(
        f"SELECT count() FROM {DATABASE_V13}.{STATES_TABLE} FINAL "
        "WHERE chunk_key = {key:String}",
        parameters={"key": chunk_key},
    ).result_rows[0][0]
    chunks = client.query(
        f"SELECT count() FROM {DATABASE_V13}.{CHUNKS_TABLE} FINAL "
        "WHERE chunk_key = {key:String} AND status = 'COMPLETE'",
        parameters={"key": chunk_key},
    ).result_rows[0][0]
    return {
        "level_changes": int(level),
        "states_100ms": int(states),
        "complete_chunk_rows": int(chunks),
    }


def _epoch_summary(discovery: EpochDiscovery) -> dict[str, Any]:
    return {
        "source_records": discovery.source_records,
        "first_apply_key": list(discovery.first_apply_key) if discovery.first_apply_key else None,
        "last_apply_key": list(discovery.last_apply_key) if discovery.last_apply_key else None,
        "gaps": [
            {
                "gap_id": gap.gap_id,
                "reason": gap.reason,
                "apply_key": [gap.segment_chain_index, gap.record_ordinal],
                "gap_time_ns": gap.gap_time_ns,
            }
            for gap in discovery.gaps
        ],
        "epochs": [
            {
                "epoch_id": epoch.epoch_id,
                "epoch_hash": epoch.epoch_hash,
                "anchor_type": epoch.anchor_type,
                "anchor_provenance": epoch.anchor_provenance,
                "anchor_apply_key": [
                    epoch.anchor_segment_chain_index,
                    epoch.anchor_record_ordinal,
                ],
                "safe_start_ns": epoch.safe_start_ns,
                "safe_end_ns": epoch.safe_end_ns,
                "terminating_reason": epoch.terminating_reason,
                "preceding_gap_id": epoch.preceding_gap_id,
                "reconnect_resync_proven": epoch.reconnect_resync_proven,
            }
            for epoch in discovery.epochs
        ],
    }


def run_epoch_aware_pilot(client: Any | None = None) -> dict[str, Any]:
    started = time.monotonic()
    own_client = client is None
    client = client or get_clickhouse_client()
    try:
        ensure_epoch_silver_schema(client)
        chain_version, chain_hash = resolve_single_chain(client, symbol=SYMBOL)
        metadata = load_segment_metadata(
            client,
            symbol=SYMBOL,
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
        )
        clean_ranks = clean_segment_start_ranks(metadata)
        contract = _persisted_import_contract(
            chain_version=chain_version,
            canonical_chain_hash=chain_hash,
            metadata=metadata,
        )
        # Bounded additive prefix only; makes the true segment_start available.
        prefix_import = import_pilot_window(
            client,
            contract=contract,
            window_name="epoch_episode1_segment_start_prefix",
            start_ns=EPISODE_SCAN_START,
            end_ns=EPISODE_PREFIX_END,
        )
        windows: dict[str, tuple[EpochDiscovery, float]] = {}
        for index, (name, start_ns, end_ns) in enumerate(
            [
                ("episode1", EPISODE_SCAN_START, EPISODE_END),
                ("reconnect_2026_09_07_21", RECONNECT_SCAN_START, RECONNECT_SCAN_END),
                ("cross_segment_2026_09_10_09", CROSS_SCAN_START, CROSS_SCAN_END),
                ("benchmark_source_2026_09_06_07", BENCHMARK_SCAN_START, BENCHMARK_SCAN_END),
            ],
            1,
        ):
            print(f"epoch pilot progress {index}/4 window={name}", flush=True)
            windows[name] = _discover(
                client,
                chain_version=chain_version,
                chain_hash=chain_hash,
                clean_ranks=clean_ranks,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            persist_epochs(client, windows[name][0].epochs)
            _check_rss()

        episode_discovery = windows["episode1"][0]
        episode_epoch = _find_epoch(
            episode_discovery.epochs,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
            warmup_ns=15 * 60 * 1_000_000_000,
        )
        episode_replay, episode_replay_s = _replay(
            client,
            epoch=episode_epoch,
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
        )
        episode_parity = _reference_episode1(client, episode_replay)
        episode_persist = persist_replay_chunk(
            client,
            epoch=episode_epoch,
            replay=episode_replay,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
            warmup_ns=15 * 60 * 1_000_000_000,
        )
        episode_second = persist_replay_chunk(
            client,
            epoch=episode_epoch,
            replay=episode_replay,
            analysis_start_ns=EPISODE_ANALYSIS_START,
            analysis_end_ns=EPISODE_END,
            warmup_ns=15 * 60 * 1_000_000_000,
        )

        benchmark_discovery = windows["benchmark_source_2026_09_06_07"][0]
        benchmark_epoch, benchmark_start, benchmark_end = _select_benchmark(
            benchmark_discovery.epochs
        )
        benchmark_replay, benchmark_replay_s = _replay(
            client,
            epoch=benchmark_epoch,
            chain_version=chain_version,
            chain_hash=chain_hash,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
        )
        # Exercise controlled resume: RUNNING only, then rebuild deterministic rows.
        benchmark_interrupted = persist_replay_chunk(
            client,
            epoch=benchmark_epoch,
            replay=benchmark_replay,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
            simulate_abort_after_running=True,
        )
        benchmark_persist = persist_replay_chunk(
            client,
            epoch=benchmark_epoch,
            replay=benchmark_replay,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
        )
        benchmark_second = persist_replay_chunk(
            client,
            epoch=benchmark_epoch,
            replay=benchmark_replay,
            analysis_start_ns=benchmark_start,
            analysis_end_ns=benchmark_end,
            warmup_ns=15 * 60 * 1_000_000_000,
        )

        verification_started = time.monotonic()
        episode_verify = _verify_persisted_chunk(
            client, episode_persist["chunk_key"]
        )
        benchmark_verify = _verify_persisted_chunk(
            client, benchmark_persist["chunk_key"]
        )
        if (
            episode_verify["level_changes"] != len(episode_replay.level_changes)
            or episode_verify["states_100ms"] != len(episode_replay.states)
            or benchmark_verify["level_changes"] != len(benchmark_replay.level_changes)
            or benchmark_verify["states_100ms"] != len(benchmark_replay.states)
            or episode_verify["complete_chunk_rows"] != 1
            or benchmark_verify["complete_chunk_rows"] != 1
        ):
            raise EpochSilverError(
                "STOP_SILVER_RESUME_NOT_IDEMPOTENT: persisted row counts differ"
            )
        utc_epoch_bad = int(client.query(
            f"""
            SELECT count()
            FROM {DATABASE_V13}.{EPOCHS_TABLE} FINAL
            WHERE toUnixTimestamp64Nano(anchor_event_time) != anchor_event_time_ns
               OR toUnixTimestamp64Nano(anchor_receive_time) != anchor_receive_time_ns
               OR toUnixTimestamp64Nano(safe_start) != safe_start_ns
               OR toUnixTimestamp64Nano(safe_end) != safe_end_ns
            """
        ).result_rows[0][0])
        utc_lc_bad = int(client.query(
            f"SELECT count() FROM {DATABASE_V13}.{LEVEL_CHANGES_TABLE} FINAL "
            "WHERE toUnixTimestamp64Nano(event_time) != event_time_ns"
        ).result_rows[0][0])
        utc_state_bad = int(client.query(
            f"SELECT count() FROM {DATABASE_V13}.{STATES_TABLE} FINAL "
            "WHERE toUnixTimestamp64Nano(bucket_start) != bucket_start_ns"
        ).result_rows[0][0])
        if utc_epoch_bad or utc_lc_bad or utc_state_bad:
            raise EpochSilverError(
                "STOP_SILVER_REPLAY_ORDER_MISMATCH: UTC/ns persistence mismatch"
            )
        verification_s = time.monotonic() - verification_started

        reconnect = windows["reconnect_2026_09_07_21"][0]
        cross = windows["cross_segment_2026_09_10_09"][0]
        if not reconnect.gaps or not reconnect.epochs:
            raise EpochSilverError(
                "STOP_EPOCH_ANCHOR_PROVENANCE_UNRESOLVED: reconnect proof incomplete"
            )
        if (
            reconnect.epochs[0].safe_start_ns <= reconnect.gaps[0].gap_time_ns
            or reconnect.epochs[0].preceding_gap_id != reconnect.gaps[0].gap_id
        ):
            raise EpochSilverError(
                "STOP_EPOCH_BOUNDARY_VIOLATION: reconnect blind interval replayed"
            )
        if not any(epoch.anchor_segment_chain_index == 116 for epoch in cross.epochs):
            raise EpochSilverError(
                "STOP_SILVER_REPLAY_ORDER_MISMATCH: cross-segment reanchor missing"
            )
        if cross.last_apply_key is None or cross.last_apply_key[0] != 117:
            raise EpochSilverError(
                "STOP_SILVER_REPLAY_ORDER_MISMATCH: rank-117 continuation missing"
            )

        episode_market_minutes = (EPISODE_END - EPISODE_ANALYSIS_START) / 60e9
        benchmark_market_minutes = (benchmark_end - benchmark_start) / 60e9
        episode_spm = episode_replay_s / episode_market_minutes
        benchmark_spm = benchmark_replay_s / benchmark_market_minutes
        if benchmark_spm >= BASELINE_SECONDS_PER_MARKET_MINUTE:
            raise EpochSilverError(
                "STOP_SILVER_PERFORMANCE_REGRESSION: benchmark did not beat baseline"
            )
        full_hours = 164
        result = {
            "verdict": "EPOCH_AWARE_SILVER_BUILDER_V1_3_PILOT_PROVEN",
            "chain_version": chain_version,
            "canonical_chain_hash": chain_hash,
            "schema": {
                "epochs": EPOCHS_TABLE,
                "level_changes": LEVEL_CHANGES_TABLE,
                "states_100ms": STATES_TABLE,
                "chunks": CHUNKS_TABLE,
            },
            "bounded_episode_prefix_import": prefix_import,
            "windows": {
                name: {
                    **_epoch_summary(discovery),
                    "discovery_s": round(elapsed, 6),
                }
                for name, (discovery, elapsed) in windows.items()
            },
            "warmup_checks": {
                "episode1": _warmup_matrix(
                    episode_discovery.epochs,
                    analysis_start_ns=EPISODE_ANALYSIS_START,
                    analysis_end_ns=EPISODE_END,
                ),
                "benchmark": _warmup_matrix(
                    benchmark_discovery.epochs,
                    analysis_start_ns=benchmark_start,
                    analysis_end_ns=benchmark_end,
                ),
            },
            "episode1": {
                "analysis_start": ns_to_iso(EPISODE_ANALYSIS_START),
                "analysis_end": ns_to_iso(EPISODE_END),
                "parity": episode_parity,
                "persist": episode_persist,
                "second_identical_build": episode_second,
                "persisted_counts": episode_verify,
            },
            "benchmark": {
                "analysis_start": ns_to_iso(benchmark_start),
                "analysis_end": ns_to_iso(benchmark_end),
                "duration_minutes": benchmark_market_minutes,
                "epoch_id": benchmark_epoch.epoch_id,
                "level_changes": len(benchmark_replay.level_changes),
                "states_100ms": len(benchmark_replay.states),
                "delta_records": benchmark_replay.delta_records,
                "interrupted_probe": benchmark_interrupted,
                "resumed_build": benchmark_persist,
                "second_identical_build": benchmark_second,
                "persisted_counts": benchmark_verify,
            },
            "performance": {
                "baseline_seconds_per_market_minute": BASELINE_SECONDS_PER_MARKET_MINUTE,
                "episode1_replay_s": round(episode_replay_s, 6),
                "episode1_seconds_per_market_minute": round(episode_spm, 6),
                "benchmark_replay_s": round(benchmark_replay_s, 6),
                "benchmark_seconds_per_market_minute": round(benchmark_spm, 6),
                "benchmark_deltas_per_second": round(
                    benchmark_replay.delta_records / benchmark_replay_s, 3
                ),
                "benchmark_level_changes_per_second": round(
                    len(benchmark_replay.level_changes) / benchmark_replay_s, 3
                ),
                "benchmark_insert_rows_per_second": round(
                    benchmark_persist.get("rows_inserted", 0)
                    / max(benchmark_persist.get("insert_s", 0.0), 1e-9),
                    3,
                ),
                "benchmark_speedup": round(
                    BASELINE_SECONDS_PER_MARKET_MINUTE / benchmark_spm, 3
                ),
                "estimated_164h_seconds": round(
                    benchmark_spm * full_hours * 60, 3
                ),
                "estimated_164h_hours": round(
                    benchmark_spm * full_hours / 60, 3
                ),
                "episode_timings": episode_replay.timings_s,
                "benchmark_timings": benchmark_replay.timings_s,
                "episode_hashing_s": episode_parity["hashing_s"],
                "verification_s": round(verification_s, 6),
            },
            "storage_proof": {
                "utc_epoch_mismatches": utc_epoch_bad,
                "utc_level_change_mismatches": utc_lc_bad,
                "utc_state_mismatches": utc_state_bad,
                "episode_chunk": episode_verify,
                "benchmark_chunk": benchmark_verify,
            },
            "resource_usage": {
                "elapsed_s": round(time.monotonic() - started, 3),
                "peak_rss_kb": _rss_kb(),
                "worker_count": 1,
            },
        }
        _check_rss()
        return result
    finally:
        if own_client:
            client.close()


def write_report(result: dict[str, Any], path: Path) -> None:
    episode = result["episode1"]
    parity = episode["parity"]
    benchmark = result["benchmark"]
    performance = result["performance"]
    reconnect = result["windows"]["reconnect_2026_09_07_21"]
    cross = result["windows"]["cross_segment_2026_09_10_09"]
    path.write_text(
        f"""# Epoch-aware Silver Builder v1.3 — Bounded Pilot

## 1. VERDICT

`{result['verdict']}`

## 2–4. Ursache, Ziel und Epoch-Vertrag

Der alte Minutenpfad lud und replayte denselben Warm-up wiederholt. Der neue
Pfad liest Bronze strikt nach `(canonical_segment_chain_index, record_ordinal)`,
persistiert deterministische Epoch-IDs/-Hashes und hält nur den aktuellen
Full-Book-Zustand. SHA bleibt reine Identität.

Epoch-Opener sind unabhängige Exchange-Snapshots und taint-freie
`segment_start`-Anker. `reconnect_resync` wird nur mit angrenzendem,
book-hash-identischem Exchange-Snapshot anerkannt. `periodic_5m` und `shutdown`
sind ausschließlich Resume-Material innerhalb einer schon bewiesenen Epoch.

## 5. Silver-Schema

- `{result['schema']['epochs']}`
- `{result['schema']['level_changes']}`
- `{result['schema']['states_100ms']}`
- `{result['schema']['chunks']}`

Alle Zeitfelder stammen aus Integer-ns; DateTime64 ist materialisiert. Bestehende
v1/v1.2-Tabellen wurden nicht verändert.

## 6–7. Streaming, Warm-up und Boundaries

ClickHouse liefert Row-Blöcke in kanonischer Apply-Reihenfolge. Ein echter Gap
beendet Zustand und Epoch sofort. Fertige 100-ms-Buckets werden kausal emittiert
und nie rückwirkend verändert. Boundary-Checks: `{json.dumps(result['warmup_checks'], sort_keys=True)}`.

## 8–10. Vier Pilotfenster und Parität

- Episode 1: {episode['analysis_start']} bis {episode['analysis_end']}
- Reconnect 2026-09-07T21Z: {len(reconnect['gaps'])} Gap(s),
  {len(reconnect['epochs'])} neue sichere Epoch(s), keine Blindzeit-Silverzeilen.
- Cross-Segment 2026-09-10T09Z: Apply-Key
  {cross['first_apply_key']} bis {cross['last_apply_key']}, Ränge bis 117.
- Benchmark: {benchmark['analysis_start']} bis {benchmark['analysis_end']}
  ({benchmark['duration_minutes']} Minuten).

Episode 1:

- Level-Changes: {parity['level_changes_actual']}/{parity['level_changes_reference']}
- LC-Hash identisch: {parity['actual_lc_hash'] == parity['reference_lc_hash']}
- states_100ms: {parity['states_100ms_actual']}/{parity['states_100ms_reference']}
- States bytefachlich identisch: {parity['states_exact']}
- Status: `{parity['status']}`

## 11. Idempotenz und Resume

- Episode Wiederholung: `{episode['second_identical_build']['status']}`
- Benchmark kontrollierter Abbruch: `{benchmark['interrupted_probe']['status']}`
- Benchmark Resume: `{benchmark['resumed_build']['status']}`
- Benchmark Wiederholung: `{benchmark['second_identical_build']['status']}`
- Persistierte Zähler: `{json.dumps(benchmark['persisted_counts'], sort_keys=True)}`

## 12–14. Performance und Ressourcen

- Historische Cross-Window-Baseline (nur Orientierung):
  {performance['baseline_seconds_per_market_minute']} s/Marktminute
- Episode 1: {performance['episode1_seconds_per_market_minute']} s/Marktminute
- 15m-Benchmark: {performance['benchmark_seconds_per_market_minute']} s/Marktminute
- Cross-Window-Speedup: {performance['benchmark_speedup']}x (vorläufig;
  kein kontrollierter Same-Input-Vergleich)
- Episode Replay-Aufteilung: `{json.dumps(performance['episode_timings'], sort_keys=True)}`
- Benchmark Replay-Aufteilung: `{json.dumps(performance['benchmark_timings'], sort_keys=True)}`
- Episode kanonisches Hashing: {performance['episode_hashing_s']} s
- Episode ClickHouse-Insert: {episode['persist'].get('insert_s', 0)} s
- Benchmark ClickHouse-Insert: {benchmark['resumed_build'].get('insert_s', 0)} s
- Persistierte Abschlussverifikation: {performance['verification_s']} s
- Deltas/s: {performance['benchmark_deltas_per_second']}
- Level-Changes/s: {performance['benchmark_level_changes_per_second']}
- Insert-Zeilen/s: {performance['benchmark_insert_rows_per_second']}
- 164h-Prognose, keine garantierte Laufzeit:
  {performance['estimated_164h_hours']} Stunden
- Peak RSS: {result['resource_usage']['peak_rss_kb']} KiB
- Gesamtlaufzeit Pilot: {result['resource_usage']['elapsed_s']} s
- Worker: 1

## 15–18. Tests, Git, Risiken und Empfehlung

Unit- und Produktionspfadtests decken Anchor-Provenance, Gap/Taint,
Cross-Segment-Replay, rückläufige Eventzeit, kausale Buckets, Boundary-Regeln,
Chain-/Epoch-Hash-Hard-Stops sowie Resume/Idempotenz ab. Der Worktree bleibt
absichtlich uncommitted; `runs/**` bleibt untracked/ignored.

Restrisiko: Dies ist ein begrenzter Vierfenster-Pilot, keine 164-Stunden-
Validierung. Nächster Schritt ist ein separat autorisierter, stufenweiser
Mehr-Epoch-Pilot mit denselben Hard-Stops — noch kein Full-Build.
""",
        encoding="utf-8",
    )


def main() -> int:
    run_dir = Path(
        "obfull_research_engine/runs/clickhouse_research_store_pilot_v1"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_epoch_aware_pilot()
        write_report(
            result, run_dir / "EPOCH_AWARE_SILVER_BUILDER_V1_3_PILOT.md"
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
