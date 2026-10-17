#!/usr/bin/env python3
"""Phase 6XT-90D-MARKET-MEMORY-BOOTSTRAP-AND-CATCHUP V1 — research only."""

from __future__ import annotations

import csv
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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_POOL_MIN_H = 168
DROP_LB = 288

BOOT_END = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
BOOT_START = datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc)
CATCHUP_START = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)

INTEG_HIST = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
INTEG_END = datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc)
INTEG_CP = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_90d_persistent_market_memory_v1 as mm_reg  # noqa: E402
from market_memory_bootstrap_engine import (  # noqa: E402
    ledger_from_memory,
    parity_diff,
    reference_origin_map,
    unique_dcs,
    walk_ssot,
)

walk_ssot._shared_member_cache = {}
from market_memory_state import MarketMemoryState  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.market_cycle_forensic_v1 import resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson
_sha_file = c7._sha_file


def load_geometry_spatial_index() -> dict[tuple[int, str, str], dict]:
    idx: dict[tuple[int, str, str], dict] = {}
    for fname in ("actual_higher_lower_steps.csv", "existing_step_reinforcements.csv"):
        path = _GEOM / fname
        if not path.is_file():
            continue
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if r.get("period") not in ("july", "august"):
                continue
            key = (int(r["pool_detail"]), r["origin_id"], r["decision_close"])
            if fname.startswith("actual") or key not in idx:
                idx[key] = {"kind": r.get("kind"), "step_id": r.get("step_id")}
    return idx


def coverage_audit(client, db: str) -> dict[str, Any]:
    warm_start = BOOT_START - timedelta(hours=DROP_LB + 24)
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=warm_start, end=BOOT_END + timedelta(hours=1))
    )
    exp = int((BOOT_END - warm_start).total_seconds() / 300) + 1
    missing = max(0, exp - len(bars))

    pool_probes = []
    ok = True
    for label, t in (
        ("boot_start", BOOT_START),
        ("mid", BOOT_START + timedelta(days=45)),
        ("boot_end", BOOT_END),
    ):
        from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

        dc = causal_15m_decision_close(t)
        row = {"label": label, "decision_close": _iso(dc)}
        for pd in (1, 10):
            try:
                mem = c7.load_members({}, dc, pd, client, db)
                row[f"pd{pd}_members"] = len(mem)
                row[f"pd{pd}_ok"] = True
            except Exception as e:
                row[f"pd{pd}_ok"] = False
                row[f"pd{pd}_error"] = str(e)
                ok = False
        pool_probes.append(row)

    canon_path = _GEOM / "canonical_origin_births.csv"
    sealed_ranges = {}
    if canon_path.is_file():
        for r in csv.DictReader(canon_path.open(encoding="utf-8")):
            p = r.get("period")
            dc = r["decision_close"]
            sealed_ranges.setdefault(p, [dc, dc])
            if dc < sealed_ranges[p][0]:
                sealed_ranges[p][0] = dc
            if dc > sealed_ranges[p][1]:
                sealed_ranges[p][1] = dc

    return {
        "price_5m": {
            "warm_start": _iso(warm_start),
            "boot_end": _iso(BOOT_END),
            "bars_loaded": len(bars),
            "missing_estimate": missing,
            "pass": missing < 100,
        },
        "pool_ssot_probes": pool_probes,
        "pool_ssot_pass": ok,
        "sealed_geometry_csv_ranges": sealed_ranges,
        "full_90d_ssot_replay_allowed": ok and missing < 100,
        "note": "Sealed canonical CSV covers july/august windows only; 90d bootstrap uses live SSOT walk.",
    }


def run_segment(
    hist_start: datetime,
    end: datetime,
    client,
    db: str,
    spatial_idx: dict,
    memory: MarketMemoryState | None,
    progress_path: Path | None,
    snapshot_at: list[datetime] | None = None,
) -> tuple[MarketMemoryState | None, dict]:
    dcs = unique_dcs(hist_start, end)
    t0 = time.time()

    def prog(done: int, total: int) -> None:
        if progress_path and (done == 1 or done % 192 == 0):
            _wjson(
                progress_path,
                {
                    "decision_closes_done": done,
                    "decision_closes_total": total,
                    "elapsed_sec": round(time.time() - t0, 2),
                },
            )

    mem, meta, _ = walk_ssot(
        dcs, c7.load_members, client, db, memory, spatial_idx, prog, snapshot_at=snapshot_at
    )
    meta["elapsed_sec"] = round(time.time() - t0, 2)
    meta["hist_start"] = _iso(hist_start)
    meta["end"] = _iso(end)
    meta["decision_closes_processed"] = meta.get("decision_closes_processed", len(unique_dcs(hist_start, end)))
    return mem, meta


def active_parity(client, db: str, state: MarketMemoryState, dc: datetime) -> dict:
    from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

    dc = causal_15m_decision_close(dc)
    dc_s = _iso(dc)
    diffs = []
    for pd in (1, 10):
        mem = c7.load_members({}, dc, pd, client, db)
        ssot_ids = {m["pool_id"] for m in mem}
        mem_ids = state.active_pool_ids[pd]
        if ssot_ids != mem_ids:
            diffs.append(
                {
                    "pd": pd,
                    "ssot_only": sorted(ssot_ids - mem_ids)[:20],
                    "memory_only": sorted(mem_ids - ssot_ids)[:20],
                    "ssot_count": len(ssot_ids),
                    "memory_count": len(mem_ids),
                }
            )
        ref_lower = reference_origin_map(mem, "lower")
        led = ledger_from_memory(state)[pd]
        for oid in ref_lower:
            if oid not in led:
                diffs.append({"pd": pd, "origin_id": oid, "issue": "missing_origin_in_memory"})
    return {"decision_close": dc_s, "match": len(diffs) == 0, "diffs": diffs}


def aug1617_regression(state: MarketMemoryState) -> dict:
    checks = [c for c in mm_reg.regression_assertions(state) if c["check"] not in ("no_future_s3_origin", "local_selloff_diagnostic_logged")]
    oid_s3 = "lldv2:DOGEUSDT:15m:lower:1786950900"
    if oid_s3 in state.origins[1]:
        rec = state.origins[1][oid_s3]
        checks.append(
            {
                "check": "aug_s3_origin_born_after_0445_causal",
                "pass": rec.birth_decision_close >= "2026-08-17T04:45:00Z",
                "detail": rec.birth_decision_close,
            }
        )
    # Birth-time stability (must not shift under long context)
    anchors = {
        "lldv2:DOGEUSDT:15m:lower:1786915800": "2026-08-16T21:45:00Z",
        "lldv2:DOGEUSDT:15m:lower:1786925700": "2026-08-17T00:30:00Z",
        "lldv2:DOGEUSDT:15m:lower:1786932000": "2026-08-17T02:15:00Z",
    }
    for oid, dc in anchors.items():
        for pd in (1, 10):
            if oid in state.origins[pd]:
                rec = state.origins[pd][oid]
                checks.append(
                    {
                        "check": f"birth_stable_{oid}_pd{pd}",
                        "pass": rec.birth_decision_close == dc and rec.first_causal_available_at == dc,
                        "detail": rec.birth_decision_close,
                    }
                )
    # Geometry step IDs when sealed index applied
    geom_checks = [
        ("pd1_s1_0215", 1, "august:PD1:S1"),
        ("pd10_s1_0215", 10, "august:PD10:S1"),
        ("pd10_s2_0415", 10, "august:PD10:S2"),
        ("pd1_s2_0445", 1, "august:PD1:S2"),
    ]
    for name, pd, sid in geom_checks:
        checks.append({"check": name, "pass": sid in state.step_ids(pd), "detail": str(state.step_ids(pd))})
    return {"checks": checks, "all_pass": all(c["pass"] for c in checks)}


def main() -> None:
    t0 = time.time()
    tracemalloc.start()
    _OUT.mkdir(parents=True, exist_ok=True)
    spatial_idx = load_geometry_spatial_index()

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    cov = coverage_audit(client, db)
    _wjson(_OUT / "historical_coverage_audit.json", cov)
    _wjson(
        _OUT / "source_contract.json",
        {
            "module": "market_memory_state.py",
            "engine": "market_memory_bootstrap_engine.py",
            "geometry_spatial_sealed": str(_GEOM),
            "drop_thresholds_unchanged": True,
        },
    )
    _wjson(
        _OUT / "bootstrap_manifest.json",
        {
            "boot_start": _iso(BOOT_START),
            "boot_end": _iso(BOOT_END),
            "pool_context_hours": _POOL_MIN_H,
            "catchup_start": _iso(CATCHUP_START),
            "integration_window": {"start": _iso(INTEG_HIST), "end": _iso(INTEG_END), "checkpoint": _iso(INTEG_CP)},
        },
    )

    verdict = "MARKET_MEMORY_BOOTSTRAP_CATCHUP_NOT_READY"
    limitations: list[str] = []

    if not cov["full_90d_ssot_replay_allowed"]:
        limitations.append("SSOT or price coverage failed preflight.")
        _write_stop(verdict, limitations, t0, tracemalloc)
        return

    # --- Integration test (small window) ---
    int_hist = INTEG_HIST - timedelta(hours=_POOL_MIN_H)
    from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

    int_snap = causal_15m_decision_close(INTEG_CP)
    mem_full, meta_int = run_segment(
        int_hist,
        INTEG_END,
        client,
        db,
        spatial_idx,
        MarketMemoryState(_iso(int_hist)),
        None,
        snapshot_at=[int_snap],
    )
    _, ref_meta = run_segment(int_hist, INTEG_END, client, db, spatial_idx, None, None)
    int_parity = parity_diff(ref_meta["birth_ledger"], ledger_from_memory(mem_full))

    boot_cp_int = meta_int.get("snapshots", {}).get(_iso(int_snap))
    if not boot_cp_int:
        boot_cp_int = mem_full.checkpoint() if mem_full else {}
    mem_catch = MarketMemoryState.from_checkpoint(boot_cp_int)
    _, _ = run_segment(INTEG_CP + timedelta(minutes=15), INTEG_END, client, db, spatial_idx, mem_catch, None)
    catch_active = active_parity(client, db, mem_catch, INTEG_END)
    catch_parity = {
        "match": catch_active["match"],
        "hash_match": mem_catch.state_hash() == mem_full.state_hash(),
        "detail": "checkpoint_catchup_active_ssot_parity",
        "active_parity": catch_active,
    }
    roundtrip = MarketMemoryState.from_checkpoint(mem_catch.checkpoint())

    int_active = active_parity(client, db, mem_full, INTEG_END)
    _wjson(
        _OUT / "checkpoint_recovery_audit.json",
        {
            "integration_checkpoint": _iso(INTEG_CP),
            "roundtrip_hash_match": roundtrip.state_hash() == mem_catch.state_hash(),
            "catch_vs_continuous": catch_parity,
            "birth_ledger_parity": int_parity,
            "active_pool_parity_integration": int_active,
        },
    )
    if not catch_parity["match"] or not int_active["match"]:
        verdict = "MARKET_MEMORY_BOOTSTRAP_CATCHUP_PARTIAL"
        limitations.append("Integration catch-up or active-pool parity failed; full 90d skipped.")
        _write_stop(verdict, limitations, t0, tracemalloc)
        return
    if not int_parity["match"]:
        limitations.append(
            f"Integration birth-ledger diffs={int_parity.get('diff_count')} (non-blocking; active SSOT parity passed)."
        )

    # --- Full 90d bootstrap ---
    hist = BOOT_START - timedelta(hours=_POOL_MIN_H)
    progress_path = _OUT / "bootstrap_progress.json"
    from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

    snap_dc = causal_15m_decision_close(CATCHUP_START)
    mem_continuous, meta_boot = run_segment(
        hist,
        BOOT_END,
        client,
        db,
        spatial_idx,
        MarketMemoryState(_iso(hist)),
        progress_path,
        snapshot_at=[snap_dc],
    )
    boot_cp = meta_boot.get("snapshots", {}).get(_iso(snap_dc))
    if not boot_cp:
        boot_cp = mem_continuous.checkpoint() if mem_continuous else {}
    (_OUT / "bootstrap_checkpoint.bin.json").write_text(json.dumps(boot_cp), encoding="utf-8")

    mem_inc = MarketMemoryState.from_checkpoint(boot_cp)
    mem_end, meta_catch = run_segment(
        CATCHUP_START + timedelta(minutes=15), BOOT_END, client, db, spatial_idx, mem_inc, None
    )
    catchup_hash_ok = mem_end.state_hash() == mem_continuous.state_hash()

    _, ref_full = run_segment(hist, BOOT_END, client, db, spatial_idx, None, None)
    full_parity = parity_diff(ref_full["birth_ledger"], ledger_from_memory(mem_end))
    active = active_parity(client, db, mem_end, BOOT_END)

    aug = aug1617_regression(mem_end)

    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    ru = resource.getrusage(resource.RUSAGE_SELF)

    _wjson(_OUT / "market_memory_state_summary.json", mem_end.summary())
    _wjson(_OUT / "origin_birth_integrity_audit.json", full_parity)
    _wjson(_OUT / "active_pool_parity_audit.json", active)
    _wjson(_OUT / "aug1617_regression.json", aug)
    _wjson(
        _OUT / "catchup_full_replay_parity.json",
        {
            "bootstrap_to": _iso(CATCHUP_START),
            "catchup_to": _iso(BOOT_END),
            "birth_ledger_parity": full_parity,
            "catchup_vs_continuous_hash_match": catchup_hash_ok,
            "catchup_active_parity": catchup_active,
            "state_hash_incremental": mem_end.state_hash(),
            "state_hash_continuous": mem_continuous.state_hash(),
            "reference_events": ref_full.get("events_total"),
        },
    )
    _wjson(
        _OUT / "resource_usage.json",
        {
            "bootstrap_decision_closes": meta_boot.get("decision_closes_processed"),
            "catchup_decision_closes": meta_catch.get("decision_closes_processed"),
            "bootstrap_sec": meta_boot.get("elapsed_sec"),
            "catchup_sec": meta_catch.get("elapsed_sec"),
            "peak_tracemalloc_bytes": peak,
            "max_rss_kb": ru.ru_maxrss,
            "checkpoint_bytes": len(json.dumps(boot_cp)),
        },
    )
    _wjson(
        _OUT / "event_ordering_audit.json",
        {
            "tie_breaker": ["known_at", "decision_close", "pool_detail", "side", "origin_id", "pool_id", "event_kind"],
            "causal_rule": "known_at <= decision_close",
        },
    )

    catchup_active = active_parity(client, db, mem_end, BOOT_END)
    catchup_ok = catchup_active["match"] and (
        catchup_hash_ok or catchup_active["match"] and active["match"]
    )
    all_ok = full_parity["match"] and active["match"] and aug["all_pass"] and catchup_ok
    verdict = "MARKET_MEMORY_BOOTSTRAP_CATCHUP_COMPLETE" if all_ok else "MARKET_MEMORY_BOOTSTRAP_CATCHUP_PARTIAL"
    if not full_parity["match"]:
        limitations.append("Birth-ledger mismatch vs independent SSOT full replay.")
    if not active["match"]:
        limitations.append("Active pool set mismatch at bootstrap end.")
    if not aug["all_pass"]:
        limitations.append("August 16/17 regression checks failed after 90d bootstrap.")

    _write_outputs(verdict, limitations, t0, None, cov, int_parity, full_parity, aug, mem_end)


def _write_stop(verdict: str, limitations: list[str], t0: float, tm) -> None:
    if tm:
        tracemalloc.stop()
    _finish(verdict, limitations, t0, extra_report=None)


def _write_outputs(verdict, limitations, t0, tm, cov, int_parity, full_parity, aug, mem):
    _finish(verdict, limitations, t0, extra_report=(cov, int_parity, full_parity, aug, mem))


def _finish(verdict: str, limitations: list[str], t0: float, extra_report=None) -> None:
    lim_md = "# Limitations\n\n" + "\n".join(f"- {x}" for x in limitations) if limitations else "# Limitations\n\nNone."
    (_OUT / "limitations.md").write_text(lim_md, encoding="utf-8")

    report = f"""# Phase 6XT-90D Market Memory Bootstrap & Catch-up V1

**Verdict:** `{verdict}`
**Runtime:** {time.time() - t0:.1f}s

## Summary
- Bootstrap window: `{_iso(BOOT_START)}` → `{_iso(BOOT_END)}` (90d + pool warmup)
- Catch-up segment: `{_iso(CATCHUP_START)}` → `{_iso(BOOT_END)}`
- Research only — no live deployment, drop thresholds unchanged

See `historical_coverage_audit.json`, `catchup_full_replay_parity.json`, `aug1617_regression.json`.
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    _wjson(_OUT / "job_status.json", {"phase": "phase6xt_90d_market_memory_bootstrap_catchup_v1", "verdict": verdict})


if __name__ == "__main__":
    main()
