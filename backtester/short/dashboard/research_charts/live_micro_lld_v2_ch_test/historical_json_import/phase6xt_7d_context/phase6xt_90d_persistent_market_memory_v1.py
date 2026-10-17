#!/usr/bin/env python3
"""Phase 6XT-90D-PERSISTENT-MARKET-MEMORY V1 (+ Aug16/17 addendum regression)."""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_persistent_market_memory_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_AUDIT = _REPO / "logs/historical_import/overnight_2026/phase6xt_aug16_17_microdrop_early_lower_audit_v1"

REPLAY_CUTOFF_START = datetime(2026, 8, 16, 21, 30, tzinfo=timezone.utc)
REPLAY_CUTOFF_END = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)
AUG_HIST = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import phase6xt_7d_context_v1 as c7  # noqa: E402
from market_memory_state import (  # noqa: E402
    EVENT_TRUE_UPWARD,
    MarketMemoryState,
)
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson


def _parse(s: str) -> datetime:
    return parse_decision_close(s)


def load_spatial_index() -> dict[tuple[int, str, str], dict]:
    idx: dict[tuple[int, str, str], dict] = {}
    for r in csv.DictReader((_GEOM / "actual_higher_lower_steps.csv").open(encoding="utf-8")):
        if r.get("period") != "august":
            continue
        key = (int(r["pool_detail"]), r["origin_id"], r["decision_close"])
        idx[key] = {"kind": r.get("kind"), "step_id": r.get("step_id")}
    for r in csv.DictReader((_GEOM / "existing_step_reinforcements.csv").open(encoding="utf-8")):
        if r.get("period") != "august":
            continue
        key = (int(r["pool_detail"]), r["origin_id"], r["decision_close"])
        if key not in idx:
            idx[key] = {"kind": r.get("kind"), "step_id": r.get("step_id")}
    return idx


def load_canonical_august_lower_events() -> list[dict]:
    path = _GEOM / "canonical_origin_births.csv"
    rows = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r.get("period") != "august" or r.get("side") != "lower":
            continue
        r["pool_detail"] = int(r["pool_detail"])
        r["is_new_independent_origin"] = r.get("is_new_independent_origin") == "True"
        r["bottom"] = float(r["bottom"])
        r["top"] = float(r["top"])
        r["strength"] = float(r["strength"]) if r.get("strength") else None
        rows.append(r)
    rows.sort(
        key=lambda x: (
            _parse(x["known_at"]),
            _parse(x["decision_close"]),
            x["pool_detail"],
            x["origin_id"],
            x["pool_id"],
        )
    )
    return rows


def apply_events_until(state: MarketMemoryState, events: list[dict], end: datetime, spatial_idx: dict) -> None:
    for r in events:
        dc = _parse(r["decision_close"])
        ka = _parse(r["known_at"])
        if dc > end or ka > end:
            continue
        if _parse(state.asof_utc) > end:
            break
        key = (r["pool_detail"], r["origin_id"], r["decision_close"])
        sp = spatial_idx.get(key, {})
        state.apply_lower_pool_event(
            pool_detail=r["pool_detail"],
            origin_id=r["origin_id"],
            pool_id=r["pool_id"],
            known_at=r["known_at"],
            decision_close=r["decision_close"],
            bottom=r["bottom"],
            top=r["top"],
            strength=r["strength"],
            is_new_origin=r["is_new_independent_origin"],
            spatial_kind=sp.get("kind"),
            step_id=sp.get("step_id"),
        )


def build_warmup_state(spatial_idx: dict) -> MarketMemoryState:
    state = MarketMemoryState(asof_utc=_iso(AUG_HIST))
    events = load_canonical_august_lower_events()
    apply_events_until(state, events, REPLAY_CUTOFF_START, spatial_idx)
    return state


def inject_audit_extras(state: MarketMemoryState) -> None:
    """Local selloff + drop reference from sealed micro-drop audit (diagnostic)."""
    state.record_local_selloff(
        asof_utc="2026-08-16T21:45:00Z",
        low_price=0.06913,
        window_start_utc="2026-08-16T21:35:00Z",
        window_end_utc="2026-08-16T21:45:00Z",
        note="aug16_micro_selloff;THRESHOLD_NOT_MET_vs_2.5pct_drop_rule",
    )
    for pd in (1, 10):
        state.record_drop_episode_reference(
            asof_utc="2026-08-16T21:30:00Z",
            pool_detail=pd,
            episode_id="aug13_merged_episode",
            reference_low=0.0699,
            source_drop_close="2026-08-13T10:30:00Z",
            status="INVALIDATED",
            diagnostic_only=False,
        )


def run_replay_from_checkpoint(cp: dict, spatial_idx: dict, post: list[dict]) -> MarketMemoryState:
    state = MarketMemoryState.from_checkpoint(cp)
    apply_events_until(state, post, REPLAY_CUTOFF_END, spatial_idx)
    inject_audit_extras(state)
    return state


def regression_assertions(state: MarketMemoryState) -> list[dict]:
    checks = []
    oid_2145 = "lldv2:DOGEUSDT:15m:lower:1786915800"
    oid_0030 = "lldv2:DOGEUSDT:15m:lower:1786925700"
    oid_0215 = "lldv2:DOGEUSDT:15m:lower:1786932000"
    oid_pd10_s2 = "lldv2:DOGEUSDT:15m:lower:1786938300"
    oid_pd1_s2 = "lldv2:DOGEUSDT:15m:lower:1786941000"

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "pass": ok, "detail": detail})

    for pd in (1, 10):
        chk(f"origin_2145_pd{pd}", state.origin_present(pd, oid_2145), oid_2145)
        chk(f"origin_0030_pd{pd}", state.origin_present(pd, oid_0030), oid_0030)

    chk(
        "origin_0030_not_true_upward_pd1",
        not state.has_true_upward_at(1, oid_0030, "2026-08-17T00:30:00Z"),
        "00:30 = within_zone per geometry",
    )
    chk(
        "origin_0030_not_true_upward_pd10",
        not state.has_true_upward_at(10, oid_0030, "2026-08-17T00:30:00Z"),
        "",
    )
    chk("pd1_s1_0215", "august:PD1:S1" in state.step_ids(1), str(state.step_ids(1)))
    chk("pd10_s1_0215", "august:PD10:S1" in state.step_ids(10), str(state.step_ids(10)))
    chk("pd10_s2_0415", "august:PD10:S2" in state.step_ids(10), str(state.step_ids(10)))
    chk("pd1_s2_0445", "august:PD1:S2" in state.step_ids(1), str(state.step_ids(1)))

    rec = state.origins[1][oid_0030]
    chk(
        "origin_0030_birth_immutable",
        rec.first_causal_available_at == "2026-08-17T00:30:00Z"
        and rec.birth_decision_close == "2026-08-17T00:30:00Z",
        json.dumps(rec.to_dict()),
    )
    chk(
        "origin_0215_still_has_original_birth",
        state.origins[1][oid_0215].birth_decision_close == "2026-08-17T02:15:00Z",
        "",
    )

    future_origin = "lldv2:DOGEUSDT:15m:lower:1786950900"  # 07:30 S3
    chk("no_future_s3_origin", not state.origin_present(1, future_origin), future_origin)

    diag = [e for e in state.event_log if e.diagnostic_only]
    chk("local_selloff_diagnostic_logged", len(diag) >= 2, f"count={len(diag)}")

    return checks


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    spatial_idx = load_spatial_index()

    warmup = build_warmup_state(spatial_idx)
    cp_2130 = warmup.checkpoint()
    _wjson(_OUT / "checkpoint_2026-08-16T21_30Z.json", cp_2130)

    full = MarketMemoryState.from_checkpoint(cp_2130)
    events = load_canonical_august_lower_events()
    post = [e for e in events if _parse(e["decision_close"]) > REPLAY_CUTOFF_START]
    apply_events_until(full, post, REPLAY_CUTOFF_END, spatial_idx)
    inject_audit_extras(full)
    full._advance_asof(_iso(REPLAY_CUTOFF_END))

    restarted = run_replay_from_checkpoint(cp_2130, spatial_idx, post)
    restarted._advance_asof(_iso(REPLAY_CUTOFF_END))

    hash_full = full.state_hash()
    roundtrip = MarketMemoryState.from_checkpoint(full.checkpoint())
    checks = regression_assertions(full)
    checks.append(
        {
            "check": "checkpoint_roundtrip_identical",
            "pass": hash_full == roundtrip.state_hash(),
            "detail": hash_full[:16],
        }
    )
    checks.append(
        {
            "check": "checkpoint_replay_matches_continuous",
            "pass": hash_full == restarted.state_hash(),
            "detail": f"continuous={hash_full[:16]} replay={restarted.state_hash()[:16]}",
        }
    )

    with (_OUT / "event_log.jsonl").open("w", encoding="utf-8") as f:
        for e in full.event_log:
            f.write(json.dumps(e.to_dict(), sort_keys=True) + "\n")

    _wjson(
        _OUT / "aug1617_regression_results.json",
        {
            "replay_window": {"start": _iso(REPLAY_CUTOFF_START), "end": _iso(REPLAY_CUTOFF_END)},
            "state_hash_at_end": hash_full,
            "checkpoint_hash_at_2130": cp_2130["state_hash"],
            "checks": checks,
            "all_pass": all(c["pass"] for c in checks),
        },
    )

    spec = """# MarketMemoryState (research v1)

## Event types
- **ORIGIN_BIRTH** — new independent lower origin (always stored; birth time immutable)
- **REINFORCEMENT** — same-origin pool variant or within-zone / geometry reinforcement
- **TRUE_UPWARD_FORMATION** — sealed `new_spatial_step` from staircase geometry only
- **DIAGNOSTIC_LOCAL_SELLOFF** — local price selloff; never auto DROP_EVENT
- **DROP_EPISODE_REFERENCE** — historical detector episode metadata (unchanged 2.5% rule)

## Isolation
PD1 and PD10 kept in separate maps (`origins`, `spatial_steps`, `drop_episodes`).

## Causality
Events rejected when `known_at` or `decision_close` > current `asof_utc`.

## Addendum sources
- `phase6xt_aug16_17_microdrop_early_lower_audit_v1/`
- `phase6xt_causal_pool_staircase_geometry_v1/` (spatial classification)
"""
    (_OUT / "market_memory_state_spec.md").write_text(spec, encoding="utf-8")

    report = f"""# Phase 6XT-90D-PERSISTENT-MARKET-MEMORY V1 — Addendum complete

**Mode:** research-only · no live deployment · drop detector thresholds unchanged

## Aug 16/17 replay regression
- Warmup: canonical august lower origins through **{_iso(REPLAY_CUTOFF_START)}**
- Replay: causal events through **{_iso(REPLAY_CUTOFF_END)}**
- Local selloff **21:35–21:45** (~0.06913) logged as **DIAGNOSTIC_LOCAL_SELLOFF**
- Verdict: **{"PASS" if all(c["pass"] for c in checks) else "FAIL"}**

## Checks
{chr(10).join(f"- {c['check']}: {'OK' if c['pass'] else 'FAIL'} {c.get('detail','')}" for c in checks)}

## Artifacts
- `market_memory_state.py` (module)
- `checkpoint_2026-08-16T21_30Z.json`
- `aug1617_regression_results.json`
- `event_log.jsonl`
- `market_memory_state_spec.md`

Runtime: {time.time() - t0:.1f}s
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    _wjson(
        _OUT / "job_status.json",
        {
            "phase": "phase6xt_90d_persistent_market_memory_v1",
            "verdict": "PERSISTENT_MARKET_MEMORY_ADDENDUM_COMPLETE"
            if all(c["pass"] for c in checks)
            else "PERSISTENT_MARKET_MEMORY_ADDENDUM_PARTIAL",
            "runtime_sec": round(time.time() - t0, 2),
        },
    )
    _wjson(
        _OUT / "source_contract.json",
        {
            "audit_addendum": str(_AUDIT),
            "geometry": str(_GEOM),
            "module": "market_memory_state.py",
        },
    )


if __name__ == "__main__":
    main()
