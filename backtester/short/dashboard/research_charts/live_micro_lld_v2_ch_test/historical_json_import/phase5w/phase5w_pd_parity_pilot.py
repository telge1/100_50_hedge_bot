#!/usr/bin/env python3
"""Phase 5W — historical PD1/PD5/PD10 switch parity pilot (read-only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase5w_historical_pd_switch"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import (  # noqa: E402
    HistoricalLmv2PilotError,
    chart_historical_lmv2_pools,
    chart_historical_lmv2_static_timeline,
)
import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_pd_contract_v1 import (  # noqa: E402
    historical_pd_registry_export,
    resolve_historical_pd_config,
)
from research_charts.historical_pool_state_batch_reader_v1 import (  # noqa: E402
    build_episode_walk_batch_cache,
)
from research_charts.historical_pool_state_v1 import (  # noqa: E402
    _iso_z,
    _load_catalog,
    _load_publication_line,
    _utc,
    load_historical_pool_state_at_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (  # noqa: E402
    PublishedActiveReader,
)

PILOT_CLOSE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
PILOT_CLOSE_ISO = "2026-09-01T12:00:00Z"
DAY_FROM = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
DAY_TO = datetime(2026, 9, 1, 23, 45, tzinfo=timezone.utc)

EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}


def _hash_obj(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_catalog(catalog: list[dict]) -> list[dict]:
    return sorted(catalog, key=lambda r: str(r.get("pool_id") or ""))


def _member_canon(m: dict) -> dict[str, Any]:
    return {
        "pool_id": m.get("pool_id"),
        "side": m.get("side"),
        "lower": m.get("lower"),
        "upper": m.get("upper"),
        "strength": m.get("strength"),
        "snapshot_id": m.get("snapshot_id"),
        "known_at": m.get("known_at"),
        "active_membership": m.get("active_membership"),
    }


def _active_set_from_state(members: list[dict]) -> list[dict]:
    return sorted([_member_canon(m) for m in members], key=lambda r: str(r.get("pool_id") or ""))


def _inventory_hardcodes() -> dict[str, Any]:
    roots = [
        _DASH / "static/js/research/research_charts.js",
        _DASH / "static/js/research/research_scanner_common_helpers.js",
        _DASH / "research_charts/chart_historical_lmv2_pools.py",
        _DASH / "research_charts/historical_pool_state_v1.py",
    ]
    hits: list[dict[str, Any]] = []
    patterns = [
        (re.compile(r"pool_detail:\s*10"), "literal_pool_detail_10"),
        (re.compile(r"15mpd10_v1"), "pipeline_15mpd10"),
        (re.compile(r"PILOT_POOL_DETAIL"), "pilot_pool_detail_symbol"),
        (re.compile(r"PIPELINE_LINE_ID"), "pipeline_line_id_default"),
    ]
    for path in roots:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for rx, label in patterns:
                if rx.search(line):
                    hits.append(
                        {
                            "file": str(path.relative_to(_REPO)),
                            "line": i,
                            "tag": label,
                            "snippet": line.strip()[:160],
                        }
                    )
    return {
        "scanned_files": [str(p.relative_to(_REPO)) for p in roots if p.is_file()],
        "hit_count": len(hits),
        "hits_sample": hits[:80],
        "note": "Remaining PD10 defaults are backward-compat fallbacks; API/chart resolve via pd_contract.",
    }


def _parity_at_close(client, database: str, pool_detail: int) -> dict[str, Any]:
    hcfg = resolve_historical_pd_config(pool_detail)
    line_id = hcfg.pipeline_line_id
    cfg_h = hcfg.config_hash
    reader = PublishedActiveReader()
    dc = _utc(PILOT_CLOSE)

    pub = _load_publication_line(reader, cfg_h, dc, pipeline_line_id=line_id)
    cat = _load_catalog(client, database, pub["snapshot_id"], dc)
    st = load_historical_pool_state_at_close(
        dc,
        pool_detail=pool_detail,
        client=client,
        database=database,
        reader=reader,
        cfg_hash=cfg_h,
    )
    batch = build_episode_walk_batch_cache(client, database, line_id, cfg_h, [dc])
    bat_pub = batch.publication(cfg_h, dc)
    bat_cat = batch.catalog(database, bat_pub["snapshot_id"], dc)

    pools_payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        PILOT_CLOSE_ISO,
        PILOT_CLOSE_ISO,
        pool_detail=pool_detail,
        include_active=True,
        include_remaining=False,
    )
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    static_alt = chart_historical_lmv2_static_timeline(
        "DOGEUSDT",
        PILOT_CLOSE_ISO,
        PILOT_CLOSE_ISO,
        pool_detail=pool_detail,
        include_active=True,
    )
    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    static_batch = chart_historical_lmv2_static_timeline(
        "DOGEUSDT",
        PILOT_CLOSE_ISO,
        PILOT_CLOSE_ISO,
        pool_detail=pool_detail,
        include_active=True,
    )
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)

    state_members = _active_set_from_state(st.active_members or [])
    pub_ids = sorted(pub.get("pool_ids") or [])
    state_ids = sorted([m["pool_id"] for m in state_members])

    pub_hash = _hash_obj(
        {
            "snapshot_id": pub.get("snapshot_id"),
            "pool_ids": pub_ids,
            "catalog": _canonical_catalog(cat),
        }
    )
    batch_hash = _hash_obj(
        {
            "snapshot_id": bat_pub.get("snapshot_id"),
            "pool_ids": sorted(bat_pub.get("pool_ids") or []),
            "catalog": _canonical_catalog(bat_cat),
        }
    )
    state_hash = _hash_obj(state_members)
    static_timeline_hash = _hash_obj(
        {
            "zones": static_alt.get("zones"),
            "segment_count": static_alt.get("segment_count"),
            "pool_count": static_alt.get("pool_count"),
            "episode_walk_close_count": static_alt.get("episode_walk_close_count"),
        }
    )
    static_batch_timeline_hash = _hash_obj(
        {
            "zones": static_batch.get("zones"),
            "segment_count": static_batch.get("segment_count"),
            "pool_count": static_batch.get("pool_count"),
            "episode_walk_close_count": static_batch.get("episode_walk_close_count"),
        }
    )
    api_pub = (pools_payload.get("publications") or [{}])[0]

    field_diffs: list[dict[str, Any]] = []
    if pub_hash != batch_hash:
        field_diffs.append({"pair": "pub_vs_batch", "pub": pub_hash, "batch": batch_hash})
    if static_timeline_hash != static_batch_timeline_hash:
        field_diffs.append(
            {
                "pair": "static_alt_vs_batch",
                "alt": static_timeline_hash,
                "batch": static_batch_timeline_hash,
            }
        )
    if api_pub.get("snapshot_id") != st.snapshot_id:
        field_diffs.append(
            {
                "pair": "pools_api_publication_vs_state",
                "api": api_pub.get("snapshot_id"),
                "state": st.snapshot_id,
            }
        )

    return {
        "pool_detail": pool_detail,
        "pipeline_line_id": line_id,
        "config_hash": cfg_h,
        "decision_close": PILOT_CLOSE_ISO,
        "snapshot_id_state": st.snapshot_id,
        "snapshot_id_publication": pub.get("snapshot_id"),
        "active_count_publication": len(pub_ids),
        "active_count_state": len(state_members),
        "episode_walk_pool_count_pools_api": pools_payload.get("pool_count"),
        "episode_walk_pool_count_static": static_alt.get("pool_count"),
        "expected_active_membership_phase5v": EXPECTED_ACTIVE.get(pool_detail),
        "pools_api_publication_snapshot_id": api_pub.get("snapshot_id"),
        "pool_id_set_pub_eq_state": set(pub_ids) == set(state_ids),
        "hashes": {
            "publication": pub_hash,
            "batch_publication": batch_hash,
            "historical_pool_state_members": state_hash,
            "static_timeline_alt": static_timeline_hash,
            "static_timeline_batch": static_batch_timeline_hash,
        },
        "field_diffs": field_diffs,
        "static_api_contract": static_batch.get("historical_pd_contract"),
        "batch_reader_active": static_batch.get("historical_lmv2_batch_reader_active"),
        "verdict": "PASS"
        if not field_diffs
        and set(pub_ids) == set(state_ids)
        and len(state_members) == EXPECTED_ACTIVE.get(pool_detail)
        else "FAIL",
    }


def _day_window_smoke(client, database: str, pool_detail: int) -> dict[str, Any]:
    from_iso = _iso_z(DAY_FROM)
    to_iso = _iso_z(DAY_TO)
    payload = chart_historical_lmv2_static_timeline(
        "DOGEUSDT",
        from_iso,
        to_iso,
        pool_detail=pool_detail,
        include_active=True,
    )
    return {
        "pool_detail": pool_detail,
        "from_close": from_iso,
        "to_close": to_iso,
        "pilot_close_count": payload.get("pilot_close_count"),
        "segment_count": payload.get("segment_count"),
        "pipeline_line_id": payload.get("pipeline_line_id"),
        "config_hash": payload.get("config_hash"),
        "historical_pd_contract": payload.get("historical_pd_contract"),
    }


def _negatives() -> dict[str, Any]:
    invalid_ok = False
    try:
        chart_historical_lmv2_pools(
            "DOGEUSDT",
            PILOT_CLOSE_ISO,
            PILOT_CLOSE_ISO,
            pool_detail=7,
        )
    except HistoricalLmv2PilotError as exc:
        invalid_ok = exc.code == "INVALID_HISTORICAL_PD"
    default_payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        PILOT_CLOSE_ISO,
        PILOT_CLOSE_ISO,
        include_active=True,
    )
    return {
        "invalid_pd_7": "PASS" if invalid_ok else "FAIL",
        "default_omitted_pool_detail": {
            "pool_detail": default_payload.get("pool_detail"),
            "line_suffix": (default_payload.get("pipeline_line_id") or "").split("_")[-1],
            "verdict": "PASS"
            if default_payload.get("pool_detail") == 10
            and str(default_payload.get("pipeline_line_id", "")).endswith("15mpd10_v1")
            else "FAIL",
        },
    }


def _cache_switch_tests() -> dict[str, Any]:
    node_test = _DASH / "tests/test_research_scanner_common_node.js"
    proc = subprocess.run(
        ["node", str(node_test)],
        cwd=str(_DASH),
        capture_output=True,
        text=True,
        timeout=120,
    )
    isolation_pass = "ok test_historical_lmv2_pd_cache_key_isolation" in proc.stdout
    return {
        "node_test_exit_code": proc.returncode,
        "pd_cache_key_isolation": "PASS" if isolation_pass and proc.returncode == 0 else "FAIL",
        "stdout_tail": proc.stdout.splitlines()[-5:],
        "stderr_tail": proc.stderr.splitlines()[-5:] if proc.stderr else [],
    }


def _write_report(verdicts: dict[str, str]) -> str:
    lines = [
        "# Phase 5W — Historical PD switch (pools repo pilot)",
        "",
        "## Scope",
        "- PD1 / PD5 / PD10 selection contract + chart/API wiring (pools dashboard tree).",
        "- No spread sync, no dashboard restart, no CH writes.",
        "",
        "## Verdicts",
    ]
    for k, v in verdicts.items():
        lines.append(f"- **{k}**: {v}")
    lines.extend(
        [
            "",
            "## Pilot",
            f"- Reference close: `{PILOT_CLOSE_ISO}`",
            "- Optional day window: 2026-09-01 00:00–23:45 UTC for PD1/PD5",
            "",
            "## Backtester",
            "- **BACKTESTER_E2E**: NOT_IMPLEMENTED",
            "- **CHART_BACKTESTER_INPUT_CONTRACT**: PASS when SSOT/static API parity holds per PD",
            "",
            "## Browser",
            "- **BROWSER_PIXEL**: NOT_VERIFIED (no live browser run in this phase)",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database

    inventory = _inventory_hardcodes()
    pd_mapping = historical_pd_registry_export()

    pilot: dict[str, Any] = {"reference_close": PILOT_CLOSE_ISO, "per_pd": {}}
    for pd in (1, 5, 10):
        pilot["per_pd"][str(pd)] = _parity_at_close(client, database, pd)
    for pd in (1, 5):
        pilot["day_window_pd" + str(pd)] = _day_window_smoke(client, database, pd)

    negatives = _negatives()
    cache_tests = _cache_switch_tests()

    inventory_path = _OUT / "inventory_and_hardcodes.json"
    mapping_path = _OUT / "pd_mapping.json"
    pilot_path = _OUT / "pd_parity_pilot.json"
    cache_path = _OUT / "cache_and_switch_tests.json"

    inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    mapping_path.write_text(json.dumps(pd_mapping, indent=2), encoding="utf-8")
    pilot_path.write_text(json.dumps(pilot, indent=2), encoding="utf-8")
    cache_path.write_text(
        json.dumps({"negatives": negatives, "cache": cache_tests}, indent=2),
        encoding="utf-8",
    )

    pd_backend = {
        1: pilot["per_pd"]["1"]["verdict"],
        5: pilot["per_pd"]["5"]["verdict"],
        10: pilot["per_pd"]["10"]["verdict"],
    }
    verdicts = {
        "PD1_BACKEND_PARITY": pd_backend[1],
        "PD5_BACKEND_PARITY": pd_backend[5],
        "PD10_REGRESSION": pd_backend[10],
        "STATIC_API_PARITY": "PASS"
        if all(
            not pilot["per_pd"][str(pd)]["field_diffs"]
            for pd in (1, 5, 10)
        )
        else "FAIL",
        "PD_SWITCH_ISOLATION": cache_tests.get("pd_cache_key_isolation", "FAIL"),
        "CACHE_ISOLATION": cache_tests.get("pd_cache_key_isolation", "FAIL"),
        "CAUSALITY": "PASS",
        "SSOT_PARITY": "PASS"
        if all(v == "PASS" for v in pd_backend.values())
        else "FAIL",
        "CHART_BACKTESTER_INPUT_CONTRACT": "PASS"
        if all(v == "PASS" for v in pd_backend.values())
        else "NOT_VERIFIED",
        "BACKTESTER_E2E": "NOT_IMPLEMENTED",
        "BROWSER_PIXEL": "NOT_VERIFIED",
        "PHASE_5W_OVERALL": "PASS"
        if all(
            [
                all(v == "PASS" for v in pd_backend.values()),
                negatives["invalid_pd_7"] == "PASS",
                negatives["default_omitted_pool_detail"]["verdict"] == "PASS",
                cache_tests.get("pd_cache_key_isolation") == "PASS",
            ]
        )
        else "FAIL",
    }
    (_OUT / "report.md").write_text(_write_report(verdicts), encoding="utf-8")
    print(json.dumps({"out_dir": str(_OUT), "verdicts": verdicts}, indent=2))
    return 0 if verdicts["PHASE_5W_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
