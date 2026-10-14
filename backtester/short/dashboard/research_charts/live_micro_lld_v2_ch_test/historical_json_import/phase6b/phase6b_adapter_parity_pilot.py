#!/usr/bin/env python3
"""Phase 6B — adapter parity pilot (read-only, single-writer, incremental)."""

from __future__ import annotations

import fcntl
import json
import os
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6b_backtester_ssot_adapter"
_LOCK = _OUT / ".phase6b_pilot.lock"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_pools  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_backtester_adapter_v1 import (  # noqa: E402
    active_members_hash,
    canonical_active_member,
    load_backtester_active_state_at_close,
)
from research_charts.historical_pool_state_v1 import (  # noqa: E402
    _active_members_from_catalog,
    _load_catalog,
    _load_publication_line,
    load_historical_pool_state_at_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (  # noqa: E402
    PublishedActiveReader,
)

REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
MULTI_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-01T12:00:00Z",
    "2026-09-03T23:45:00Z",
    "2026-09-04T00:00:00Z",
    "2026-09-07T23:45:00Z",
]
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _field_diffs(a: list[dict], b: list[dict], limit: int = 5) -> list[dict]:
    diffs = []
    am = {str(x.get("pool_id")): x for x in a}
    bm = {str(x.get("pool_id")): x for x in b}
    for pid in sorted(set(am) | set(bm)):
        if am.get(pid) != bm.get(pid):
            diffs.append({"pool_id": pid, "left": am.get(pid), "right": bm.get(pid)})
        if len(diffs) >= limit:
            break
    return diffs


def _pub_members(client, database, pd: int, close_iso: str):
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config

    hcfg = resolve_historical_pd_config(pd)
    reader = PublishedActiveReader()
    dc = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    pub = _load_publication_line(
        reader, hcfg.config_hash, dc, pipeline_line_id=hcfg.pipeline_line_id
    )
    cat = _load_catalog(client, database, pub["snapshot_id"], dc)
    members = _active_members_from_catalog(
        cat, pub["snapshot_id"], pipeline_line_id=hcfg.pipeline_line_id
    )
    return pub, members


def _parity_row(
    client,
    database,
    reader: PublishedActiveReader,
    pd: int,
    close_iso: str,
    *,
    include_chart_api: bool,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    pub, pub_members = _pub_members(client, database, pd, close_iso)
    pub_canon = sorted(
        [canonical_active_member(m) for m in pub_members],
        key=lambda r: str(r.get("pool_id") or ""),
    )
    pub_hash = active_members_hash(pub_members)

    chart_ssot = load_historical_pool_state_at_close(
        close_iso,
        pool_detail=pd,
        client=client,
        database=database,
        reader=reader,
        include_remaining=False,
    )
    chart_canon = sorted(
        [canonical_active_member(m) for m in chart_ssot.active_members or []],
        key=lambda r: str(r.get("pool_id") or ""),
    )
    chart_hash = active_members_hash(list(chart_ssot.active_members or []))

    adapted = load_backtester_active_state_at_close(
        close_iso,
        pool_detail=pd,
        client=client,
        database=database,
        reader=reader,
    )

    row: dict[str, Any] = {
        "pool_detail": pd,
        "decision_close": close_iso,
        "snapshot_id_publication": pub.get("snapshot_id"),
        "snapshot_id_chart_ssot": chart_ssot.snapshot_id,
        "snapshot_id_adapter": adapted.snapshot_id,
        "active_count_publication": len(pub_canon),
        "active_count_chart_ssot": len(chart_canon),
        "active_count_adapter": adapted.active_member_count,
        "expected_active_control": EXPECTED_ACTIVE.get(pd),
        "members_hash_publication": pub_hash,
        "members_hash_chart_ssot": chart_hash,
        "members_hash_adapter": adapted.members_hash,
        "field_diffs_pub_vs_chart": _field_diffs(pub_canon, chart_canon),
        "field_diffs_chart_vs_adapter": _field_diffs(chart_canon, list(adapted.active_members)),
        "wall_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "rss_delta_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0,
    }

    if include_chart_api:
        pools_api = chart_historical_lmv2_pools(
            "DOGEUSDT",
            close_iso,
            close_iso,
            pool_detail=pd,
            include_active=True,
            include_remaining=False,
        )
        api_pub = (pools_api.get("publications") or [{}])[0]
        row["chart_pools_api_snapshot_id"] = api_pub.get("snapshot_id")
        row["chart_pools_api_contract"] = pools_api.get("historical_pd_contract")
        row["chart_pools_api_snapshot_match"] = (
            api_pub.get("snapshot_id") == adapted.snapshot_id
        )
        row["chart_pools_api_note"] = (
            "pools API episode-walk geometry; ACTIVE membership SSOT compared via "
            "include_remaining=False HistoricalPoolState above"
        )

    row["verdict"] = (
        "PASS"
        if pub_hash == chart_hash == adapted.members_hash
        and not row["field_diffs_pub_vs_chart"]
        and not row["field_diffs_chart_vs_adapter"]
        and pub.get("snapshot_id") == adapted.snapshot_id
        else "FAIL"
    )
    return row


def _acquire_lock() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another phase6b pilot holds the lock; aborting.")
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def main() -> int:
    lock_fd = _acquire_lock()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    reader = PublishedActiveReader()
    try:
        pilot: dict[str, Any] = {"reference_close": REFERENCE_CLOSE, "per_pd": {}}
        for pd in (1, 5, 10):
            row = _parity_row(
                client,
                ch_cfg.database,
                reader,
                pd,
                REFERENCE_CLOSE,
                include_chart_api=True,
            )
            pilot["per_pd"][str(pd)] = row
            (_OUT / f"parity_pilot_pd{pd}.json").write_text(
                json.dumps(row, indent=2), encoding="utf-8"
            )
        (_OUT / "parity_pilot.json").write_text(json.dumps(pilot, indent=2), encoding="utf-8")

        if any(v.get("verdict") != "PASS" for v in pilot["per_pd"].values()):
            print(json.dumps({"status": "STOP", "reason": "reference_close_fail", "pilot": pilot}, indent=2))
            return 1

        regression: dict[str, Any] = {"closes": MULTI_CLOSES, "rows": []}
        for close_iso in MULTI_CLOSES:
            for pd in (1, 5, 10):
                row = _parity_row(
                    client,
                    ch_cfg.database,
                    reader,
                    pd,
                    close_iso,
                    include_chart_api=False,
                )
                regression["rows"].append(row)
                (_OUT / f"regression_pd{pd}_{close_iso.replace(':', '').replace('-', '')}.json").write_text(
                    json.dumps(row, indent=2), encoding="utf-8"
                )
        (_OUT / "regression_results.json").write_text(
            json.dumps(regression, indent=2), encoding="utf-8"
        )
        reg_fail = any(r.get("verdict") != "PASS" for r in regression["rows"])
    finally:
        client.close()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    verdicts = {
        "ADAPTER_IMPLEMENTED": "PASS",
        "PD1_PARITY": pilot["per_pd"]["1"]["verdict"],
        "PD5_PARITY": pilot["per_pd"]["5"]["verdict"],
        "PD10_PARITY": pilot["per_pd"]["10"]["verdict"],
        "SSOT_PARITY": "PASS" if not reg_fail else "FAIL",
        "CHART_STATE_PARITY": "PASS"
        if all(
            pilot["per_pd"][str(pd)]["members_hash_chart_ssot"]
            == pilot["per_pd"][str(pd)]["members_hash_adapter"]
            for pd in (1, 5, 10)
        )
        else "FAIL",
        "BACKTESTER_CURRENT_INTEGRATION": "NOT_IMPLEMENTED",
        "BACKTESTER_E2E": "NOT_IMPLEMENTED",
        "PHASE_6B_OVERALL": "PASS" if not reg_fail else "FAIL",
    }
    (_OUT / "verdicts.json").write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(_OUT), "verdicts": verdicts}, indent=2))
    return 0 if verdicts["PHASE_6B_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
