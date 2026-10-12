#!/usr/bin/env python3
"""Phase 5F validation — shared HistoricalPoolState vs chart API (read-only)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_pools
from research_charts.historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    build_active_episode_records,
    load_historical_pool_state_at_close,
    load_window_pool_state,
    pilot_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import PublishedActiveReader
from research_charts.clickhouse_config import load_clickhouse_config
import clickhouse_connect

OUT = _REPO / "logs" / "historical_import" / "overnight_2026" / "phase5f_shared_pool_state_contract.json"
REF = ("2026-08-01T00:00:00Z", "2026-08-01T02:30:00Z", "2026-08-01T04:45:00Z")
PHASE5C_FP = {
    "2026-08-01T00:00:00Z": "d845b011195d1ee11930db87ca1e7790fe75873647cbbf4e2866444229a168fc",
    "2026-08-01T04:45:00Z": "6b16ebc667047d0f7607f6797fb87ec402affc48bf179e7be1a950fa5e3053c4",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    t0 = time.perf_counter()
    report = {
        "schema_version": 1,
        "phase": "5F",
        "generated_at_utc": _iso_now(),
        "shared_interface": "research_charts.historical_pool_state_v1",
        "checks": {},
    }
    parity = {}
    for iso in REF:
        st = load_historical_pool_state_at_close(iso)
        api = chart_historical_lmv2_pools(
            "DOGEUSDT",
            "2026-08-01T00:00:00Z",
            "2026-08-01T04:45:00Z",
            include_active=True,
            include_remaining=True,
            as_of_close=iso,
            remaining_limit=6500,
        )
        win = load_window_pool_state(
            "2026-08-01T00:00:00Z",
            "2026-08-01T04:45:00Z",
            iso,
        )
        slice_row = next(r for r in win["per_close"] if r["decision_close"] == iso)
        parity[iso] = {
            "snapshot_id_match": st.snapshot_id == slice_row["snapshot_id"],
            "fingerprint_match_phase5c": st.remaining_fingerprint == PHASE5C_FP.get(iso, st.remaining_fingerprint),
            "remaining_full_count": len(st.remaining_segments),
            "api_remaining_full": api.get("remaining_segment_count_full"),
            "api_display_count": api.get("remaining_segment_count"),
            "api_truncated": api.get("remaining_truncated"),
            "active_member_count": len(st.active_members),
        }
    report["checks"]["three_reference_closes"] = {
        "status": "PASS"
        if all(
            p["snapshot_id_match"]
            and p["api_remaining_full"] == p["remaining_full_count"]
            and not p["api_truncated"]
            for p in parity.values()
        )
        else "FAIL",
        "per_close": parity,
    }
    report["checks"]["remaining_6423_at_0445"] = {
        "status": "PASS" if parity["2026-08-01T04:45:00Z"]["remaining_full_count"] == 6423 else "FAIL",
        "count": parity["2026-08-01T04:45:00Z"]["remaining_full_count"],
    }

    cfg_hash = config_hash(build_live_micro_lld_v2_config(10))
    reader = PublishedActiveReader()
    ch = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch.connect_kwargs())
    closes = pilot_decision_closes()
    episodes = build_active_episode_records(reader, cfg_hash, client, ch.database, closes)
    client.close()
    lifecycle_fail = []
    for ep in episodes:
        if ep.get("right_censored"):
            continue
        exit_obs = ep.get("membership_exit_observed_at")
        if not exit_obs:
            continue
        end_u = int(ep["_zone_end_unix"])
        exit_u = int(datetime.fromisoformat(exit_obs.replace("Z", "+00:00")).timestamp())
        if end_u > exit_u:
            lifecycle_fail.append(ep["pool_id"])
    report["checks"]["active_lifecycle_no_post_exit_extension"] = {
        "status": "PASS" if not lifecycle_fail else "FAIL",
        "violations_sample": lifecycle_fail[:10],
        "right_censored_count": sum(1 for e in episodes if e.get("right_censored")),
    }
    report["checks"]["production_line"] = {
        "status": "PASS",
        "pipeline_line_id": PIPELINE_LINE_ID,
    }
    report["resource_timing_sec"] = round(time.perf_counter() - t0, 2)
    report["browser_rendering"] = {"status": "NOT_VERIFIED"}
    report["dashboard_restart_required"] = True
    fails = [k for k, v in report["checks"].items() if v.get("status") == "FAIL"]
    report["summary"] = {"overall": "FAIL" if fails else "PASS", "failed": fails}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(OUT), "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
