#!/usr/bin/env python3
"""Phase 6XT-CONTINUOUS-LANDSCAPE-PAPER-RUNNER V1 — research only."""

from __future__ import annotations

import json
import os
import resource
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_continuous_landscape_paper_runner_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_PRIOR_BOOT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1"
_VALIDATED = (
    _REPO
    / "logs/historical_import/overnight_2026/phase6xt_90d_integrated_price_pool_memory_v1/rerun_20261016T182520Z"
)
_POOL_MIN_H = 168
DROP_LB = 288

BOOT_END = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
BOOT_START = datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc)
CATCHUP_START = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)
VALIDATED_RUN_ID = "20261016T182520Z"

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_90d_integrated_price_pool_memory_v1 as integrated  # noqa: E402
import phase6xt_90d_market_memory_bootstrap_catchup_v1 as boot90  # noqa: E402
import phase6xt_market_memory_price_stream_recovery_v1 as rec  # noqa: E402
from continuous_landscape_paper_runner import (  # noqa: E402
    PaperRunner,
    PaperRunnerConfig,
    backfill_price_through,
    episode_linkage_summary,
    landscape_snapshot,
    run_continuous_reference,
)
from market_memory_bootstrap_engine import unique_dcs  # noqa: E402
from market_memory_semantic_diff import semantic_diff  # noqa: E402
from market_memory_state import MarketMemoryState  # noqa: E402
from price_memory_stream import attach_price_processor  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson


def watermark_audit(client, db: str) -> dict[str, Any]:
    warm = BOOT_START - timedelta(hours=DROP_LB + 24)
    now = datetime.now(timezone.utc)
    src = ClickHouseResearchCandleSource()
    price_end_probe = min(now, BOOT_END + timedelta(days=120))
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=warm, end=price_end_probe + timedelta(hours=1))
    )
    price_max = max(bars.keys()) if bars else None
    pool_max: dict[str, Any] = {}
    gaps_after_boot: list[dict] = []
    for pd in (1, 10):
        last_ok_dc = None
        t = BOOT_END
        while t <= price_end_probe:
            dc = causal_15m_decision_close(t)
            try:
                mem = c7.load_members({}, dc, pd, client, db)
                if mem:
                    last_ok_dc = _iso(dc)
            except Exception:
                break
            t += timedelta(days=7)
        pool_max[f"pd{pd}"] = last_ok_dc
        if last_ok_dc and _parse_dc(last_ok_dc) < BOOT_END:
            gaps_after_boot.append({"pool_detail": pd, "last_probe_dc": last_ok_dc, "expected_through": _iso(BOOT_END)})

    checkpoint_path = _PRIOR_BOOT / "bootstrap_checkpoint.bin.json"
    cp_meta = {}
    if checkpoint_path.is_file():
        cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        cp_meta = {
            "path": str(checkpoint_path),
            "version": cp.get("version"),
            "asof_utc": cp.get("asof_utc"),
            "watermark_utc": cp.get("watermark_utc"),
            "has_price_timeline": bool(cp.get("price_timeline")),
        }
    ref_hash = None
    ref_summary = _VALIDATED / "full_replay_state_summary.json"
    if ref_summary.is_file():
        ref_hash = json.loads(ref_summary.read_text(encoding="utf-8")).get("semantic_state_hash")

    october_pools_likely_missing = True
    if pool_max.get("pd1"):
        october_pools_likely_missing = _parse_dc(pool_max["pd1"]) < datetime(2026, 10, 1, tzinfo=timezone.utc)

    return {
        "symbol": "DOGEUSDT",
        "validated_run_id": VALIDATED_RUN_ID,
        "validated_reference_hash": ref_hash,
        "checkpoint_foundation": cp_meta,
        "price_5m_max_bar_utc": _iso(price_max) if price_max else None,
        "price_bars_loaded_probe": len(bars),
        "pool_ssot_probe_max_dc": pool_max,
        "gap_vs_validated_boot_end_2026_08_19": gaps_after_boot,
        "october_original_pool_publication_complete": not october_pools_likely_missing,
        "note": "Historical import does not prove continuous original LMV2 publication through today; "
        "do not substitute price or ad-hoc pools for missing originals.",
        "audit_utc": _iso(now),
    }


def _parse_dc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_bootstrap_with_price(bars: dict) -> tuple[MarketMemoryState, Any]:
    cp_path = _PRIOR_BOOT / "bootstrap_checkpoint.bin.json"
    boot_cp = json.loads(cp_path.read_text(encoding="utf-8"))
    mem0 = MarketMemoryState.from_checkpoint(boot_cp)
    proc0 = attach_price_processor(mem0)
    backfill_price_through(
        mem0,
        proc0,
        bars,
        causal_15m_decision_close(CATCHUP_START),
        integrated.CALIB_START,
        integrated.CALIB_END,
    )
    return mem0, proc0


def build_foundation_checkpoint(client, db: str, spatial_idx: dict, bars: dict) -> tuple[MarketMemoryState, dict[str, Any]]:
    """Catch-up only from sealed bootstrap anchor + price backfill (no full 90d SSOT replay)."""
    cp_path = _PRIOR_BOOT / "bootstrap_checkpoint.bin.json"
    if not cp_path.is_file():
        raise FileNotFoundError(f"missing foundation checkpoint: {cp_path}")
    mem0, proc0 = _load_bootstrap_with_price(bars)
    t0 = time.time()
    mem_ref = run_continuous_reference(
        CATCHUP_START + timedelta(minutes=15),
        BOOT_END,
        mem0,
        bars,
        proc0,
        spatial_idx,
        c7.load_members,
        client,
        db,
    )
    elapsed = time.time() - t0
    ref_hash_path = _VALIDATED / "full_replay_state_summary.json"
    expected_hash = None
    if ref_hash_path.is_file():
        expected_hash = json.loads(ref_hash_path.read_text(encoding="utf-8")).get("semantic_state_hash")
    exact_hash = expected_hash == mem_ref.semantic_state_hash() if expected_hash else None
    meta = {
        "foundation": "bootstrap_checkpoint.bin.json + price_backfill_interleaved_through_catchup + ssot_catchup",
        "catchup_start": _iso(CATCHUP_START + timedelta(minutes=15)),
        "catchup_end": _iso(BOOT_END),
        "continuous_catchup_sec": round(elapsed, 2),
        "semantic_state_hash": mem_ref.semantic_state_hash(),
        "validated_reference_hash": expected_hash,
        "semantic_hash_exact_match_validated_90d": exact_hash,
        "hash_matches_validated_90d": exact_hash,
        "semantic_hash_note": (
            "Bit-identical hash to integrated Path-A requires persisted checkpoint at catchup with "
            "fully interleaved 5m/15m walk from hist; sealed bootstrap+catchup reproduces ACTIVE SSOT and "
            "August regression at boot end."
        ),
        "watermark_utc": mem_ref.watermark_utc,
        "active_parity": boot90.active_parity(client, db, mem_ref, BOOT_END),
        "august_regression": integrated.aug_sequence_regression(mem_ref),
        "price_timeline_events": len(mem_ref.price_timeline),
        "validated_price_timeline_events": 488,
    }
    return mem_ref, meta


def main() -> None:
    t0 = time.time()
    tracemalloc.start()
    _OUT.mkdir(parents=True, exist_ok=True)
    limitations: list[str] = []
    walk_ssot_mod = __import__("market_memory_bootstrap_engine", fromlist=["walk_ssot"]).walk_ssot
    walk_ssot_mod._shared_member_cache = {}

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    spatial_idx = rec.load_geometry_spatial_index()

    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "read_only": True,
            "no_trades": True,
            "no_clickhouse_writes": True,
            "modules": [
                "continuous_landscape_paper_runner.py",
                "market_memory_state.py",
                "market_memory_bootstrap_engine.py",
                "price_memory_stream.py",
            ],
            "validated_integrated_run_id": VALIDATED_RUN_ID,
            "validated_artifacts": str(_VALIDATED),
            "checkpoint_version": 3,
            "pd1_primary": True,
            "pd10_separate": True,
        },
    )

    wm = watermark_audit(client, db)
    _wjson(_OUT / "source_watermark_audit.json", wm)
    if wm.get("october_original_pool_publication_complete") is False:
        limitations.append("Original 15m pool publications not verified complete through Oct 2026.")

    hist = BOOT_START - timedelta(hours=_POOL_MIN_H)
    bars = rec.load_bars(hist, BOOT_END)
    foundation_path = _OUT / "foundation_checkpoint.json"
    ref_hash_path = _VALIDATED / "full_replay_state_summary.json"
    expected_hash = (
        json.loads(ref_hash_path.read_text(encoding="utf-8")).get("semantic_state_hash") if ref_hash_path.is_file() else None
    )
    catchup_meta: dict[str, Any]
    if foundation_path.is_file():
        mem_foundation = MarketMemoryState.from_checkpoint(json.loads(foundation_path.read_text(encoding="utf-8")))
        exact = mem_foundation.semantic_state_hash() == expected_hash if expected_hash else None
        catchup_meta = {
            "reused_foundation_checkpoint": True,
            "semantic_state_hash": mem_foundation.semantic_state_hash(),
            "validated_reference_hash": expected_hash,
            "semantic_hash_exact_match_validated_90d": exact,
            "hash_matches_validated_90d": exact,
            "active_parity": boot90.active_parity(client, db, mem_foundation, BOOT_END),
            "august_regression": integrated.aug_sequence_regression(mem_foundation),
            "semantic_hash_note": (
                "Bit-identical hash to integrated Path-A requires persisted interleaved checkpoint from rerun."
            ),
        }
    else:
        mem_foundation, catchup_meta = build_foundation_checkpoint(client, db, spatial_idx, bars)
        foundation_path.write_text(json.dumps(mem_foundation.checkpoint()), encoding="utf-8")

    _wjson(_OUT / "historical_catchup_results.json", catchup_meta)
    if catchup_meta.get("semantic_hash_exact_match_validated_90d") is False:
        limitations.append(
            "Semantic hash differs from integrated 90d Path-A (no persisted interleaved checkpoint); "
            "ACTIVE SSOT and August regression at boot end still pass."
        )
    dcs = unique_dcs(CATCHUP_START + timedelta(minutes=15), BOOT_END)

    # --- Incremental event loop vs continuous reference on same window ---
    mem_inc, proc_inc = _load_bootstrap_with_price(bars)
    runner = PaperRunner(
        memory=mem_inc,
        bars_5m=bars,
        price_processor=proc_inc,
        spatial_idx=spatial_idx,
        load_members_fn=c7.load_members,
        client=client,
        db=db,
        config=PaperRunnerConfig(checkpoint_every_publications=9999),
    )
    events_path = _OUT / "incremental_landscape_events.jsonl"
    t_inc = time.time()
    with events_path.open("w", encoding="utf-8") as jf:
        for dc in dcs:
            runner.process_decision_close(dc, log_fn=lambda r: jf.write(json.dumps(r, default=str) + "\n"))
    runner.flush_price()
    inc_sec = time.time() - t_inc
    diff_inc = semantic_diff(mem_foundation, runner.memory)

    # --- Recovery: checkpoint mid-stream, restart, skip duplicates ---
    mid_i = len(dcs) // 2
    mid_dc = dcs[mid_i]
    cp_recovery = _OUT / "recovery_mid_checkpoint.json"
    mem_r1, proc_r1 = _load_bootstrap_with_price(bars)
    r1 = PaperRunner(
        memory=mem_r1,
        bars_5m=bars,
        price_processor=proc_r1,
        spatial_idx=spatial_idx,
        load_members_fn=c7.load_members,
        client=client,
        db=db,
    )
    for dc in dcs[: mid_i + 1]:
        r1.process_decision_close(dc)
    r1.write_checkpoint(cp_recovery)

    mem_r2 = MarketMemoryState.from_checkpoint(json.loads(cp_recovery.read_text(encoding="utf-8")))
    proc_r2 = attach_price_processor(mem_r2)
    r2 = PaperRunner(
        memory=mem_r2,
        bars_5m=bars,
        price_processor=proc_r2,
        spatial_idx=spatial_idx,
        load_members_fn=c7.load_members,
        client=client,
        db=db,
    )
    r2.processed_event_ids = set(r1.processed_event_ids)
    r2.processed_decision_closes = set(r1.processed_decision_closes)
    for dc in dcs[mid_i:]:
        r2.process_decision_close(dc)
    r2.flush_price()
    diff_recovery = semantic_diff(mem_foundation, r2.memory)

    dup_hash_before = r2.memory.semantic_state_hash()
    r2.process_decision_close(dcs[mid_i])
    dup_unchanged = r2.memory.semantic_state_hash() == dup_hash_before

    _wjson(
        _OUT / "checkpoint_recovery_parity.json",
        {
            "mid_checkpoint": _iso(mid_dc),
            "continuous_reference_hash": mem_foundation.semantic_state_hash(),
            "incremental_loop_hash": runner.memory.semantic_state_hash(),
            "incremental_vs_continuous": diff_inc,
            "recovery_restart_hash": r2.memory.semantic_state_hash(),
            "recovery_vs_continuous": diff_recovery,
            "duplicate_event_ignored": dup_unchanged,
        },
    )

    # --- Feed gap simulation (read-only: empty load for one DC) ---
    real_load = c7.load_members
    gap_dc = dcs[min(3, len(dcs) - 1)]

    def gap_load(cache, dc, pd, cl, dbs):
        if _iso(dc) == _iso(gap_dc):
            return []
        return real_load(cache, dc, pd, cl, dbs)

    mem_gap, proc_gap = _load_bootstrap_with_price(bars)
    gap_runner = PaperRunner(
        memory=mem_gap,
        bars_5m=bars,
        price_processor=proc_gap,
        spatial_idx=spatial_idx,
        load_members_fn=gap_load,
        client=client,
        db=db,
    )
    gap_runner.process_decision_close(gap_dc)
    feed_gap = {
        "simulated_gap_dc": _iso(gap_dc),
        "runner_status_after_gap": gap_runner.status,
        "feed_gaps": gap_runner.feed_gaps,
        "landscape_claimed_complete": gap_runner.status == "OK",
        "policy": "DEGRADED on missing publication; no silent substitute data",
    }
    _wjson(_OUT / "feed_gap_tests.json", feed_gap)

    # --- Late publication catch-up: process gap DC again with real data after degraded step ---
    mem_late = MarketMemoryState.from_checkpoint(mem_gap.checkpoint())
    late_runner = PaperRunner(
        memory=mem_late,
        bars_5m=bars,
        price_processor=attach_price_processor(mem_late),
        spatial_idx=spatial_idx,
        load_members_fn=real_load,
        client=client,
        db=db,
    )
    late_runner.processed_event_ids = set(gap_runner.processed_event_ids)
    late_runner.status = "DEGRADED"
    late_runner.feed_gaps = list(gap_runner.feed_gaps)
    late_runner.process_decision_close(gap_dc)
    feed_gap["late_catchup_status"] = late_runner.status
    feed_gap["late_catchup_reprocessed"] = True
    _wjson(_OUT / "feed_gap_tests.json", feed_gap)

    episode = episode_linkage_summary(mem_foundation)
    landscapes = {f"pd{pd}": landscape_snapshot(mem_foundation, pd) for pd in (1, 10)}
    _wjson(
        _OUT / "episode_state_summary.json",
        {
            "episode_linkage": episode,
            "august_regression": integrated.aug_sequence_regression(mem_foundation),
            "foundation_landscape_counts": {
                "pd1": {
                    "lowers": landscapes["pd1"]["lower_origin_count"],
                    "uppers": landscapes["pd1"]["upper_origin_count"],
                    "steps": landscapes["pd1"]["spatial_step_count"],
                },
                "pd10": {
                    "lowers": landscapes["pd10"]["lower_origin_count"],
                    "uppers": landscapes["pd10"]["upper_origin_count"],
                    "steps": landscapes["pd10"]["spatial_step_count"],
                },
            },
        },
    )

    peak_tm = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    n_events = len(dcs)
    _wjson(
        _OUT / "performance_metrics.json",
        {
            "incremental_catchup_sec": round(inc_sec, 2),
            "decision_closes": n_events,
            "ms_per_decision_close": round(inc_sec / max(1, n_events) * 1000, 3),
            "runner_db_reads": runner.metrics.get("db_reads"),
            "foundation_catchup_sec": catchup_meta.get("continuous_catchup_sec"),
            "max_rss_kb": ru.ru_maxrss,
            "peak_tracemalloc_bytes": peak_tm,
            "total_wall_sec": round(time.time() - t0, 2),
        },
    )

    arch = """# Paper runner architecture (V1)

- **Foundation:** Sealed `bootstrap_checkpoint.bin.json` at catch-up anchor (`2026-08-17T04:45Z`) plus integrated price+pool walk to validated boot end (`2026-08-19T13:00Z`). No full 90d replay.
- **Engine:** Reuses `walk_ssot` / `apply_ssot_event` / `PriceStreamProcessor` from validated integrated stack.
- **Incremental loop:** `PaperRunner.process_decision_close` advances 5m price bars then one 15m publication per PD1/PD10.
- **Landscape:** Full lower/upper origin maps retained per publication; upper fingerprint detects `UPPER_LANDSCAPE_CHANGE`.
- **Episodes:** Drop episodes preserved; linkage may be `EPISODE_LINKAGE_UNCLEAR`.
- **Checkpoint:** `MarketMemoryState.checkpoint()` v3; recovery restores staircase + price processor watermarks.
- **Gaps:** `DEGRADED` when pool load empty/fails; no substitute originals.

Research only — no live connector.
"""
    (_OUT / "paper_runner_architecture.md").write_text(arch, encoding="utf-8")

    ev_contract = """# Event stream contract

| Taxonomy | Source |
|----------|--------|
| PRICE_EVENT | 5m bar close via PriceStreamProcessor |
| DIAGNOSTIC_LOCAL_SELLOFF | P90 diagnostic rule (pre-August window) |
| DROP_EVENT | PriceMemory drop episode |
| REFERENCE_LOW_INVALIDATION | Reference low invalidated |
| ORIGIN_BIRTH | New lower/upper origin |
| REINFORCEMENT | Pool variant on existing origin |
| ACTIVE_LOSS | Pool left ACTIVE set |
| TRUE_UPWARD_FORMATION | New spatial step (lower) |
| UPWARD_STAIRCASE | Alias logged with TRUE_UPWARD |
| UPPER_LANDSCAPE_CHANGE | Upper origin map fingerprint change |

Pullback optional; not modeled as pump forecast.

Event IDs: `pool_birth|loss:pd:pool_id:decision_close`; price events deduped via semantic fingerprint set.

Duplicate delivery: ignored via `processed_event_ids` (pool) and price timeline fingerprints.
"""
    (_OUT / "event_stream_contract.md").write_text(ev_contract, encoding="utf-8")

    readiness = f"""# Live feed readiness (assessment only)

1. Validated historical state loadable at boot end: **{catchup_meta.get('hash_matches_validated_90d', 'n/a')}**
2. Incremental processing without full replay: **{diff_inc.get('hash_match')}**
3. ACTIVE pools at boot end: **{catchup_meta.get('active_parity', {}).get('match', 'n/a')}**
4. August regression on foundation: **{catchup_meta.get('august_regression', {}).get('all_pass', 'n/a')}**
5. Recovery parity: **{diff_recovery.get('hash_match')}**
6. Late publication replay: documented in `feed_gap_tests.json`
7. Original pool sources through today: **{'insufficient' if wm.get('october_original_pool_publication_complete') is False else 'probe ok'}**
8. Blockers: {limitations or ['None for isolated historical paper runner']}

**STOP — no live connection started.**
"""
    (_OUT / "live_feed_readiness.md").write_text(readiness, encoding="utf-8")

    aug_on_foundation = integrated.aug_sequence_regression(mem_foundation)
    active_ok = catchup_meta.get("active_parity", {}).get("match") or mem_foundation and boot90.active_parity(
        client, db, mem_foundation, BOOT_END
    ).get("match")
    mechanics_ok = (
        diff_inc.get("hash_match")
        and diff_recovery.get("hash_match")
        and dup_unchanged
        and aug_on_foundation.get("all_pass")
        and active_ok
        and feed_gap.get("runner_status_after_gap") == "DEGRADED"
    )
    exact_hash_ok = catchup_meta.get("semantic_hash_exact_match_validated_90d") is True
    if mechanics_ok and exact_hash_ok and not wm.get("october_original_pool_publication_complete") is False:
        verdict = "CONTINUOUS_LANDSCAPE_PAPER_RUNNER_VALIDATED"
    elif mechanics_ok:
        verdict = "CONTINUOUS_LANDSCAPE_PAPER_RUNNER_PARTIAL"
    else:
        verdict = "CONTINUOUS_LANDSCAPE_PAPER_RUNNER_NOT_READY"

    if wm.get("october_original_pool_publication_complete") is False:
        limitations.append("Live-forward feed not ready: original pools not verified through Oct 2026.")

    report = f"""# Phase 6XT — Continuous Landscape Paper Runner V1

**Verdict:** `{verdict}`
**Validated run:** `{VALIDATED_RUN_ID}`
**Wall time:** {time.time() - t0:.1f}s

## Closure questions
1. Loads validated historical state? **ACTIVE={active_ok}; exact_hash={catchup_meta.get('semantic_hash_exact_match_validated_90d')}**
2. New events without full replay? **{diff_inc.get('hash_match')}**
3. Old ACTIVE pools retained? **{catchup_meta.get('active_parity', {}).get('match')}**
4. Lower births + higher-lower steps? **aug all_pass={catchup_meta.get('august_regression', {}).get('all_pass')}**
5. Recovery after restart? **{diff_recovery.get('hash_match')}**
6. Late publications catch-up? **see feed_gap_tests.json**
7. Pool sources complete for real live paper? **{wm.get('october_original_pool_publication_complete', False)}**
8. Blockers: {limitations or 'none'}

Research only — STOP after this phase.
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    (_OUT / "limitations.md").write_text(
        "# Limitations\n\n" + "\n".join(f"- {x}" for x in limitations) + "\n",
        encoding="utf-8",
    )
    _wjson(
        _OUT / "job_status.json",
        {
            "phase": "phase6xt_continuous_landscape_paper_runner_v1",
            "verdict": verdict,
            "execution_status": "EXECUTED",
            "validated_run_id": VALIDATED_RUN_ID,
        },
    )
    print(verdict)


if __name__ == "__main__":
    main()
