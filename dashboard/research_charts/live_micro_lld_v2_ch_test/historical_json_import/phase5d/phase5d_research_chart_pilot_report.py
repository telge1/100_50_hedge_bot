#!/usr/bin/env python3
"""Phase 5D — generate research chart pilot validation report."""

from __future__ import annotations

import hashlib
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

from research_charts.chart_historical_lmv2_pools import (
    PILOT_FROM,
    PILOT_TO,
    chart_historical_lmv2_pools,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.phase5c.phase5c_20_closes_validation import (
    _remaining_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    CandleCache,
    ClickHousePublicationSource,
    load_historical_remaining,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.phase5c.phase5c_20_closes_validation import (
    IMMUTABLE_PRICE_HASH,
    IMMUTABLE_REVISION_HASH,
    IMMUTABLE_SNAPSHOT_ID,
    LineFilteredPublicationSource,
    PIPELINE_LINE_ID,
    POOL_DETAIL,
    SNAPSHOT_ROOT,
    SYMBOL,
    TIMEFRAME,
)
from research_charts.clickhouse_config import load_clickhouse_config

import clickhouse_connect

OUT = _REPO / "logs" / "historical_import" / "overnight_2026" / "phase5d_research_chart_pilot.json"
PHASE5C = _REPO / "logs" / "historical_import" / "overnight_2026" / "phase5c_20_closes_validation.json"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    phase5c = json.loads(PHASE5C.read_text(encoding="utf-8")) if PHASE5C.is_file() else {}
    t0 = time.perf_counter()
    checks: list[dict] = []

    active_payload = chart_historical_lmv2_pools(
        "DOGEUSDT", PILOT_FROM, PILOT_TO, include_active=True, include_remaining=False
    )
    checks.append(
        {
            "id": "publications_20",
            "status": "PASS" if active_payload["pilot_close_count"] == 20 else "FAIL",
            "count": active_payload["pilot_close_count"],
        }
    )
    c_last = phase5c.get("active_timeline", {}).get("per_close", [{}])[-1]
    checks.append(
        {
            "id": "active_count_last_close",
            "status": "PASS" if active_payload["pool_count"] >= int(c_last.get("active_count", 0)) - 3 else "FAIL",
            "chart_pool_episodes": active_payload["pool_count"],
            "phase5c_last_close": c_last.get("active_count"),
        }
    )

    rem_payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        PILOT_FROM,
        PILOT_TO,
        include_active=False,
        include_remaining=True,
        as_of_close=PILOT_TO,
        remaining_limit=1500,
    )
    checks.append(
        {
            "id": "remaining_capped",
            "status": "PASS" if rem_payload["remaining_truncated"] else "NOT_VERIFIED",
            "segments_returned": rem_payload["remaining_segment_count"],
        }
    )

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    filtered = LineFilteredPublicationSource(ClickHousePublicationSource(client, ch_cfg.database), PIPELINE_LINE_ID)
    rem = load_historical_remaining(
        filtered,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        pool_detail=POOL_DETAIL,
        decision_close=PILOT_TO,
        immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
        price_content_hash=IMMUTABLE_PRICE_HASH,
        revision_metadata_hash=IMMUTABLE_REVISION_HASH,
        snapshot_root=SNAPSHOT_ROOT,
        cache=CandleCache(max_entries=1),
    )
    fp5c = None
    for row in phase5c.get("remaining_timeline", {}).get("per_close", []):
        if row.get("decision_close") == "2026-08-01T04:45:00Z":
            fp5c = row.get("fingerprint")
    fp_now = _remaining_fingerprint(rem)
    checks.append(
        {
            "id": "remaining_fingerprint_vs_5c",
            "status": "PASS" if fp5c and fp5c == fp_now else "FAIL",
            "phase5c_fingerprint": fp5c,
            "current_fingerprint": fp_now,
        }
    )
    client.close()

    win_a = chart_historical_lmv2_pools(
        "DOGEUSDT", PILOT_FROM, PILOT_TO, include_active=True, include_remaining=False
    )
    win_b = chart_historical_lmv2_pools(
        "DOGEUSDT",
        datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc),
        PILOT_TO,
        include_active=True,
        include_remaining=False,
    )
    pub_full = {p["decision_close"]: p for p in win_a["publications"]}
    pub_sub = {p["decision_close"]: p for p in win_b["publications"]}
    spot = "2026-08-01T04:45:00Z"
    checks.append(
        {
            "id": "window_independence_snapshot_at_close",
            "status": "PASS"
            if pub_full.get(spot, {}).get("snapshot_id") == pub_sub.get(spot, {}).get("snapshot_id")
            else "FAIL",
            "decision_close": spot,
            "snapshot_id": pub_full.get(spot, {}).get("snapshot_id"),
        }
    )

    report = {
        "schema_version": 1,
        "phase": "5D",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "api_route": "POST /api/research/historical-lld-v2-pools",
        "ui": {
            "checkbox": "researchIndHistoricalLmv2Pools",
            "layers": ["researchIndHistoricalLmv2Active", "researchIndHistoricalLmv2Remaining"],
            "banner": "researchHistoricalLmv2Banner",
            "replaced": "researchLiveMicroV2ChCompare (V2 Compare — was JS-only, no HTML)",
        },
        "checks": checks,
        "performance": {"elapsed_seconds": round(time.perf_counter() - t0, 2)},
        "limitations": [
            "Pilot locked to 20 closes 2026-08-01 00:00–04:45 UTC",
            "Remaining segments capped at 1500 in API for chart performance",
            "RESEARCH_HISTORICAL banner only; no runtime visibility gate in CH publish table",
            "DOGEUSDT 15m PD10 only",
        ],
        "summary": {
            "overall": "FAIL"
            if any(c["status"] == "FAIL" for c in checks)
            else ("PASS_WITH_NOT_VERIFIED" if any(c["status"] == "NOT_VERIFIED" for c in checks) else "PASS"),
            "pass": sum(1 for c in checks if c["status"] == "PASS"),
            "fail": sum(1 for c in checks if c["status"] == "FAIL"),
            "not_verified": sum(1 for c in checks if c["status"] == "NOT_VERIFIED"),
        },
    }
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "overall": report["summary"]["overall"]}, indent=2))
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
