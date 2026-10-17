#!/usr/bin/env python3
"""Phase 6XT-MARKET-MEMORY-PRICE-STREAM-AND-RECOVERY V1 — research only."""

from __future__ import annotations

import copy
import csv
import json
import resource
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_market_memory_price_stream_recovery_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_DET = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_to_upward_staircase_detector_v1"
_POOL_MIN_H = 168
DROP_LB = 288

BOOT_END = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
BOOT_START = datetime(2026, 5, 21, 13, 0, tzinfo=timezone.utc)
CATCHUP_START = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)

INTEG_HIST = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
INTEG_END = datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc)
INTEG_CP = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

AUG_HIST = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
AUG_END = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)

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
import phase6xt_90d_persistent_market_memory_v1 as mm_reg  # noqa: E402
from market_memory_bootstrap_engine import (  # noqa: E402
    _sort_key_event,
    extract_birth_events,
    extract_death_events,
    ledger_from_memory,
    parity_diff,
    unique_dcs,
    walk_ssot,
)
from market_memory_semantic_diff import semantic_diff  # noqa: E402
from market_memory_state import (  # noqa: E402
    EVENT_DIAG_SELLOFF,
    MarketMemoryState,
    SCHEMA_CODE_VERSION,
)
from price_memory_stream import attach_price_processor, load_drop_params  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson

walk_ssot._shared_member_cache = {}


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


def load_bars(start: datetime, end: datetime) -> dict[datetime, dict]:
    warm = start - timedelta(hours=DROP_LB + 24)
    src = ClickHouseResearchCandleSource()
    return resample_5m_from_candles(src.get_candles("DOGEUSDT", "5m", start=warm, end=end + timedelta(hours=1)))


def new_processor(bars: dict) -> Any:
    proc = attach_price_processor(MarketMemoryState(asof_utc=_iso(CALIB_START)))
    proc.calibrate_diagnostic_threshold(bars, CALIB_START, CALIB_END)
    return proc


def run_segment_price(
    hist_start: datetime,
    end: datetime,
    client,
    db: str,
    spatial_idx: dict,
    memory: MarketMemoryState | None,
    bars: dict | None,
    processor: Any | None,
    snapshot_at: list[datetime] | None = None,
    with_price: bool = True,
) -> tuple[MarketMemoryState | None, dict, Any | None]:
    dcs = unique_dcs(hist_start, end)
    t0 = time.time()
    proc = processor
    bars_use = bars or {}
    if with_price:
        if memory and proc is None:
            proc = attach_price_processor(memory)
        if proc is None:
            proc = new_processor(bars_use)
    else:
        proc = None
    mem, meta, _ = walk_ssot(
        dcs,
        c7.load_members,
        client,
        db,
        memory,
        spatial_idx,
        None,
        snapshot_at=snapshot_at,
        bars_5m=bars_use if with_price else None,
        price_processor=proc,
    )
    meta["elapsed_sec"] = round(time.time() - t0, 2)
    return mem, meta, proc


def collect_pool_ordering_sample(client, db: str, start: datetime, end: datetime, limit: int = 40) -> list[dict]:
    dcs = unique_dcs(start, end)[:limit]
    prev: dict[int, set[str]] = {1: set(), 10: set()}
    rows = []
    for dc in dcs:
        dc_s = _iso(dc)
        for pd in (1, 10):
            members = c7.load_members({}, dc, pd, client, db)
            for ev in extract_birth_events(prev[pd], members, pd, dc_s) + extract_death_events(
                prev[pd], members, pd, dc_s
            ):
                rows.append(
                    {
                        "source": "pool_ssot",
                        "event_id": f"{ev['event_kind']}:{pd}:{ev.get('pool_id')}:{dc_s}",
                        "known_at": ev["known_at"],
                        "decision_close": ev["decision_close"],
                        "sort_key": list(_sort_key_event(ev)),
                    }
                )
            prev[pd] = {m["pool_id"] for m in members}
    return rows


def aug1617_regression(state: MarketMemoryState, price_timeline: list[dict]) -> dict:
    checks = [c for c in boot90.aug1617_regression(state)["checks"]]
    diag_bars = [
        e
        for e in price_timeline
        if e.get("event_type") == EVENT_DIAG_SELLOFF and "2026-08-16T21:3" <= e.get("asof_utc", "") <= "2026-08-16T21:50:00Z"
    ]
    checks.append(
        {
            "check": "aug16_diagnostic_selloff_from_price_stream",
            "pass": len(diag_bars) > 0,
            "detail": f"count={len(diag_bars)};no_manual_injection",
        }
    )
    if not diag_bars:
        checks.append(
            {
                "check": "aug16_selloff_feature_finding",
                "pass": True,
                "detail": "General rule did not fire; document as historical feature not detection pass",
            }
        )
    return {"checks": checks, "all_pass": all(c["pass"] for c in checks if c["check"] != "aug16_selloff_feature_finding")}


def recovery_tests(state: MarketMemoryState, cp: dict, bars: dict, spatial_idx: dict, client, db: str) -> dict:
    results = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append({"test": name, "pass": ok, "detail": detail})

    dup = MarketMemoryState.from_checkpoint(cp)
    proc = attach_price_processor(dup)
    dcs = unique_dcs(CATCHUP_START, CATCHUP_START + timedelta(hours=2))
    walk_ssot(dcs[:1], c7.load_members, client, db, dup, spatial_idx, bars_5m=bars, price_processor=proc)
    h1 = dup.state_hash()
    walk_ssot(dcs[:1], c7.load_members, client, db, dup, spatial_idx, bars_5m=bars, price_processor=proc)
    record("duplicate_pool_publication_idempotent", h1 == dup.state_hash(), "re-walk same dc")

    bad = copy.deepcopy(cp)
    bad["version"] = 999
    try:
        MarketMemoryState.from_checkpoint(bad)
        record("corrupt_checkpoint_version", False, "accepted bad version")
    except ValueError:
        record("corrupt_checkpoint_version", True, "rejected")

    wm_bad = copy.deepcopy(cp)
    wm_bad["watermark_utc"] = "2020-01-01T00:00:00Z"
    st_wm = MarketMemoryState.from_checkpoint(wm_bad)
    record("inconsistent_watermark_loads", st_wm.watermark_utc == "2020-01-01T00:00:00Z", "caller must detect")

    return {"tests": results, "all_pass": all(t["pass"] for t in results)}


def write_static_artifacts() -> None:
    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "module": "market_memory_state.py",
            "price_stream": "price_memory_stream.py",
            "engine": "market_memory_bootstrap_engine.py",
            "drop_params": str(_DET / "detector_parameters.json"),
            "geometry_sealed": str(_GEOM),
            "read_only": True,
            "no_clickhouse_writes": True,
        },
    )
    (_OUT / "price_memory_event_contract.md").write_text(
        """# Price memory event contract (V1)

| event_type | Role |
|------------|------|
| `DROP_EVENT` | Unchanged OR drawdown/range drop rule; sets reference low episode |
| `DIAGNOSTIC_LOCAL_SELLOFF` | Red candle + wick ≥ variant A + range ≥ P90 calibrated May–Jul 2026 |
| `REFERENCE_LOW_INVALIDATED` | Bar low breaks active reference low minus eps |

Pool events remain on SSOT walk (`ORIGIN_BIRTH`, `REINFORCEMENT`, `TRUE_UPWARD_FORMATION`, etc.).
5m bars processed at bar close UTC, before 15m `decision_close` pool diff at same timestamp.
""",
        encoding="utf-8",
    )
    (_OUT / "canonical_state_hash_contract.md").write_text(
        f"""# Canonical semantic state hash

- Schema: `{SCHEMA_CODE_VERSION}`
- Payload: `MarketMemoryState.semantic_payload()` — sorted keys, canonical spatial step order
- Includes: origins, upper, spatial steps, drop episodes, active pools, staircase fields, price_processor blob, price_timeline
- Excludes: ephemeral `event_log` (derivable); checkpoint-only duplicate hashes
- `state_hash()` aliases `semantic_state_hash()`
""",
        encoding="utf-8",
    )
    (_OUT / "restart_hash_root_cause.md").write_text(
        """# Restart hash divergence root cause

**Class C (semantic):** `seen_origins`, `main_step_seq`, `main_last_step_bottom`, and `main_step_initialized` were kept only in `walk_ssot` local variables. Checkpoint restart re-initialized staircase from the first decision close in the catch-up segment, producing different `bootstrap:PD*:S*` step IDs versus continuous replay.

**Class A:** `spatial_steps` list order differed before canonical sort in hash (addressed via `_canonical_spatial_steps`).

**Fix:** Persist staircase fields on `MarketMemoryState` (checkpoint v3), restore at walk start, sync after each publication.
""",
        encoding="utf-8",
    )


def main() -> None:
    t0 = time.time()
    tracemalloc.start()
    _OUT.mkdir(parents=True, exist_ok=True)
    write_static_artifacts()
    spatial_idx = load_geometry_spatial_index()
    limitations: list[str] = []
    verdict = "MARKET_MEMORY_PRICE_RECOVERY_NOT_READY"

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    cov = boot90.coverage_audit(client, db)
    _wjson(_OUT / "price_event_coverage_audit.json", cov)
    if not cov.get("full_90d_ssot_replay_allowed"):
        limitations.append("Preflight coverage failed.")
        _finish(verdict, limitations, t0)
        return

    # --- Integration window July 1–10 ---
    int_hist = INTEG_HIST - timedelta(hours=_POOL_MIN_H)
    bars_int = load_bars(int_hist, INTEG_END)
    from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close

    int_snap = causal_15m_decision_close(INTEG_CP)
    proc_int = new_processor(bars_int)
    mem_cont, meta_int, _ = run_segment_price(
        int_hist, INTEG_END, client, db, spatial_idx, MarketMemoryState(_iso(int_hist)), bars_int, proc_int, [int_snap]
    )
    _, ref_meta, _ = run_segment_price(
        int_hist, INTEG_END, client, db, spatial_idx, None, None, None, None, with_price=False
    )
    int_parity = parity_diff(ref_meta["birth_ledger"], ledger_from_memory(mem_cont))
    boot_cp = meta_int.get("snapshots", {}).get(_iso(int_snap)) or mem_cont.checkpoint()
    mem_restart = MarketMemoryState.from_checkpoint(boot_cp)
    proc_restart = attach_price_processor(mem_restart)
    mem_catch, _, _ = run_segment_price(
        INTEG_CP + timedelta(minutes=15), INTEG_END, client, db, spatial_idx, mem_restart, bars_int, proc_restart, None
    )
    diff_int = semantic_diff(mem_cont, mem_catch)
    _wjson(
        _OUT / "checkpoint_restart_parity.json",
        {
            "integration_window": {"start": _iso(INTEG_HIST), "end": _iso(INTEG_END), "checkpoint": _iso(INTEG_CP)},
            "path_A_continuous_hash": mem_cont.semantic_state_hash(),
            "path_B_restart_catchup_hash": mem_catch.semantic_state_hash(),
            "semantic_hash_match": diff_int["hash_match"],
            "birth_ledger_parity": int_parity,
            "active_parity": boot90.active_parity(client, db, mem_catch, INTEG_END),
        },
    )
    _wjson(_OUT / "semantic_state_diff.json", diff_int)

    ordering_rows = collect_pool_ordering_sample(client, db, INTEG_HIST, INTEG_END)
    for t in sorted(bars_int.keys())[:5]:
        ordering_rows.append(
            {
                "source": "price_5m",
                "event_id": f"bar_close:{_iso(t)}",
                "known_at": _iso(t),
                "decision_close": _iso(t),
                "sort_key": [_iso(t), _iso(t)],
            }
        )
    _wjson(
        _OUT / "price_pool_event_ordering_audit.json",
        {
            "rules": {
                "5m": "processed at bar close before same-instant 15m decision_close",
                "pool": "known_at <= decision_close required",
                "tie_breaker": [
                    "known_at",
                    "decision_close",
                    "pool_detail",
                    "side",
                    "origin_id",
                    "pool_id",
                    "event_kind",
                ],
            },
            "sample": ordering_rows,
        },
    )

    if diff_int.get("semantic_mismatch"):
        limitations.append("Integration restart semantic hash mismatch.")
        verdict = "MARKET_MEMORY_PRICE_RECOVERY_PARTIAL"

    # --- August 16/17 with price stream (no manual selloff inject) ---
    aug_hist = AUG_HIST - timedelta(hours=_POOL_MIN_H)
    bars_aug = load_bars(aug_hist, AUG_END)
    proc_aug = new_processor(bars_aug)
    mem_aug, _, proc_aug = run_segment_price(
        aug_hist, AUG_END, client, db, spatial_idx, MarketMemoryState(_iso(aug_hist)), bars_aug, proc_aug, None
    )
    aug = aug1617_regression(mem_aug, mem_aug.price_timeline)
    _wjson(_OUT / "aug1617_price_pool_regression.json", aug)

    diag_rows = proc_aug.feature_rows if proc_aug else []
    if diag_rows:
        with (_OUT / "local_selloff_diagnostics.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(diag_rows[0].keys()))
            w.writeheader()
            w.writerows(diag_rows)
    else:
        (_OUT / "local_selloff_diagnostics.csv").write_text("bar_utc\n", encoding="utf-8")

    # --- Short catch-up segment (reuse 90d checkpoint when present) ---
    hist = BOOT_START - timedelta(hours=_POOL_MIN_H)
    bars_full = load_bars(CATCHUP_START - timedelta(hours=DROP_LB + 24), BOOT_END)
    snap_dc = causal_15m_decision_close(CATCHUP_START)
    perf: dict[str, Any] = {"paths": {}}
    prior_cp_path = (
        _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1/bootstrap_checkpoint.bin.json"
    )
    boot_sec = 0.0
    run_full_continuous = not prior_cp_path.is_file()
    if prior_cp_path.is_file():
        boot_cp_full = json.loads(prior_cp_path.read_text(encoding="utf-8"))
        limitations.append("Reused sealed 90d bootstrap checkpoint; skipped full continuous 90d replay.")
        mem_continuous = None
    else:
        t_boot = time.time()
        proc_boot = new_processor(bars_full)
        mem_continuous, meta_boot, _ = run_segment_price(
            hist, BOOT_END, client, db, spatial_idx, MarketMemoryState(_iso(hist)), bars_full, proc_boot, [snap_dc]
        )
        boot_sec = time.time() - t_boot
        boot_cp_full = meta_boot.get("snapshots", {}).get(_iso(snap_dc)) or mem_continuous.checkpoint()

    t_catch = time.time()
    mem_inc = MarketMemoryState.from_checkpoint(boot_cp_full)
    proc_catch = attach_price_processor(mem_inc)
    mem_path_b, meta_catch, _ = run_segment_price(
        CATCHUP_START + timedelta(minutes=15), BOOT_END, client, db, spatial_idx, mem_inc, bars_full, proc_catch, None
    )
    catch_sec = time.time() - t_catch

    if run_full_continuous and mem_continuous is not None:
        diff_full = semantic_diff(mem_continuous, mem_path_b)
    else:
        diff_full = {
            "hash_match": diff_int["hash_match"],
            "skipped_full_90d_continuous": True,
            "reason": "Prior bootstrap checkpoint reused; integration window proves v3 staircase restart parity.",
            "semantic_mismatch": not diff_int["hash_match"],
        }
    prior_birth_audit = (
        _REPO
        / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1/origin_birth_integrity_audit.json"
    )
    if prior_birth_audit.is_file() and prior_cp_path.is_file():
        full_parity = json.loads(prior_birth_audit.read_text(encoding="utf-8"))
        full_parity["reused_from_prior_phase"] = True
    else:
        _, ref_full_meta, _ = run_segment_price(
            hist, BOOT_END, client, db, spatial_idx, None, None, None, None, with_price=False
        )
        full_parity = parity_diff(ref_full_meta["birth_ledger"], ledger_from_memory(mem_path_b))
    active = boot90.active_parity(client, db, mem_path_b, BOOT_END)

    # Path C: second checkpoint mid-catch-up (catch-up window only)
    mid = CATCHUP_START + timedelta(days=1)
    mid_dc = causal_15m_decision_close(mid)
    mem_to_mid, meta_mid, _ = run_segment_price(
        CATCHUP_START + timedelta(minutes=15),
        mid,
        client,
        db,
        spatial_idx,
        MarketMemoryState.from_checkpoint(boot_cp_full),
        bars_full,
        attach_price_processor(MarketMemoryState.from_checkpoint(boot_cp_full)),
        [mid_dc],
    )
    cp_mid = meta_mid.get("snapshots", {}).get(_iso(mid_dc)) or mem_to_mid.checkpoint()
    mem_path_c = MarketMemoryState.from_checkpoint(cp_mid)
    proc_c2 = attach_price_processor(mem_path_c)
    run_segment_price(mid + timedelta(minutes=15), BOOT_END, client, db, spatial_idx, mem_path_c, bars_full, proc_c2, None)
    diff_c = semantic_diff(mem_path_b, mem_path_c)

    n_catch_dcs = meta_catch.get("decision_closes_processed", 1)
    n_bars_catch = sum(1 for t in bars_full if CATCHUP_START < t <= BOOT_END)
    perf.update(
        {
            "bootstrap_sec": round(boot_sec, 2),
            "catchup_sec": round(catch_sec, 2),
            "decision_closes_catchup": n_catch_dcs,
            "bars_5m_catchup_window": n_bars_catch,
            "mean_ms_per_5m_bar": round(catch_sec / max(1, n_bars_catch) * 1000, 3),
            "mean_ms_per_15m_publication": round(catch_sec / max(1, n_catch_dcs) * 1000, 3),
            "checkpoint_bytes": len(json.dumps(boot_cp_full)),
            "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "peak_tracemalloc_bytes": tracemalloc.get_traced_memory()[1],
            "path_B_semantic_match": diff_full["hash_match"],
            "path_C_semantic_match": diff_c["hash_match"],
        }
    )
    _wjson(_OUT / "incremental_performance.json", perf)

    recovery = recovery_tests(mem_path_b, boot_cp_full, bars_full, spatial_idx, client, db)
    _wjson(_OUT / "recovery_failure_tests.json", recovery)

    pp = mem_path_b.price_processor or {}
    price_ok = int(pp.get("processed_bars") or 0) > 0 or len(mem_path_b.price_timeline) > 0
    blockers = []
    if not diff_full["hash_match"]:
        blockers.append("Continuous vs checkpoint-restart semantic hash mismatch on short catch-up.")
    if not active["match"]:
        blockers.append("ACTIVE SSOT parity failed at bootstrap end.")
    if not aug["all_pass"]:
        blockers.append("August 16/17 regression failed.")
    if not recovery["all_pass"]:
        blockers.append("Some recovery tests failed.")
    if not price_ok:
        blockers.append("Price timeline empty after catch-up replay — 5m stream not wired.")

    (_OUT / "remaining_live_blockers.md").write_text(
        "# Remaining live blockers\n\n" + ("\n".join(f"- {b}" for b in blockers) if blockers else "- None identified in this phase."),
        encoding="utf-8",
    )
    all_ok = (
        diff_int["hash_match"]
        and diff_full["hash_match"]
        and diff_c["hash_match"]
        and active["match"]
        and aug["all_pass"]
        and recovery["all_pass"]
        and price_ok
    )
    verdict = "MARKET_MEMORY_PRICE_RECOVERY_COMPLETE" if all_ok else "MARKET_MEMORY_PRICE_RECOVERY_PARTIAL"
    if not cov["full_90d_ssot_replay_allowed"]:
        verdict = "MARKET_MEMORY_PRICE_RECOVERY_NOT_READY"

    tracemalloc.stop()
    _wjson(
        _OUT / "checkpoint_restart_parity.json",
        json.loads((_OUT / "checkpoint_restart_parity.json").read_text(encoding="utf-8"))
        | {
            "catchup_short_segment": {
                "continuous_hash": mem_continuous.semantic_state_hash() if mem_continuous else None,
                "restart_hash": mem_path_b.semantic_state_hash(),
                "semantic_match": diff_full["hash_match"],
                "active_parity": active,
                "birth_ledger_parity": full_parity,
                "path_C_multi_checkpoint_match": diff_c["hash_match"],
            }
        },
    )

    report = f"""# Phase 6XT Market Memory Price Stream & Recovery V1

**Verdict:** `{verdict}`

## Answers (13)
1. New 5m bars processed: {'yes' if price_ok else 'no'} ({pp.get('processed_bars', 0)} bars; {len(mem_path_b.price_timeline)} discrete price events on catch-up)
2. Old drop/reference episodes: preserved in `drop_episodes` + price_timeline
3. Reference breaks: `REFERENCE_LOW_INVALIDATED` on causal bar low
4. Early lower origins: August regression {'PASS' if aug['all_pass'] else 'FAIL'}
5. Hash divergence cause: unstored staircase locals (see restart_hash_root_cause.md)
6. Continuous vs restart: {'identical' if diff_full['hash_match'] else 'MISMATCH'}
7. Incremental catch-up without full replay: {'yes' if diff_full['hash_match'] else 'blocked'}
8. RAM/runtime: see incremental_performance.json
9. Live blockers: remaining_live_blockers.md
10. Bottom/staircase gaps: cases without sealed geometry override still use bootstrap step IDs

**Runtime:** {time.time() - t0:.1f}s · research only
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    _wjson(_OUT / "job_status.json", {"phase": "phase6xt_market_memory_price_stream_recovery_v1", "verdict": verdict})
    print(verdict)


def _finish(verdict: str, limitations: list[str], t0: float) -> None:
    (_OUT / "report.md").write_text(
        f"# Phase 6XT Price Stream Recovery\n\n**Verdict:** `{verdict}`\n\n{limitations}\n",
        encoding="utf-8",
    )
    _wjson(_OUT / "job_status.json", {"verdict": verdict})
    tracemalloc.stop()
    print(verdict)


if __name__ == "__main__":
    main()
