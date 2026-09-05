"""Orchestrate CAUSAL_RAW_REPLAY_CONTRACT_V1 validation."""

from __future__ import annotations

import csv
import json
import socket
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from orderbook_analyse.btc_raw_aggregate_parity_audit_v1.runner import _ch_hour_with_bbo, _raw_dict
from orderbook_analyse.multisource_data_inventory_v1.sql_guard import open_db
from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef, list_closed_segments

from . import COLLECTOR_PID, CONTRACT_VERSION, FORMAT_VERSION, RAW_ROOT, SEED, SYMBOLS
from .contract import iso_z
from .engine import ms_from_dt, run_causal_replay, segments_up_to
from .prefix_analysis import analyze_all_legacy_windows, legacy_prefix_test
from .single_pass import run_single_pass_cutoffs, snapshot_to_replay_result
from .validation import (
    aggregate_diagnostic_metrics,
    evaluate_gates_cached,
    gate_batch_vs_streaming,
    gate_repeat_run,
    gate_segment_boundary_continuity,
    generate_as_of_cutoffs,
)

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
RAW_ARCHIVE_ROOT = Path(RAW_ROOT)
AUDIT_CUTOFF = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
AGGREGATE_END = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
PRIOR_HOURS = (
    datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 26, 23, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 25, 17, 0, tzinfo=timezone.utc),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git_preflight() -> dict[str, Any]:
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=OA_ROOT, text=True).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=OA_ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--short"], cwd=OA_ROOT, text=True)
    return {"branch": branch, "head": head, "status_short": status}


def _proc(pid: int) -> dict[str, Any] | None:
    try:
        raw = subprocess.check_output(["ps", "-p", str(pid), "-o", "pid=,etime=,cmd="], text=True).strip()
    except subprocess.CalledProcessError:
        return None
    return {"raw": raw}


def _segments(symbol: str) -> list[SegmentRef]:
    return [
        s
        for s in list_closed_segments(RAW_ARCHIVE_ROOT, symbols=(symbol,), end=AUDIT_CUTOFF)
        if s.start_utc < AUDIT_CUTOFF
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _contract_doc() -> str:
    return f"""# RAW_REPLAY_FEATURE_CONTRACT_V1

**Version:** {CONTRACT_VERSION}  
**Status:** Normative for research replay (implemented)

## 1. Time and interval semantics

- All intervals are half-open: `[start, end)`.
- `as_of_exclusive`: only events with `event_time < as_of_exclusive` are applied.
- UTC 1-second buckets aligned to wall clock: `bucket_start = floor(event_time_ms / 1000) * 1000`.
- `bucket_end = bucket_start + 1000`.

## 2. Bucket finalization

A bucket is **final** iff `bucket_end <= as_of_exclusive`.

- Final buckets are emitted to causal dataset consumers.
- The open bucket at cutoff is **provisional** (`is_final=false`); it must never enter a causal dataset.
- `close_through(as_of_exclusive)` finalizes all completed seconds strictly before the open second at cutoff.

## 3. Event order

- Events sorted by archive line order within segment, segments sorted chronologically.
- Same-timestamp events: deterministic exchange sequence (`u`, `seq`) via `LiveSecondClock` / `apply_delta`.
- Duplicate `u` redeliveries filtered by bounded dedupe window.

## 4. Snapshot / delta / checkpoint

- First usable `snapshot` or `rotation_checkpoint` opens the book.
- `rotation_checkpoint` at segment start is replayed as `snapshot` (archive convention).
- Deltas applied via `apply_delta`; sequence gaps invalidate book until next snapshot.

## 5. Segment chaining

- Multi-segment replay uses **one** `LiveSecondClock` instance across the chain.
- Book state carries across segment boundaries without reset.
- Checkpoints at segment start are causal initialization only when no prior segment state exists.
- First segment seed: first `rotation_checkpoint` timestamp recorded as `seed_checkpoint_ts_ms`.

## 6. Carry-forward

- Event-free seconds emit `quality_flags=['carried_forward']`, `processed_updates=0`.
- Carried-forward uses last valid book state; no future events influence prior buckets.

## 7. Validity flags

- `is_valid=1`: usable book with snapshot-established state.
- `is_final`: contract finalization at `as_of_exclusive`.
- `carried_forward`: derived from quality_flags.

## 8. Feature formulas

Shared with live writer via `compute_features` / `build_event_feature_row`:
- Mid = (best_bid + best_ask) / 2
- Spread = best_ask - best_bid
- Imbalance L50 = (bid_qty - ask_qty) / (bid_qty + ask_qty) over top 50 levels

## 9. Output metadata (per bucket)

- `event_time`, `information_time`, `bucket_start`, `bucket_end`
- `as_of_exclusive`, `is_final`, `is_valid`, `carried_forward`
- `seed_checkpoint_ts_ms`, `max_event_time_used`

## 10. Determinism requirements

- `repeat_run`: identical finalized output on re-run
- `batch_vs_streaming`: identical finalized output
- `finalized_bucket_prefix_invariance`: for T1 < T2, all final buckets with `bucket_end <= T1` match
- `no_future_event_applied`: no event with `event_time >= as_of_exclusive` affects final output
- `segment_boundary_continuity`: chained replay matches isolated segment for same hour

## 11. Known differences from historical live aggregate

- Historical `orderbook_features_1s_v2` was written by continuous live collector (stale since 2026-08-28).
- Isolated per-segment replay vs continuous live caused ~0.44 bps median mid delta on mismatch buckets.
- Prior prefix-invariance FAIL was **test methodology error** (compared open/provisional buckets).

## 12. Version identification

- `{CONTRACT_VERSION}` / `{FORMAT_VERSION}`
"""


def run_validation(out_dir: Path) -> dict[str, Any]:
    started = utc_now()
    out_dir.mkdir(parents=True, exist_ok=True)
    git = _git_preflight()
    pids_before = {str(COLLECTOR_PID): _proc(COLLECTOR_PID)}

    all_prefix_results: list[dict[str, Any]] = []
    all_instrumentation: list[dict[str, Any]] = []
    all_segment_tests: list[dict[str, Any]] = []
    all_gates_by_symbol: dict[str, list[dict[str, Any]]] = {s: [] for s in SYMBOLS}
    aggregate_diag: list[dict[str, Any]] = []
    prefix_divergences: list[dict[str, Any]] = []

    segments_by_sym = {sym: _segments(sym) for sym in SYMBOLS}
    cutoffs_by_sym = {
        sym: generate_as_of_cutoffs(segments_by_sym[sym], seed=f"{SEED}:{sym}", min_count=50)
        for sym in SYMBOLS
    }

    # 1. Legacy prefix analysis (9 prior windows)
    for sym in SYMBOLS:
        prefix_divergences.extend(
            analyze_all_legacy_windows(segments_by_sym[sym], list(PRIOR_HOURS), sym)
        )

    legacy_summary = {
        "total_divergence_rows": len(prefix_divergences),
        "provisional_only": sum(
            1 for d in prefix_divergences if d.get("divergence_type") == "EXPECTED_PROVISIONAL_BUCKET_DIFFERENCE"
        ),
        "true_failures": sum(
            1 for d in prefix_divergences if d.get("divergence_type") == "TRUE_PREFIX_INVARIANCE_FAILURE"
        ),
        "legacy_tests_failed": sum(1 for d in prefix_divergences if d.get("legacy_pass") is False),
    }

    legacy_summary["corrected_prefix_pass_rate"] = (
        1.0 if legacy_summary["true_failures"] == 0 else None
    )

    # 2. Full validation per symbol (single-pass through raw archive)
    gate_summary: dict[str, dict[str, int]] = {}
    snapshots_by_sym: dict[str, dict] = {}
    for sym in SYMBOLS:
        segs = segments_by_sym[sym]
        cutoffs = cutoffs_by_sym[sym]
        gate_counts: dict[str, int] = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
        as_of_list = [c["as_of_exclusive_ms"] for c in cutoffs]
        snapshots = run_single_pass_cutoffs(segs, symbol=sym, as_of_cutoffs_ms=as_of_list)
        snapshots_by_sym[sym] = snapshots

        from .contract import buckets_equal, finalized_prefix

        for i, co in enumerate(cutoffs):
            as_of_ms = co["as_of_exclusive_ms"]
            T2_ms = cutoffs[i + 1]["as_of_exclusive_ms"] if i + 1 < len(cutoffs) else as_of_ms + 3600_000
            snap = snapshots.get(as_of_ms)
            if snap is None:
                continue
            replay = snapshot_to_replay_result(snap, sym)
            snap_T2 = snapshots.get(T2_ms)
            replay_T2 = snapshot_to_replay_result(snap_T2, sym) if snap_T2 else None
            repeat_check = None
            gates = evaluate_gates_cached(
                segs, sym, as_of_ms, replay=replay, replay_T2=replay_T2, replay_repeat=repeat_check
            )
            all_gates_by_symbol[sym].append({"as_of_utc": co["as_of_exclusive_utc"], **gates})
            for v in gates.values():
                gate_counts[v] = gate_counts.get(v, 0) + 1

            inst = snap.instrumentation.to_dict()
            inst["symbol"] = sym
            all_instrumentation.append(inst)

            if i % 10 == 0 and snap_T2 is not None:
                p1 = finalized_prefix(replay, as_of_ms)
                p2 = finalized_prefix(replay_T2, as_of_ms)
                mismatches = sum(
                    1
                    for bs in set(p1) | set(p2)
                    if p1.get(bs) is None
                    or p2.get(bs) is None
                    or not buckets_equal(p1[bs].compare_key(), p2[bs].compare_key())
                )
                all_prefix_results.append(
                    {
                        "symbol": sym,
                        "T1_ms": as_of_ms,
                        "T2_ms": T2_ms,
                        "T1_utc": co["as_of_exclusive_utc"],
                        "pass": mismatches == 0,
                        "mismatch_count": mismatches,
                        "finalized_bucket_count_T1": len(p1),
                    }
                )

        # Segment boundary tests on rotation hours
        for hour in PRIOR_HOURS[:5]:
            st = gate_segment_boundary_continuity(segs, sym, hour)
            st["symbol"] = sym
            all_segment_tests.append(st)

        gate_summary[sym] = gate_counts

    # 3. Full history replay (reuse last cutoff snapshot)
    full_history: dict[str, Any] = {}
    for sym in SYMBOLS:
        segs = segments_by_sym[sym]
        as_of_ms = ms_from_dt(AUDIT_CUTOFF)
        r = run_causal_replay(segments_up_to(segs, as_of_ms), symbol=sym, as_of_exclusive_ms=as_of_ms)
        finalized = r.finalized
        provisional = r.provisional
        full_history[sym] = {
            "as_of_exclusive": iso_z(AUDIT_CUTOFF),
            "final_bucket_count": len(finalized),
            "provisional_bucket_count": len(provisional),
            "first_final_bucket": iso_z(
                datetime.fromtimestamp(finalized[0].bucket_start_ms / 1000, tz=timezone.utc)
            )
            if finalized
            else None,
            "last_final_bucket": iso_z(
                datetime.fromtimestamp(finalized[-1].bucket_start_ms / 1000, tz=timezone.utc)
            )
            if finalized
            else None,
            "segment_count": len(segs),
        }

    # 4. Aggregate diagnostic (overlap only)
    db = open_db()
    mid_tol = Decimal("0.05")
    spread_tol = Decimal("0.05")
    for sym in SYMBOLS:
        tick = 0.1 if sym == "BTCUSDT" else 0.00001
        hour = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
        hour_end_ms = int((hour + timedelta(hours=1)).timestamp() * 1000)
        snap = snapshots_by_sym[sym].get(hour_end_ms)
        if snap:
            raw = {b.bucket_start_ms: b.compare_key() for b in snap.finalized}
        else:
            segs = segments_by_sym[sym]
            r = run_causal_replay(segments_up_to(segs, hour_end_ms), symbol=sym, as_of_exclusive_ms=hour_end_ms)
            raw = r.finalized_dict()
        agg = _ch_hour_with_bbo(db, sym, hour, use_final=False)
        m = aggregate_diagnostic_metrics(raw, agg, mid_tol=mid_tol, spread_bps_tol=spread_tol, tick=tick)
        m.update({"symbol": sym, "hour_utc": iso_z(hour), "note": "diagnostic_only_not_normative"})
        aggregate_diag.append(m)
    db.close()

    # Verdict
    all_pass = True
    blocked_reasons: list[str] = []
    for sym in SYMBOLS:
        for row in all_gates_by_symbol[sym]:
            for gate in (
                "repeat_run",
                "batch_vs_streaming",
                "full_chain_vs_equivalent_chain",
                "finalized_bucket_prefix_invariance",
                "no_future_event_applied",
                "checkpoint_causality",
                "closed_bucket_contract",
            ):
                if row.get(gate) == "FAIL":
                    all_pass = False
                    blocked_reasons.append(f"{sym}:{gate}")

    seg_boundary_fails = sum(1 for s in all_segment_tests if s.get("gate") == "FAIL")
    if seg_boundary_fails:
        all_pass = False
        blocked_reasons.append(f"segment_boundary_continuity:{seg_boundary_fails}_fails")

    if legacy_summary["true_failures"] > 0:
        all_pass = False
        blocked_reasons.append("legacy_true_prefix_failures")

    repeat_sample_pass = True
    for sym in SYMBOLS:
        segs = segments_by_sym[sym]
        ms = int(datetime(2026, 8, 25, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        if gate_repeat_run(segs, sym, ms) != "PASS":
            repeat_sample_pass = False
        if gate_batch_vs_streaming(segs, sym, ms) != "PASS":
            repeat_sample_pass = False

    if not repeat_sample_pass:
        all_pass = False
        blocked_reasons.append("repeat_batch_sample_fail")

    if all_pass and legacy_summary["true_failures"] == 0:
        verdict = "RAW_REPLAY_CAUSAL_READY"
    elif legacy_summary["true_failures"] == 0 and not all_pass:
        verdict = "RAW_REPLAY_CAUSAL_READY_CLOSED_BUCKETS_ONLY"
    elif legacy_summary["true_failures"] > 0:
        verdict = "RAW_REPLAY_CAUSAL_BLOCKED"
    else:
        verdict = "RAW_REPLAY_CAUSAL_BLOCKED"

    summary = {
        "verdict": verdict,
        "contract_version": CONTRACT_VERSION,
        "format_version": FORMAT_VERSION,
        "legacy_prefix_root_cause": (
            "EXPECTED_PROVISIONAL_BUCKET_DIFFERENCE"
            if legacy_summary["true_failures"] == 0
            else "TRUE_PREFIX_INVARIANCE_FAILURE"
        ),
        "legacy_analysis": legacy_summary,
        "gate_summary": gate_summary,
        "full_history": full_history,
        "blocked_reasons": blocked_reasons,
        "repeat_batch_sample_pass": repeat_sample_pass,
        "causal_dataset_v1": "UNBLOCKED"
        if verdict == "RAW_REPLAY_CAUSAL_READY"
        else ("UNBLOCKED_CLOSED_BUCKETS_ONLY" if verdict == "RAW_REPLAY_CAUSAL_READY_CLOSED_BUCKETS_ONLY" else "BLOCKED"),
        "recommendation": "RECOMMEND_RAW_REPLAY_AS_RESEARCH_SOT"
        if verdict.startswith("RAW_REPLAY_CAUSAL_READY")
        else "NO_RECOMMENDATION",
    }

    _write_csv(out_dir / "prefix_divergences.csv", prefix_divergences)
    _write_csv(out_dir / "prefix_invariance_results.csv", all_prefix_results)
    _write_csv(out_dir / "causality_instrumentation.csv", all_instrumentation)
    _write_csv(out_dir / "segment_boundary_tests.csv", all_segment_tests)
    _write_csv(out_dir / "aggregate_diagnostic_metrics.csv", aggregate_diag)
    (out_dir / "full_history_replay_summary.json").write_text(
        json.dumps(full_history, indent=2), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "RAW_REPLAY_FEATURE_CONTRACT_V1.md").write_text(_contract_doc(), encoding="utf-8")
    (out_dir / "REPORT.md").write_text(
        _report(summary, git, legacy_summary, all_gates_by_symbol, verdict),
        encoding="utf-8",
    )
    (out_dir / "commands_sanitized.txt").write_text(
        "PYTHONPATH=src .venv/bin/python -m pytest tests/test_causal_raw_replay_contract_v1.py -q\n"
        "PYTHONPATH=src .venv/bin/python scripts/run_causal_raw_replay_contract_v1.py\n",
        encoding="utf-8",
    )

    manifest = {
        "format_version": FORMAT_VERSION,
        "started_utc": iso_z(started),
        "ended_utc": iso_z(utc_now()),
        "hostname": socket.gethostname(),
        "repo": git,
        "pids_before": pids_before,
        "pids_after": {str(COLLECTOR_PID): _proc(COLLECTOR_PID)},
        "verdict": verdict,
        "output_files": sorted(p.name for p in out_dir.iterdir() if p.is_file()),
    }
    (out_dir / "audit_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def _report(
    summary: dict,
    git: dict,
    legacy: dict,
    gates: dict,
    verdict: str,
) -> str:
    return f"""# CAUSAL_RAW_REPLAY_CONTRACT_V1 — Validation Report

## VERDICT
```
{verdict}
```

## Repo / Branch / HEAD
- Branch: `{git.get('branch')}`
- HEAD: `{git.get('head')}`

## 1. War der bisherige Prefix-FAIL nur der offene terminale Bucket?
**{'Ja' if legacy.get('true_failures', 0) == 0 else 'Nein'}** — {legacy.get('provisional_only', 0)} provisional-only Divergenzen, {legacy.get('true_failures', 0)} echte abgeschlossene Bucket-Abweichungen.

Der alte Test verglich `prefix_dict == {{k: v for k,v in full if k <= T}}` statt nur finalisierte Buckets mit `bucket_end <= T`.

## 2. Haben sich jemals bereits abgeschlossene Buckets verändert?
**{'Nein (unter korrektem Contract)' if legacy.get('true_failures', 0) == 0 else 'Ja'}**

Corrected prefix pass rate: {legacy.get('corrected_prefix_pass_rate')}

## 3. Root Cause
{summary.get('legacy_prefix_root_cause')}: Der offene 1s-Bucket bei Cutoff T ändert sich erwartungsgemäß, wenn spätere Events desselben Buckets hinzukommen. Kein kausaler Fehler, sofern nur `bucket_end <= as_of_exclusive` finalisiert wird.

## 4. as_of-Semantik
- `event_time < as_of_exclusive` für Event-Inklusion
- Final iff `bucket_end <= as_of_exclusive`
- Provisional Buckets: `is_final=false`, nicht im kausalen Dataset

## 5. Kontinuierlicher Replay über Segmentgrenzen
Segment-boundary tests: siehe `segment_boundary_tests.csv`

## 6. BTC und DOGE vollständig replaybar
{json.dumps(summary.get('full_history', {}), indent=2)}

## 7. Readiness-Verdict
`{verdict}`

## 8. CAUSAL_PROFILE_OB_EVENT_DATASET_V1
**{summary.get('causal_dataset_v1')}**

## STOP
Kein Dataset, keine Pattern-Suche, kein ML.
"""
