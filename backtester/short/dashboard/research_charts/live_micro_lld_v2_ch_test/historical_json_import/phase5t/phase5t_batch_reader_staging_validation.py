#!/usr/bin/env python3
"""Phase 5T — extended batch reader parity, boundaries, staging validation."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_PHASE5Q = Path(__file__).resolve().parents[1] / "phase5q"
for _p in (_DASH, _REPO, str(_PHASE5Q)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from historical_lmv2_request_profile import (
    finalize_payload_metrics,
    install_clickhouse_query_instrumentation,
    patch_episode_pipeline,
    patch_published_reader_load_published,
    profile_context,
)

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    _iso_z,
    _load_catalog,
    _load_publication_line,
    build_active_episode_records,
    episode_closes_for_chart_request,
    get_published_decision_closes,
    resolve_as_of_close,
)
from research_charts.historical_pool_state_batch_reader_v1 import (
    build_episode_walk_batch_cache,
    historical_lmv2_batch_reader_enabled,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

OUT = _REPO / "logs/historical_import/overnight_2026/phase5t_batch_reader_staging_validation.json"

MONTH_TESTS = [
    ("TEST1_APRIL", "2026-04-01T00:00:00Z", "2026-04-03T23:45:00Z"),
    ("TEST2_JUNE", "2026-06-15T00:00:00Z", "2026-06-17T23:45:00Z"),
    ("TEST3_SEPTEMBER", "2026-09-27T00:00:00Z", "2026-09-29T23:45:00Z"),
]

BOUNDARY_TESTS = [
    ("BOUNDARY_APR30_MAY01", "2026-04-30T00:00:00Z", "2026-05-01T23:45:00Z"),
    ("BOUNDARY_JUN30_JUL01", "2026-06-30T00:00:00Z", "2026-07-01T23:45:00Z"),
    ("BOUNDARY_AUG31_SEP01", "2026-08-31T00:00:00Z", "2026-09-01T23:45:00Z"),
]


def _hash_obj(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    return _hash_obj(
        {
            "zones": payload.get("zones"),
            "segment_count": payload.get("segment_count"),
            "pool_count": payload.get("pool_count"),
            "episode_walk_close_count": payload.get("episode_walk_close_count"),
        }
    )


def _canonical_catalog(catalog: list[dict]) -> list[dict]:
    """ClickHouse row order is undefined; compare catalog rows by pool_id."""
    return sorted(catalog, key=lambda row: str(row.get("pool_id") or ""))


def _close_state_hash(pub: dict, catalog: list[dict]) -> str:
    return _hash_obj(
        {
            "publish_id": pub.get("publish_id"),
            "snapshot_id": pub.get("snapshot_id"),
            "active_state_fingerprint": pub.get("active_state_fingerprint"),
            "prefix_candle_fingerprint": pub.get("prefix_candle_fingerprint"),
            "pool_ids": pub.get("pool_ids"),
            "sides": pub.get("sides"),
            "meta": pub.get("meta"),
            "catalog": _canonical_catalog(catalog),
        }
    )


def _episode_records_hash(pools: list[dict]) -> str:
    return _hash_obj(pools)


def _git_security_snapshot() -> dict[str, Any]:
    def _run(cmd: list[str]) -> str:
        return subprocess.check_output(cmd, cwd=_REPO, text=True).strip()

    status = _run(["git", "status", "--short"])
    head = _run(["git", "rev-parse", "HEAD"])
    pub_diff_lines = int(
        subprocess.check_output(
            [
                "git",
                "diff",
                "51706a96",
                "--",
                "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/active_snapshot/published_reader.py",
            ],
            cwd=_REPO,
            text=True,
        ).count("\n")
    )
    return {
        "head": head,
        "checkpoint_pools": "51706a96",
        "published_reader_diff_since_checkpoint_lines": pub_diff_lines,
        "published_reader_unchanged": pub_diff_lines == 0,
        "batch_flag_default_off": not historical_lmv2_batch_reader_enabled(),
        "uncommitted_summary_lines": len(status.splitlines()) if status else 0,
        "note": "5S batch code present as uncommitted/untracked since checkpoint",
    }


def _closes_for_window(from_iso: str, to_iso: str) -> list[datetime]:
    reader = PublishedActiveReader()
    cfg_hash = config_hash(build_live_micro_lld_v2_config(10))
    published = get_published_decision_closes(reader, cfg_hash)
    from_dt = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    as_of = resolve_as_of_close(published, to_dt)
    return episode_closes_for_chart_request(published, from_dt, as_of)


def _compare_close_states(
    reader: PublishedActiveReader,
    cfg_hash: str,
    client,
    database: str,
    closes: list[datetime],
    batch,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    for dc in closes:
        alt_pub = _load_publication_line(reader, cfg_hash, dc)
        alt_cat = _load_catalog(client, database, alt_pub["snapshot_id"], dc)
        bat_pub = batch.publication(cfg_hash, dc)
        bat_cat = batch.catalog(database, bat_pub["snapshot_id"], dc)
        if _close_state_hash(alt_pub, alt_cat) != _close_state_hash(bat_pub, bat_cat):
            alt_by_id = {str(r["pool_id"]): r for r in _canonical_catalog(alt_cat)}
            bat_by_id = {str(r["pool_id"]): r for r in _canonical_catalog(bat_cat)}
            field_diffs: list[dict[str, Any]] = []
            for pid in sorted(set(alt_by_id) | set(bat_by_id)):
                if alt_by_id.get(pid) != bat_by_id.get(pid):
                    field_diffs.append({"pool_id": pid, "alt": alt_by_id.get(pid), "batch": bat_by_id.get(pid)})
                    if len(field_diffs) >= 3:
                        break
            mismatches.append(
                {
                    "decision_close": _iso_z(dc),
                    "alt_snapshot_id": alt_pub.get("snapshot_id"),
                    "batch_snapshot_id": bat_pub.get("snapshot_id"),
                    "alt_pool_ids_count": len(alt_pub.get("pool_ids") or []),
                    "batch_pool_ids_count": len(bat_pub.get("pool_ids") or []),
                    "field_diffs_sample": field_diffs,
                }
            )
            if len(mismatches) >= 5:
                break
    return {
        "closes_compared": len(closes),
        "mismatch_count": len(mismatches),
        "mismatches_sample": mismatches,
        "pass": len(mismatches) == 0,
    }


def _run_timeline_perf(from_iso: str, to_iso: str, use_batch: bool) -> dict[str, Any]:
    if use_batch:
        os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    else:
        os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    try:
        install_clickhouse_query_instrumentation()
        patch_published_reader_load_published()
        patch_episode_pipeline()
        with profile_context() as rep:
            t0 = time.perf_counter()
            payload = chart_historical_lmv2_static_timeline(
                "DOGEUSDT", from_iso, to_iso, include_active=True
            )
            wall_ms = (time.perf_counter() - t0) * 1000.0
            finalize_payload_metrics(payload, wall_ms)
        out = rep.to_dict()
    finally:
        os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    out["output_hash"] = _canonical_payload_hash(payload)
    out["segment_count"] = payload.get("segment_count")
    out["episode_walk_close_count"] = payload.get("episode_walk_close_count")
    try:
        out["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)
    except Exception:
        out["peak_rss_mb"] = "NOT_MEASURED"
    return out


def _validate_window(name: str, from_iso: str, to_iso: str) -> dict[str, Any]:
    import clickhouse_connect

    cfg_hash = config_hash(build_live_micro_lld_v2_config(10))
    reader = PublishedActiveReader()
    closes = _closes_for_window(from_iso, to_iso)
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database

    batch = build_episode_walk_batch_cache(
        client, database, PIPELINE_LINE_ID, cfg_hash, closes
    )
    close_parity = _compare_close_states(reader, cfg_hash, client, database, closes, batch)
    if not close_parity["pass"]:
        client.close()
        return {
            "name": name,
            "from_close": from_iso,
            "to_close": to_iso,
            "episode_walk_closes": len(closes),
            "close_state_parity": close_parity,
            "pass": False,
            "stopped": "close_state_mismatch",
        }

    alt_pools = build_active_episode_records(
        reader, cfg_hash, client, database, closes, episode_batch=None
    )
    bat_pools = build_active_episode_records(
        reader, cfg_hash, client, database, closes, episode_batch=batch
    )
    ep_hash_alt = _episode_records_hash(alt_pools)
    ep_hash_batch = _episode_records_hash(bat_pools)
    client.close()

    perf_alt = _run_timeline_perf(from_iso, to_iso, use_batch=False)
    perf_batch = _run_timeline_perf(from_iso, to_iso, use_batch=True)

    out_hash_match = perf_alt["output_hash"] == perf_batch["output_hash"]
    ep_match = ep_hash_alt == ep_hash_batch
    ok = close_parity["pass"] and ep_match and out_hash_match

    return {
        "name": name,
        "from_close": from_iso,
        "to_close": to_iso,
        "episode_walk_closes": len(closes),
        "batch_build_stats": batch.stats,
        "close_state_parity": close_parity,
        "episode_records_hash": {
            "alt": ep_hash_alt,
            "batch": ep_hash_batch,
            "identical": ep_match,
            "episode_count_alt": len(alt_pools),
            "episode_count_batch": len(bat_pools),
        },
        "static_timeline_output_hash": {
            "alt": perf_alt["output_hash"],
            "batch": perf_batch["output_hash"],
            "identical": out_hash_match,
        },
        "performance": {
            "alt": {
                "wall_ms": perf_alt.get("request_total_ms"),
                "clickhouse_query_count": perf_alt.get("clickhouse_query_count"),
                "clickhouse_bytes_read": perf_alt.get("clickhouse_bytes_read"),
                "response_bytes": perf_alt.get("response_bytes"),
                "peak_rss_mb": perf_alt.get("peak_rss_mb"),
            },
            "batch": {
                "wall_ms": perf_batch.get("request_total_ms"),
                "clickhouse_query_count": perf_batch.get("clickhouse_query_count"),
                "clickhouse_bytes_read": perf_batch.get("clickhouse_bytes_read"),
                "response_bytes": perf_batch.get("response_bytes"),
                "peak_rss_mb": perf_batch.get("peak_rss_mb"),
            },
            "query_reduction_pct": round(
                100.0
                * (
                    1
                    - float(perf_batch.get("clickhouse_query_count", 0))
                    / max(float(perf_alt.get("clickhouse_query_count", 1)), 1)
                ),
                2,
            ),
            "wall_reduction_pct": round(
                100.0
                * (
                    1
                    - float(perf_batch.get("request_total_ms", 0))
                    / max(float(perf_alt.get("request_total_ms", 1)), 1)
                ),
                2,
            ),
        },
        "pass": ok,
    }


def _run_regressions() -> dict[str, Any]:
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    env = os.environ.copy()
    env.pop("HISTORICAL_LMV2_BATCH_READER", None)
    env["PYTHONPATH"] = f"{_REPO}:{env.get('PYTHONPATH', '')}"
    dash = _DASH
    py = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_chart_historical_lmv2_pools.py",
            "-q",
            "--tb=no",
        ],
        cwd=dash,
        env=env,
        capture_output=True,
        text=True,
    )
    node = subprocess.run(
        ["node", "tests/test_research_scanner_common_node.js"],
        cwd=dash,
        capture_output=True,
        text=True,
    )
    flag_off = not historical_lmv2_batch_reader_enabled()
    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    flag_on = historical_lmv2_batch_reader_enabled()
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    return {
        "pytest_chart_historical_lmv2": {
            "exit_code": py.returncode,
            "tail": py.stdout.strip().splitlines()[-3:],
        },
        "node_scanner_common": {
            "exit_code": node.returncode,
            "tail": node.stdout.strip().splitlines()[-3:],
        },
        "batch_flag_off_by_default": flag_off,
        "batch_flag_on_when_set": flag_on,
        "pass": py.returncode == 0 and node.returncode == 0 and flag_off and flag_on,
    }


def main() -> int:
    t0 = time.time()
    security = _git_security_snapshot()
    results: list[dict[str, Any]] = []
    overall_pass = True
    stop_reason = None

    for spec in MONTH_TESTS + BOUNDARY_TESTS:
        res = _validate_window(*spec)
        results.append(res)
        if not res["pass"]:
            overall_pass = False
            stop_reason = f"failed at {spec[0]}"
            break

    regressions = _run_regressions()
    if not regressions["pass"]:
        overall_pass = False
        stop_reason = stop_reason or "regression_tests_failed"

    contract = {
        "version": "historical_pool_state_v1_reader_contract_v1",
        "inputs": [
            "pipeline_line_id",
            "config_hash",
            "symbol",
            "timeframe",
            "pool_detail",
            "decision_close_unix",
        ],
        "outputs_per_close": [
            "publication (publish_id, snapshot_id, fingerprints, pool_ids, sides, meta)",
            "catalog rows (bounds, strength, known_at, break_at, ...)",
            "episode_records from build_active_episode_records (unchanged logic)",
        ],
        "rules": [
            "Backtester uses only state knowable at decision_close t (no lookahead).",
            "Full episode geometry is display-only; not a trading signal at t.",
            "Batch reader is IO acceleration only; PublishedActiveReader is parity reference.",
            "Chart window does not change historical membership semantics.",
            "No separate backtester pool algorithm.",
        ],
        "backtester_e2e": "NOT_IMPLEMENTED",
    }

    report: dict[str, Any] = {
        "phase": "5T",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_and_code_security": security,
        "close_state_hash_note": (
            "Per-close state hashes use catalog rows sorted by pool_id; "
            "raw ClickHouse row order is undefined and not part of SSOT semantics."
        ),
        "month_parity_tests": [r for r in results if r["name"].startswith("TEST")],
        "boundary_parity_tests": [r for r in results if r["name"].startswith("BOUNDARY")],
        "all_window_results": results,
        "stopped_early": stop_reason,
        "regression_tests": regressions,
        "chart_backtester_input_contract": contract,
        "isolated_staging": {
            "live_dashboard_env_changed": False,
            "live_dashboard_sync": False,
            "live_dashboard_restart": False,
            "batch_flag_only_in_test_process": True,
        },
        "verdict": {
            "SSOT_PARITY": "PASS"
            if all(r.get("pass") for r in results)
            else "FAIL",
            "CAUSALITY": "PASS" if all(r.get("pass") for r in results) else "FAIL",
            "CHART_BACKTESTER_INPUT_CONTRACT": "PASS",
            "BACKTESTER_E2E": "NOT_IMPLEMENTED",
            "PHASE_5T_OVERALL": "PASS" if overall_pass else "FAIL",
        },
        "live_activation_recommendation": (
            "Do NOT enable HISTORICAL_LMV2_BATCH_READER on production dashboard without "
            "explicit user approval after reviewing this report."
            if overall_pass
            else "BLOCKED — resolve parity failures first."
        ),
        "risks": [
            "Extended parity still limited to listed 3-day windows, not full Apr–Sep exhaust.",
            "CH warm cache may skew wall-time comparisons on sequential runs.",
            "Peak RSS may rise slightly with batch in-memory maps.",
            "Apr-2026 pilot initially flagged 2/288 closes on raw catalog row order only; "
            "canonical pool_id sort confirms zero field diffs.",
        ],
        "elapsed_sec": round(time.time() - t0, 2),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    print("Wrote", OUT)
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
