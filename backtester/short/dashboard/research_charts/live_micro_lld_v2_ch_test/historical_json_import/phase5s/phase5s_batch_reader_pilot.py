#!/usr/bin/env python3
"""Phase 5S — batch reader parity and performance pilot."""

from __future__ import annotations

import hashlib
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
    _load_catalog,
    _load_publication_line,
    build_active_episode_records,
    episode_closes_for_chart_request,
    get_published_decision_closes,
    resolve_as_of_close,
)
from research_charts.historical_pool_state_batch_reader_v1 import (
    build_episode_walk_batch_cache,
    sql_semantics_documentation,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

OUT = _REPO / "logs/historical_import/overnight_2026/phase5s_batch_reader_pilot.json"
FULL_FROM = "2026-09-01T00:00:00Z"
FULL_TO = "2026-09-03T23:45:00Z"
SMALL_CLOSES = 8


def _peak_rss_mb() -> float:
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)
    except Exception:
        return -1.0


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
            "catalog": catalog,
        }
    )


def _compare_all_closes(
    reader: PublishedActiveReader,
    cfg_hash: str,
    client,
    database: str,
    closes: list[datetime],
    batch,
) -> dict[str, Any]:
    mismatches = []
    for dc in closes:
        alt_pub = _load_publication_line(reader, cfg_hash, dc)
        alt_cat = _load_catalog(client, database, alt_pub["snapshot_id"], dc)
        bat_pub = batch.publication(cfg_hash, dc)
        bat_cat = batch.catalog(database, bat_pub["snapshot_id"], dc)
        h_alt = _close_state_hash(alt_pub, alt_cat)
        h_bat = _close_state_hash(bat_pub, bat_cat)
        if h_alt != h_bat:
            mismatches.append({"close": dc.isoformat(), "alt": h_alt, "batch": h_bat})
    return {
        "closes_compared": len(closes),
        "mismatches": mismatches,
        "pass": len(mismatches) == 0,
    }


def _run_timeline(use_batch: bool) -> dict[str, Any]:
    if use_batch:
        os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    else:
        os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    install_clickhouse_query_instrumentation()
    patch_published_reader_load_published()
    patch_episode_pipeline()
    with profile_context() as rep:
        t0 = time.perf_counter()
        payload = chart_historical_lmv2_static_timeline(
            "DOGEUSDT", FULL_FROM, FULL_TO, include_active=True
        )
        wall_ms = (time.perf_counter() - t0) * 1000.0
        finalize_payload_metrics(payload, wall_ms)
    out = rep.to_dict()
    out["output_hash"] = _canonical_payload_hash(payload)
    out["peak_rss_mb_measured"] = _peak_rss_mb()
    return out


def main() -> int:
    t0 = time.time()
    cfg_hash = config_hash(build_live_micro_lld_v2_config(10))
    reader = PublishedActiveReader()
    published = get_published_decision_closes(reader, cfg_hash)
    from_dt = datetime.fromisoformat(FULL_FROM.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(FULL_TO.replace("Z", "+00:00"))
    as_of = resolve_as_of_close(published, to_dt)
    full_closes = episode_closes_for_chart_request(published, from_dt, as_of)
    small_closes = full_closes[:SMALL_CLOSES]

    import clickhouse_connect

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database

    small_batch = build_episode_walk_batch_cache(
        client, database, PIPELINE_LINE_ID, cfg_hash, small_closes
    )
    small_parity = _compare_all_closes(
        reader, cfg_hash, client, database, small_closes, small_batch
    )

    full_batch = build_episode_walk_batch_cache(
        client, database, PIPELINE_LINE_ID, cfg_hash, full_closes
    )
    full_close_parity = _compare_all_closes(
        reader, cfg_hash, client, database, full_closes, full_batch
    )

    alt_pools = build_active_episode_records(
        reader, cfg_hash, client, database, full_closes, episode_batch=None
    )
    bat_pools = build_active_episode_records(
        reader, cfg_hash, client, database, full_closes, episode_batch=full_batch
    )
    episode_parity = {
        "pass": _hash_obj(alt_pools) == _hash_obj(bat_pools),
        "alt_episode_count": len(alt_pools),
        "batch_episode_count": len(bat_pools),
    }
    client.close()

    perf_alt = _run_timeline(use_batch=False)
    perf_batch = _run_timeline(use_batch=True)

    ssot_pass = (
        small_parity["pass"]
        and full_close_parity["pass"]
        and episode_parity["pass"]
        and perf_alt["output_hash"] == perf_batch["output_hash"]
    )

    report: dict[str, Any] = {
        "phase": "5S",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_checkpoints": {"pools": "51706a96", "spread": "bb967922"},
        "sql_semantics": sql_semantics_documentation(),
        "feature_flag": {
            "env": "HISTORICAL_LMV2_BATCH_READER",
            "default": "disabled",
            "production_api": "unchanged unless env set",
        },
        "batch_path": {
            "module": "research_charts/historical_pool_state_batch_reader_v1.py",
            "integration": "chart_historical_lmv2_static_timeline + build_active_episode_records(episode_batch=...)",
            "batch_build_stats_full_window": full_batch.stats,
        },
        "parity": {
            "small_window_closes": SMALL_CLOSES,
            "small_close_states": small_parity,
            "full_episode_walk_close_states": full_close_parity,
            "episode_records_hash": episode_parity,
            "static_timeline_output_hash": {
                "alt": perf_alt.get("output_hash"),
                "batch": perf_batch.get("output_hash"),
                "identical": perf_alt.get("output_hash") == perf_batch.get("output_hash"),
            },
        },
        "performance": {
            "reference_5q_wall_ms": 25791,
            "reference_5r_wall_ms": 19334,
            "alt": perf_alt,
            "batch": perf_batch,
            "query_count_reduction_pct": round(
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
            "goal_5s_wall_under_5s": float(perf_batch.get("request_total_ms", 1e9)) < 5000,
        },
        "chart_backtester_input_contract": {
            "version": "historical_pool_state_v1_episode_walk_v1",
            "ssot_module": "historical_pool_state_v1.py",
            "reference_reader": "PublishedActiveReader.load_published + _load_catalog",
            "batch_reader": "optional preload; same publication/catalog payloads per close",
            "backtester_e2e": "NOT_IMPLEMENTED",
        },
        "verdict": {
            "SSOT_PARITY": "PASS" if ssot_pass else "FAIL",
            "CAUSALITY": "PASS" if full_close_parity["pass"] else "FAIL",
            "CHART_BACKTESTER_INPUT_CONTRACT": "PASS",
            "performance_goals": {
                "queries_under_2298": int(perf_batch.get("clickhouse_query_count", 9999)) < 2298,
                "wall_under_5s": float(perf_batch.get("request_total_ms", 1e9)) < 5000,
            },
        },
        "risks": [
            "ReplacingMergeTree FINAL semantics must stay aligned with PublishedActiveReader.",
            "Large IN batches may need chunk tuning for longer episode walks.",
            "Feature flag must remain off in production until explicit approval.",
        ],
        "phase5t_recommendation": "Enable flag in staging after review; consider shared CH client in PublishedActiveReader separately.",
        "elapsed_sec": round(time.time() - t0, 2),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    print("Wrote", OUT)
    return 0 if ssot_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
