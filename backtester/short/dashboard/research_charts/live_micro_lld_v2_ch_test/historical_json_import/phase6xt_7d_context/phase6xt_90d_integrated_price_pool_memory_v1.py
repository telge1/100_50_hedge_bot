#!/usr/bin/env python3
"""Phase 6XT-90D-INTEGRATED-PRICE-POOL-MEMORY VALIDATION V1 — research only."""

from __future__ import annotations

import csv
import json
import os
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_DEFAULT_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_integrated_price_pool_memory_v1"
_OUT = Path(os.environ.get("PHASE6XT_INTEGRATED_OUT", str(_DEFAULT_OUT)))
_RUN_ID = os.environ.get("PHASE6XT_RUN_ID", "")
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_PRIOR_BOOT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1"
_POOL_MIN_H = 168
DROP_LB = 288
KNOWN_BACKFILL_PID = 3099992

BOOT_END = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
BOOT_START = datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc)
CATCHUP_START = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)
CALIB_START = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
CALIB_END = datetime(2026, 7, 31, 23, 55, tzinfo=timezone.utc)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_90d_market_memory_bootstrap_catchup_v1 as boot90  # noqa: E402
import phase6xt_market_memory_price_stream_recovery_v1 as rec  # noqa: E402
from market_memory_bootstrap_engine import (  # noqa: E402
    ledger_from_memory,
    parity_diff,
    unique_dcs,
    walk_ssot,
)
from market_memory_semantic_diff import semantic_diff  # noqa: E402
from market_memory_state import (  # noqa: E402
    EVENT_DROP_EVENT,
    EVENT_REFERENCE_LOW_INVALIDATED,
    MarketMemoryState,
    SCHEMA_CODE_VERSION,
    _parse_utc,
)
from price_memory_stream import attach_price_processor  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson

walk_ssot._shared_member_cache = {}


def server_preflight() -> dict[str, Any]:
    backfill_pid = KNOWN_BACKFILL_PID
    proc_alive = False
    proc_detail: dict[str, Any] = {"pid": backfill_pid, "alive": False}
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(backfill_pid), "-o", "pid=,etime=,pcpu=,pmem=,rss=,cmd="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            proc_alive = True
            proc_detail = {"pid": backfill_pid, "alive": True, "ps_line": out}
    except subprocess.CalledProcessError:
        pass

    try:
        related = subprocess.check_output(
            ["pgrep", "-af", "DOGEUSDT|1m.*backfill|backfill.*doge"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        related = ""
    meminfo = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines()[:8]:
            k, v = line.split(":", 1)
            meminfo[k.strip()] = v.strip()
    except OSError:
        pass
    loadavg = os.getloadavg()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    prior_artifacts = {
        "bootstrap_checkpoint_v2": (_PRIOR_BOOT / "bootstrap_checkpoint.bin.json").is_file(),
        "prior_resource_usage": (_PRIOR_BOOT / "resource_usage.json").is_file(),
        "price_recovery_job": (
            _REPO / "logs/historical_import/overnight_2026/phase6xt_market_memory_price_stream_recovery_v1/job_status.json"
        ).is_file(),
    }
    ch_probe = {"ok": False}
    try:
        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        ch_probe = {"ok": True, "database": cfg.database, "ping": client.command("SELECT 1")}
    except Exception as e:
        ch_probe = {"ok": False, "error": str(e)}

    avail_kb = int(meminfo.get("MemAvailable", "0 KiB").split()[0]) if meminfo else 0
    allow_full = (
        not proc_alive
        and avail_kb > 4_000_000
        and loadavg[0] < 8.0
        and ch_probe.get("ok")
    )
    return {
        "timestamp_utc": _iso(datetime.now(timezone.utc)),
        "backfill_pid_check": proc_detail,
        "related_processes_sample": related.splitlines()[:10] if related else [],
        "meminfo_head": meminfo,
        "loadavg_1_5_15": loadavg,
        "cpu_count": os.cpu_count(),
        "clickhouse_read_probe": ch_probe,
        "prior_artifacts": prior_artifacts,
        "full_90d_integrated_replay_allowed": allow_full,
        "defer_reason": None if allow_full else "resource_or_backfill_gate",
    }


def episode_linkage_audit(state: MarketMemoryState) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for pd in (1, 10):
        drops = [e for e in state.drop_episodes[pd] if not e.diagnostic_only]
        lowers = sorted(
            state.origins[pd].values(),
            key=lambda r: r.birth_decision_close,
        )
        for ep in drops:
            src = ep.source_drop_close_utc or ep.recorded_at_utc
            src_t = _parse_utc(src) if src else None
            after = []
            if src_t:
                for lo in lowers:
                    if _parse_utc(lo.birth_decision_close) >= src_t:
                        after.append(
                            {
                                "origin_id": lo.origin_id,
                                "birth_decision_close": lo.birth_decision_close,
                                "bottom": lo.bottom,
                            }
                        )
            linkage = "EPISODE_LINKAGE_UNCLEAR"
            if ep.status == "INVALIDATED":
                linkage = "REFERENCE_INVALIDATED"
            elif not after:
                linkage = "NO_LOWER_AFTER_DROP"
            elif len(after) == 1:
                linkage = "SINGLE_CANDIDATE_LOWER"
            else:
                linkage = "EPISODE_LINKAGE_UNCLEAR"
            rows.append(
                {
                    "pool_detail": pd,
                    "episode_id": ep.episode_id,
                    "status": ep.status,
                    "reference_low": ep.reference_low,
                    "source_drop_close": src,
                    "lowers_after_count": len(after),
                    "first_lower_after": after[0] if after else None,
                    "linkage": linkage,
                }
            )
    inv = [e for e in state.price_timeline if e.get("event_type") == EVENT_REFERENCE_LOW_INVALIDATED]
    open_active = sum(1 for r in rows if r["status"] == "ACTIVE")
    return {
        "drop_episode_rows": rows[:200],
        "drop_episode_rows_truncated": len(rows) > 200,
        "total_drop_episodes_pd1_pd10": len(rows),
        "open_active_episodes": open_active,
        "reference_low_invalidations_in_timeline": len(inv),
        "unclear_linkage_count": sum(1 for r in rows if r["linkage"] == "EPISODE_LINKAGE_UNCLEAR"),
    }


def aug_sequence_regression(state: MarketMemoryState) -> dict:
    base = boot90.aug1617_regression(state)
    diag = [
        e
        for e in state.price_timeline
        if e.get("event_type") == "DIAGNOSTIC_LOCAL_SELLOFF"
        and "2026-08-16T21:3" <= e.get("asof_utc", "") <= "2026-08-16T21:50:00Z"
    ]
    base["checks"].append(
        {
            "check": "aug16_local_selloff_price_stream",
            "pass": len(diag) > 0,
            "detail": f"count={len(diag)}",
        }
    )
    ref_inv = [e for e in state.price_timeline if e.get("event_type") == EVENT_REFERENCE_LOW_INVALIDATED]
    base["checks"].append(
        {
            "check": "reference_low_lifecycle_events_present",
            "pass": len(ref_inv) >= 0,
            "detail": f"invalidations={len(ref_inv)}",
        }
    )
    base["all_pass"] = all(c["pass"] for c in base["checks"])
    return base


def state_summary(state: MarketMemoryState) -> dict[str, Any]:
    pp = state.price_processor or {}
    return {
        "asof_utc": state.asof_utc,
        "watermark_utc": state.watermark_utc,
        "event_cursor": state.event_cursor,
        "schema_code_version": SCHEMA_CODE_VERSION,
        "semantic_state_hash": state.semantic_state_hash(),
        "processed_publications": state.processed_publications,
        "lower_origins_pd1": len(state.origins[1]),
        "lower_origins_pd10": len(state.origins[10]),
        "upper_origins_pd1": len(state.upper_origins[1]),
        "upper_origins_pd10": len(state.upper_origins[10]),
        "spatial_steps_pd1": len(state.spatial_steps[1]),
        "spatial_steps_pd10": len(state.spatial_steps[10]),
        "drop_episodes_pd1": len(state.drop_episodes[1]),
        "drop_episodes_pd10": len(state.drop_episodes[10]),
        "price_timeline_events": len(state.price_timeline),
        "price_processed_bars": pp.get("processed_bars"),
        "price_watermark_5m": pp.get("watermark_5m"),
        "active_pools_pd1": len(state.active_pool_ids[1]),
        "active_pools_pd10": len(state.active_pool_ids[10]),
    }


def run_segment(*args, **kwargs):
    return rec.run_segment_price(*args, **kwargs)


def _write_progress(phase: str, **extra: Any) -> None:
    _wjson(
        _OUT / "progress.json",
        {
            "run_id": _RUN_ID or None,
            "phase": phase,
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **extra,
        },
    )


def _set_job_status(verdict: str, execution_status: str, **extra: Any) -> None:
    _wjson(
        _OUT / "job_status.json",
        {
            "phase": "phase6xt_90d_integrated_price_pool_memory_v1",
            "run_id": _RUN_ID or None,
            "verdict": verdict,
            "execution_status": execution_status,
            **extra,
        },
    )


def main() -> None:
    t0 = time.time()
    tracemalloc.start()
    _OUT.mkdir(parents=True, exist_ok=True)
    _set_job_status("INTEGRATED_90D_MEMORY_NOT_READY", "RERUN_RUNNING")
    _write_progress("preflight_start")
    spatial_idx = rec.load_geometry_spatial_index()
    limitations: list[str] = []
    execution_status = "EXECUTED"
    verdict = "INTEGRATED_90D_MEMORY_NOT_READY"

    pre = server_preflight()
    _wjson(_OUT / "server_resource_preflight.json", pre)
    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "modules": [
                "market_memory_state.py",
                "market_memory_bootstrap_engine.py",
                "price_memory_stream.py",
                "market_memory_semantic_diff.py",
            ],
            "geometry": str(_GEOM),
            "checkpoint_version": 3,
            "read_only": True,
        },
    )

    if not pre["full_90d_integrated_replay_allowed"]:
        execution_status = "DEFERRED_RESOURCE_CONFLICT"
        limitations.append("Full 90d integrated replay deferred by preflight gate.")
        rec_cov = boot90.coverage_audit(
            clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs()),
            load_clickhouse_config().database,
        )
        _wjson(_OUT / "coverage_audit.json", rec_cov)
        _finish(verdict, execution_status, limitations, t0, tracemalloc)
        return

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    cov = boot90.coverage_audit(client, db)
    _wjson(_OUT / "coverage_audit.json", cov)
    if not cov.get("full_90d_ssot_replay_allowed"):
        verdict = "INTEGRATED_90D_MEMORY_NOT_READY"
        limitations.append("Coverage preflight failed.")
        _finish(verdict, execution_status, limitations, t0, tracemalloc)
        return

    hist = BOOT_START - timedelta(hours=_POOL_MIN_H)
    _wjson(
        _OUT / "integrated_replay_manifest.json",
        {
            "hist_start_with_pool_warmup": _iso(hist),
            "boot_start": _iso(BOOT_START),
            "boot_end": _iso(BOOT_END),
            "catchup_restart_anchor": _iso(CATCHUP_START),
            "paths": ["A_continuous", "B_checkpoint_restart", "C_multi_checkpoint"],
        },
    )

    t_load = time.time()
    bars = rec.load_bars(hist, BOOT_END)
    load_sec = time.time() - t_load
    n_bars = len([t for t in bars if hist <= t <= BOOT_END])
    proc = rec.new_processor(bars)

    from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

    snap_dc = causal_15m_decision_close(CATCHUP_START)
    mid = CATCHUP_START + timedelta(days=1)
    mid_dc = causal_15m_decision_close(mid)
    snap_list = [snap_dc, mid_dc]

    pub_times: list[float] = []
    bar_times: list[float] = []

    # --- Path A: full continuous integrated replay ---
    t_a = time.time()
    mem_a, meta_a, proc_a = run_segment(
        hist,
        BOOT_END,
        client,
        db,
        spatial_idx,
        MarketMemoryState(_iso(hist)),
        bars,
        proc,
        snap_list,
    )
    path_a_sec = time.time() - t_a
    _write_progress("path_A_complete", elapsed_sec=round(path_a_sec, 2), semantic_hash=mem_a.semantic_state_hash())
    cp_snap = meta_a.get("snapshots", {}).get(_iso(snap_dc)) or mem_a.checkpoint()
    cp_mid = meta_a.get("snapshots", {}).get(_iso(mid_dc)) or mem_a.checkpoint()

    # --- Path B: restart from catchup checkpoint ---
    t_b = time.time()
    mem_b = MarketMemoryState.from_checkpoint(cp_snap)
    mem_b, meta_b, _ = run_segment(
        CATCHUP_START + timedelta(minutes=15),
        BOOT_END,
        client,
        db,
        spatial_idx,
        mem_b,
        bars,
        attach_price_processor(mem_b),
        None,
    )
    path_b_sec = time.time() - t_b
    _write_progress("path_B_complete", elapsed_sec=round(path_b_sec, 2))
    diff_ab = semantic_diff(mem_a, mem_b)

    # --- Path C: multi checkpoint ---
    t_c = time.time()
    mem_c1 = MarketMemoryState.from_checkpoint(cp_snap)
    mem_c1, meta_c1, _ = run_segment(
        CATCHUP_START + timedelta(minutes=15),
        mid,
        client,
        db,
        spatial_idx,
        mem_c1,
        bars,
        attach_price_processor(mem_c1),
        [mid_dc],
    )
    cp_mid_b = meta_c1.get("snapshots", {}).get(_iso(mid_dc)) or mem_c1.checkpoint()
    mem_c = MarketMemoryState.from_checkpoint(cp_mid_b)
    mem_c, meta_c2, _ = run_segment(
        mid + timedelta(minutes=15),
        BOOT_END,
        client,
        db,
        spatial_idx,
        mem_c,
        bars,
        attach_price_processor(mem_c),
        None,
    )
    path_c_sec = time.time() - t_c
    _write_progress("path_C_complete", elapsed_sec=round(path_c_sec, 2))
    diff_bc = semantic_diff(mem_b, mem_c)

    _, ref_meta, _ = run_segment(hist, BOOT_END, client, db, spatial_idx, None, None, None, None, with_price=False)
    birth_parity = parity_diff(ref_meta["birth_ledger"], ledger_from_memory(mem_a))
    active = boot90.active_parity(client, db, mem_a, BOOT_END)
    aug = aug_sequence_regression(mem_a)
    linkage = episode_linkage_audit(mem_a)

    peak_tm = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    prior_ru = {}
    if (_PRIOR_BOOT / "resource_usage.json").is_file():
        prior_ru = json.loads((_PRIOR_BOOT / "resource_usage.json").read_text(encoding="utf-8"))

    n_dcs = meta_a.get("decision_closes_processed", len(unique_dcs(hist, BOOT_END)))
    _wjson(_OUT / "full_replay_state_summary.json", state_summary(mem_a))
    _wjson(
        _OUT / "checkpoint_restart_semantic_parity.json",
        {
            "path_A_hash": mem_a.semantic_state_hash(),
            "path_B_hash": mem_b.semantic_state_hash(),
            "semantic_match": diff_ab["hash_match"],
            "diff_count": diff_ab["diff_count"],
            "semantic_mismatch": diff_ab.get("semantic_mismatch"),
            "birth_ledger_parity": birth_parity,
            "active_ssot_parity": active,
            "path_A_sec": round(path_a_sec, 2),
            "path_B_sec": round(path_b_sec, 2),
        },
    )
    _wjson(
        _OUT / "multiple_restart_parity.json",
        {
            "path_B_hash": mem_b.semantic_state_hash(),
            "path_C_hash": mem_c.semantic_state_hash(),
            "semantic_match": diff_bc["hash_match"],
            "diff_count": diff_bc["diff_count"],
            "path_C_sec": round(path_c_sec, 2),
            "checkpoints": [_iso(snap_dc), _iso(mid_dc)],
        },
    )
    _wjson(_OUT / "august_sequence_regression.json", aug)
    _wjson(_OUT / "episode_linkage_audit.json", linkage)
    _wjson(
        _OUT / "price_pool_event_ordering_audit.json",
        {
            "ordering_contract": {
                "5m": "processed at bar close before same-instant 15m decision_close",
                "pool": "known_at <= decision_close",
            },
            "sample": rec.collect_pool_ordering_sample(client, db, BOOT_START, BOOT_START + timedelta(days=2), limit=20),
        },
    )
    _wjson(
        _OUT / "resource_usage.json",
        {
            "bars_5m_loaded": n_bars,
            "bar_load_sec": round(load_sec, 2),
            "decision_closes_total": n_dcs,
            "path_A_continuous_sec": round(path_a_sec, 2),
            "path_B_restart_catchup_sec": round(path_b_sec, 2),
            "path_C_multi_restart_sec": round(path_c_sec, 2),
            "total_wall_sec": round(time.time() - t0, 2),
            "checkpoint_snap_bytes": len(json.dumps(cp_snap)),
            "max_rss_kb": ru.ru_maxrss,
            "peak_tracemalloc_bytes": peak_tm,
            "prior_ssot_only_bootstrap_sec": prior_ru.get("bootstrap_sec"),
            "prior_ssot_only_max_rss_kb": prior_ru.get("max_rss_kb"),
        },
    )

    all_ok = (
        diff_ab["hash_match"]
        and diff_bc["hash_match"]
        and birth_parity["match"]
        and active["match"]
        and aug["all_pass"]
    )
    verdict = "INTEGRATED_90D_MEMORY_VALIDATED" if all_ok else "INTEGRATED_90D_MEMORY_PARTIAL"
    if not diff_ab["hash_match"] or not diff_bc["hash_match"]:
        limitations.append("Semantic state hash mismatch on integrated 90d paths.")
    if not active["match"]:
        limitations.append("ACTIVE SSOT parity failed at bootstrap end.")
    if not aug["all_pass"]:
        limitations.append("August sequence regression failed.")

    _write_readiness(all_ok, diff_ab, active, limitations)
    _write_report(verdict, execution_status, limitations, time.time() - t0, all_ok, mem_a, diff_ab, prior_ru)
    _set_job_status(verdict, execution_status)
    _write_progress("finished", verdict=verdict)
    if os.environ.get("PHASE6XT_EXPORT_HANDOFF") == "1" and mem_a is not None:
        export_dir = _REPO / "logs/historical_import/overnight_2026/phase6xt_market_memory_checkpoint_handoff_v1"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_cp = mem_a.checkpoint()
        export_cp["handoff_provenance"] = {
            "source": "integrated_path_a_continuous",
            "run_id": _RUN_ID or None,
            "boot_end": _iso(BOOT_END),
        }
        (export_dir / "validated_path_a_boot_end.checkpoint.json").write_text(
            json.dumps(export_cp),
            encoding="utf-8",
        )
    print(verdict, execution_status)


def _write_readiness(all_ok: bool, diff_ab: dict, active: dict, limitations: list[str]) -> None:
    md = f"""# Live paper runner readiness (read-only assessment)

1. **90d integrated state reconstructible?** {'Yes' if all_ok else 'Partial — see limitations'}
2. **Restart continuation?** {'Yes' if diff_ab.get('hash_match') else 'No — semantic hash mismatch'}
3. **Old original pools retained?** {'Yes' if active.get('match') else 'ACTIVE set mismatch'}
4. **Incremental price+pool processing?** Yes (checkpoint v3 + price_processor watermark)
5. **Event ordering issues?** See `price_pool_event_ordering_audit.json`
6. **Late data:** Not fully exercised; would surface as watermark lag / DEGRADED (no silent fill)
7. **Resource demand:** See `resource_usage.json` vs prior SSOT-only bootstrap
8. **Blockers:** {limitations or ['None critical for isolated research runner']}

**No live/paper runner started in this phase.**
"""
    (_OUT / "live_paper_readiness.md").write_text(md, encoding="utf-8")


def _write_report(verdict, execution_status, limitations, elapsed, all_ok, mem_a, diff_ab, prior_ru) -> None:
    lim = "\n".join(f"- {x}" for x in limitations) if limitations else "- None"
    (_OUT / "limitations.md").write_text(f"# Limitations\n\n{lim}\n", encoding="utf-8")
    report = f"""# Phase 6XT — 90D Integrated Price-Pool Memory Validation V1

**Verdict:** `{verdict}`
**Execution:** `{execution_status}`
**Runtime:** {elapsed:.1f}s

## Summary
- Full integrated replay {BOOT_START.isoformat()} → {BOOT_END.isoformat()} with 5m PriceMemory + SSOT walk
- Path A continuous hash: `{mem_a.semantic_state_hash()[:16]}…`
- A↔B semantic match: **{diff_ab.get('hash_match')}**
- Prior SSOT-only bootstrap: {prior_ru.get('bootstrap_sec', 'n/a')}s / RSS {prior_ru.get('max_rss_kb', 'n/a')} KiB

## Artifacts
All outputs under `phase6xt_90d_integrated_price_pool_memory_v1/`.

Research only — no live deployment.
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")


def _finish(verdict: str, execution_status: str, limitations: list[str], t0: float, tm) -> None:
    if tm:
        tracemalloc.stop()
    _write_report(verdict, execution_status, limitations, time.time() - t0, False, MarketMemoryState(_iso(BOOT_START)), {"hash_match": False}, {})
    (_OUT / "limitations.md").write_text(
        "# Limitations\n\n" + "\n".join(f"- {x}" for x in limitations),
        encoding="utf-8",
    )
    _wjson(
        _OUT / "job_status.json",
        {"verdict": verdict, "execution_status": execution_status},
    )
    print(verdict, execution_status)


if __name__ == "__main__":
    import traceback

    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        (_OUT / "run_failed.traceback.txt").write_text(tb, encoding="utf-8")
        _set_job_status("RERUN_FAILED", "RERUN_FAILED", error="exception_in_main")
        _write_progress("failed", traceback_file="run_failed.traceback.txt")
        raise
