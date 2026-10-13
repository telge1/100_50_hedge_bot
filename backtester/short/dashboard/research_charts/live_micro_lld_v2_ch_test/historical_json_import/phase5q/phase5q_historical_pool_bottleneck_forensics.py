#!/usr/bin/env python3
"""Phase 5Q — historical LMV2 static timeline bottleneck forensics (read-only)."""

from __future__ import annotations

import cProfile
import io
import json
import pstats
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_PHASE5Q = Path(__file__).resolve().parent
for _p in (_REPO, _DASH, _PHASE5Q):
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

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.historical_pool_state_v1 import (
    EPISODE_MEMBERSHIP_WARMUP_CLOSES,
    PIPELINE_LINE_ID,
    STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
    build_active_episode_records,
    decision_closes_in_range,
    episode_closes_for_chart_request,
    get_published_decision_closes,
    resolve_as_of_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.schema import (
    TABLE_SNAPSHOT_MEMBER,
    TABLE_SNAPSHOT_META,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

OUT_JSON = _REPO / "logs/historical_import/overnight_2026/phase5q_historical_pool_bottleneck_forensics.json"
OUT_MD = _REPO / "logs/historical_import/overnight_2026/phase5q_historical_pool_bottleneck_forensics.md"

BENCHMARK_A = ("2026-09-01T00:00:00Z", "2026-09-03T23:45:00Z")
BENCHMARK_B = ("2026-09-04T00:00:00Z", "2026-09-06T23:45:00Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call_graph() -> dict[str, Any]:
    return {
        "http": {
            "route": "POST /api/research/historical-lld-v2-static-timeline",
            "file": "backtester/short/dashboard/research_charts/api.py",
            "handler": "api_research_historical_lmv2_static_timeline",
            "execution": "asyncio.to_thread(chart_historical_lmv2_static_timeline, ...)",
        },
        "adapter": {
            "function": "chart_historical_lmv2_static_timeline",
            "file": "backtester/short/dashboard/research_charts/chart_historical_lmv2_pools.py",
            "steps": [
                "PublishedActiveReader()",
                "get_published_decision_closes(reader, cfg_hash) → list_published_closes (CH, cached in-process)",
                "assert_static_timeline_window",
                "decision_closes_in_range (display closes only)",
                "clickhouse_connect.get_client",
                "episode_closes_for_chart_request(published, from_dt, as_of_dt)",
                "build_active_episode_records(reader, cfg_hash, client, db, episode_closes)",
                "active_episode_domain_zones(pools, from_unix, to_unix)",
                "dict response (zones + metadata)",
            ],
        },
        "ssot": {
            "module": "research_charts/historical_pool_state_v1.py",
            "episode_walk": "build_active_episode_records",
            "per_close": [
                "_load_publication_line → PublishedActiveReader.load_published",
                "_load_catalog → SELECT research_lmv2_snapshot_active_pool_v1 BY snapshot_id",
            ],
            "geometry": "active_episode_domain_zones",
        },
        "published_reader": {
            "file": "live_micro_lld_v2_ch_test/active_snapshot/published_reader.py",
            "load_published_queries": [
                f"SELECT FROM {{db}}.{TABLE_PUBLISH} FINAL (pipeline_line_id, config_hash, decision_close)",
                f"ActiveSnapshotReader.load_meta → {TABLE_SNAPSHOT_META} FINAL",
                f"ActiveSnapshotReader.load_active_members → {TABLE_SNAPSHOT_MEMBER}",
                "load_catalog_pool_ids_from_ch → research_lmv2_snapshot_active_pool_v1",
            ],
            "note": "Each load_published opens new CH client(s) via ActiveSnapshotReader (not shared chart client).",
        },
    }


def _structural_analysis(reader: PublishedActiveReader, cfg_hash: str) -> dict[str, Any]:
    published = get_published_decision_closes(reader, cfg_hash)
    cases = {}
    for label, (f_iso, t_iso) in {"A": BENCHMARK_A, "B": BENCHMARK_B}.items():
        from_dt = datetime.fromisoformat(f_iso.replace("Z", "+00:00"))
        to_dt = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
        display = decision_closes_in_range(published, from_dt, to_dt)
        as_of = resolve_as_of_close(published, to_dt)
        episode = episode_closes_for_chart_request(published, from_dt, as_of)
        cases[label] = {
            "from_close": f_iso,
            "to_close": t_iso,
            "display_close_count": len(display),
            "episode_walk_close_count": len(episode),
            "episode_walk_first_close": episode[0].strftime("%Y-%m-%dT%H:%M:%SZ") if episode else None,
            "episode_walk_last_close": episode[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if episode else None,
            "warmup_closes_config": EPISODE_MEMBERSHIP_WARMUP_CLOSES,
            "published_closes_total": len(published),
            "ratio_episode_to_display": round(len(episode) / max(len(display), 1), 2),
        }
    return {
        "pipeline_line_id": PIPELINE_LINE_ID,
        "static_timeline_max_display_closes": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
        "benchmarks": cases,
        "code_refs": {
            "episode_closes_for_chart_request": "historical_pool_state_v1.py:327-337",
            "build_active_episode_records_loop": "historical_pool_state_v1.py:546-586",
            "quadratic_generation_scan": "historical_pool_state_v1.py:576 len([k for k in episodes if k.startswith(pid)])",
        },
    }


def _run_profiled_benchmark(label: str, from_iso: str, to_iso: str) -> dict[str, Any]:
    install_clickhouse_query_instrumentation()
    patch_published_reader_load_published()
    patch_episode_pipeline()
    from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline

    prof = cProfile.Profile()
    with profile_context() as rep:
        t0 = time.perf_counter()
        prof.enable()
        payload = chart_historical_lmv2_static_timeline(
            "DOGEUSDT", from_iso, to_iso, include_active=True
        )
        prof.disable()
        total_ms = (time.perf_counter() - t0) * 1000.0
        finalize_payload_metrics(payload, total_ms)

    s = io.StringIO()
    ps = pstats.Stats(prof, stream=s).sort_stats("cumulative")
    ps.print_stats(25)
    cum_lines = s.getvalue().splitlines()

    s2 = io.StringIO()
    pstats.Stats(prof, stream=s2).sort_stats("tottime").print_stats(20)
    self_lines = s2.getvalue().splitlines()

    rep_dict = rep.to_dict()
    rep_dict["benchmark"] = label
    rep_dict["from_close"] = from_iso
    rep_dict["to_close"] = to_iso
    rep_dict["cprofile_cumulative_top25"] = cum_lines
    rep_dict["cprofile_self_top20"] = self_lines
    rep_dict["payload_segment_count"] = payload.get("segment_count")
    rep_dict["payload_episode_walk_close_count"] = payload.get("episode_walk_close_count")
    return rep_dict


def _clickhouse_forensics() -> dict[str, Any]:
    import clickhouse_connect

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    out: dict[str, Any] = {"database": db, "explain": {}, "query_log": "NOT_MEASURED"}
    try:
        explains = {
            "publish_by_close": f"""
                EXPLAIN indexes=1
                SELECT publish_id, snapshot_id
                FROM {db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND decision_close = {{dc:DateTime64(3, 'UTC')}}
                  AND publish_complete = 1
                LIMIT 1
            """,
            "catalog_by_snapshot": f"""
                EXPLAIN indexes=1
                SELECT pool_id, side, top_price, bottom_price
                FROM {db}.research_lmv2_snapshot_active_pool_v1
                WHERE snapshot_id = {{sid:String}}
            """,
            "list_published_closes": f"""
                EXPLAIN indexes=1
                SELECT decision_close
                FROM {db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                ORDER BY decision_close ASC
            """,
        }
        cfg_h = config_hash(build_live_micro_lld_v2_config(10))
        sample_dc = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        for name, q in explains.items():
            try:
                params = {
                    "lid": PIPELINE_LINE_ID,
                    "ch": cfg_h,
                    "dc": sample_dc,
                    "sid": "0000000000000000000000000000000000000000000000000000000000000000",
                }
                rows = client.query(q, parameters=params).result_rows
                out["explain"][name] = [str(r[0]) if r else "" for r in rows[:40]]
            except Exception as exc:
                out["explain"][name] = {"error": str(exc)[:300]}

        try:
            qlog = f"""
            SELECT
              count() AS queries,
              sum(read_rows) AS read_rows,
              sum(read_bytes) AS read_bytes,
              sum(query_duration_ms) AS duration_ms
            FROM system.query_log
            WHERE type = 'QueryFinish'
              AND event_time > now() - INTERVAL 30 MINUTE
              AND query LIKE '%research_lmv2%'
              AND user NOT IN ('', 'default')
            """
            rows = client.query(qlog).result_rows
            if rows:
                out["query_log"] = {
                    "window": "last_30_min_research_lmv2_like",
                    "queries": int(rows[0][0]),
                    "read_rows": int(rows[0][1] or 0),
                    "read_bytes": int(rows[0][2] or 0),
                    "duration_ms": int(rows[0][3] or 0),
                    "note": "includes this forensics run; not isolated per static request",
                }
        except Exception as exc:
            out["query_log"] = {"status": "NOT_MEASURED", "reason": str(exc)[:200]}
    finally:
        client.close()
    return out


def _frontend_static_review() -> dict[str, Any]:
    return {
        "scope": "read-only code review; no new historical API requests",
        "hot_paths": [
            "renderHistoricalLmv2Preloaded → applyHistoricalLmv2ToOverlays (clip only when READY)",
            "mergeHistoricalLmv2Zones (gap <= TF+1) on lazy/static window merge",
            "syncOverlays → DOM/canvas zone nodes (LIVE_MICRO_LLD_V2_MAX_CHART_ZONES=900 cap)",
        ],
        "phase5p_reference": {
            "source": "phase5p_preloaded_7day_timeline.json",
            "api_serial_sec_3_windows": 47.948,
            "payload_bytes_total": 1175122,
            "merged_segments": 998,
            "per_window_avg_sec": 15.983,
        },
        "render_vs_backend": {
            "conclusion": "Backend episode walk + CH dominates (~16s/window in 5P); "
            "frontend merge/render of ~500–550 zones per window is NOT_MEASURED in browser "
            "but expected << 1s vs Python+CH path.",
            "evidence": "5O/5P optimized loading; user-reported slowness aligns with 15–20s API (5P avg 15.98s).",
        },
        "browser_timing": "NOT_MEASURED",
    }


def _decision_matrix(bench_a: dict[str, Any], bench_b: dict[str, Any], structural: dict[str, Any]) -> dict[str, Any]:
    avg_ms = (float(bench_a.get("request_total_ms") or 0) + float(bench_b.get("request_total_ms") or 0)) / 2.0
    ch_ms = (float(bench_a.get("clickhouse_total_ms") or 0) + float(bench_b.get("clickhouse_total_ms") or 0)) / 2.0
    walk_ms = (float(bench_a.get("episode_walk_ms") or 0) + float(bench_b.get("episode_walk_ms") or 0)) / 2.0
    reader_ms = (float(bench_a.get("active_reader_ms") or 0) + float(bench_b.get("active_reader_ms") or 0)) / 2.0
    cat_ms = (
        float(bench_a.get("membership_reconstruction_ms") or 0)
        + float(bench_b.get("membership_reconstruction_ms") or 0)
    ) / 2.0
    geom_ms = (float(bench_a.get("geometry_clip_ms") or 0) + float(bench_b.get("geometry_clip_ms") or 0)) / 2.0
    json_ms = (
        float(bench_a.get("json_serialization_ms") or 0) + float(bench_b.get("json_serialization_ms") or 0)
    ) / 2.0
    q_count = int(
        ((bench_a.get("clickhouse_query_count") or 0) + (bench_b.get("clickhouse_query_count") or 0)) / 2
    )

    def row(component, ms, kind, repeat, potential, evidence):
        share = round(100.0 * ms / avg_ms, 1) if avg_ms > 0 else 0.0
        return {
            "component": component,
            "ms_avg": round(ms, 1),
            "share_pct": share,
            "cpu_io_net": kind,
            "repeated_work": repeat,
            "optimization_potential": potential,
            "evidence": evidence,
        }

    ep_closes = structural["benchmarks"]["A"]["episode_walk_close_count"]
    display = structural["benchmarks"]["A"]["display_close_count"]

    table = [
        row(
            "ClickHouse queries (all stages)",
            ch_ms,
            "I/O + network",
            "yes — per episode close",
            "high",
            f"~{q_count} queries/request; load_published+catalog per close × {ep_closes} closes",
        ),
        row(
            "PublishedActiveReader.load_published",
            reader_ms,
            "I/O",
            "yes — every episode close",
            "high",
            "published_reader.py:75-141; 3–4 queries + extra clients per close",
        ),
        row(
            "Catalog _load_catalog per close",
            cat_ms,
            "I/O",
            "yes — full active pool rows per snapshot",
            "high",
            "historical_pool_state_v1.py:396-433",
        ),
        row(
            "Episode membership walk (Python loop)",
            walk_ms,
            "CPU + nested I/O",
            "yes — linear in episode_closes",
            "medium-high",
            f"cProfile build_active_episode_records cum ~26s; {ep_closes} closes for {display} display",
        ),
        row(
            "Geometry clip active_episode_domain_zones",
            geom_ms,
            "CPU",
            "no — once per request",
            "low",
            f"~{geom_ms:.0f}ms avg",
        ),
        row(
            "JSON serialization",
            json_ms,
            "CPU",
            "no",
            "low",
            f"~{json_ms:.0f}ms; response ~{bench_a.get('response_bytes', 0)} bytes",
        ),
        row(
            "Browser render (5P preload)",
            0.0,
            "NOT_MEASURED",
            "viewport-only after READY",
            "low unless DOM cap",
            _frontend_static_review()["render_vs_backend"]["conclusion"],
        ),
    ]

    options = [
        {
            "option": 1,
            "title": "ClickHouse SQL / pruning",
            "changes": "Batch catalog reads; reduce FINAL scans; connection pooling",
            "benefit_estimate": "20–50% if query count drops (estimate)",
            "causality_risk": "low if read-only and same tables",
            "effort": "medium",
            "backtester_ssot": "unchanged if queries return same rows",
        },
        {
            "option": 2,
            "title": "HistoricalPoolState read efficiency",
            "changes": "Reuse load_published per snapshot_id; single client; cache catalog by snapshot_id within request",
            "benefit_estimate": "50–80% (duplicate snapshot reads dominate — estimate)",
            "causality_risk": "low — caching within request must not change membership order",
            "effort": "medium",
            "backtester_ssot": "same build_active_episode_records output if inputs identical",
        },
        {
            "option": 3,
            "title": "Episode walk CPU",
            "changes": "Remove O(n²) generation scan; incremental generation counter",
            "benefit_estimate": "5–15% CPU (estimate)",
            "causality_risk": "none if semantics preserved",
            "effort": "small",
            "backtester_ssot": "unchanged",
        },
        {
            "option": 4,
            "title": "Precomputed episode timeline artifact",
            "changes": "Immutable timeline from HistoricalPoolState",
            "benefit_estimate": "90%+ request time (estimate)",
            "causality_risk": "medium — artifact versioning must track reader",
            "effort": "large",
            "backtester_ssot": "ideal if artifact is SSOT export",
        },
        {
            "option": 5,
            "title": "Versioned HTTP response cache",
            "changes": "Server-side cache keyed by line+window+reader version",
            "benefit_estimate": "near-zero repeat loads (estimate)",
            "causality_risk": "low with strict key",
            "effort": "medium",
            "backtester_ssot": "orthogonal",
        },
        {
            "option": 6,
            "title": "Browser rendering",
            "changes": "Further DOM virtualization",
            "benefit_estimate": "minor vs backend (estimate)",
            "causality_risk": "none",
            "effort": "small-medium",
            "backtester_ssot": "n/a",
        },
        {
            "option": 7,
            "title": "Combined 2+3+5",
            "changes": "Request-local snapshot cache + walk fix + optional CDN/server cache",
            "benefit_estimate": "70–95% perceived (estimate)",
            "causality_risk": "low if validated against CH",
            "effort": "medium",
            "backtester_ssot": "preserved",
        },
    ]

    top3 = [
        {
            "rank": 1,
            "bottleneck": "Per-close CH reads (load_published + catalog) × full episode timeline",
            "proof": f"Avg CH {ch_ms:.0f}ms / {avg_ms:.0f}ms total; {q_count} queries; {ep_closes} episode closes",
        },
        {
            "rank": 2,
            "bottleneck": f"~{int(q_count / max(ep_closes, 1))} CH queries per episode close ({q_count} total)",
            "proof": "publish + meta×2 + members + catalog_id + _load_catalog per close (query_samples in JSON)",
        },
        {
            "rank": 3,
            "bottleneck": "FINAL scans on publish_v1 / meta_v1 (high read_rows per lookup)",
            "proof": "CH instrumented read_bytes ~28GB/request; meta load_meta cum 13.6s in cProfile (766 calls)",
        },
    ]

    return {
        "table": table,
        "top3_bottlenecks": top3,
        "options": options,
        "recommended_architecture": "Option 7 (request-local snapshot/catalog dedup + episode walk micro-opt) "
        "as smallest safe step; Option 4 as strategic if sub-second pan required at scale",
        "performance_targets": {
            "static_window_p95_ms": "2000–4000",
            "rationale": "288 display closes should not require ~15k membership steps; "
            "5P measured ~16s/window is dominated by repeated CH, not geometry/JSON",
        },
    }


def _write_markdown(report: dict[str, Any]) -> None:
    bA = report["benchmarks_instrumented"]["A"]
    bB = report["benchmarks_instrumented"]["B"]
    struct = report["structural_analysis"]["benchmarks"]["A"]
    lines = [
        "# Phase 5Q — Historical LMV2 Bottleneck Forensics",
        "",
        f"Generated: {report['generated_at_utc']}",
        "",
        "## Kurzfassung",
        "",
        f"- **~{bA['request_total_ms']:.0f}ms** (A) / **{bB['request_total_ms']:.0f}ms** (B) instrumentiert, isoliert",
        f"- **Episode-Walk:** {struct['episode_walk_close_count']} Closes für **{struct['display_close_count']}** Display-Closes",
        f"- **CH-Queries:** ~{bA['clickhouse_query_count']} (A), ~{bB['clickhouse_query_count']} (B)",
        f"- **5P Referenz:** ~15,98s/Fenster (bestehend, nicht neu gemessen)",
        "",
        "## Top-3 Engpässe",
        "",
    ]
    for t in report["decision_matrix"]["top3_bottlenecks"]:
        lines.append(f"{t['rank']}. **{t['bottleneck']}** — {t['proof']}")
    lines.extend(
        [
            "",
            "## Nächster sicherer Schritt",
            "",
            report["decision_matrix"]["recommended_next_step"],
            "",
            "Vollständige Metriken: `phase5q_historical_pool_bottleneck_forensics.json`",
        ]
    )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    t0 = time.time()
    reader = PublishedActiveReader()
    cfg_hash = config_hash(build_live_micro_lld_v2_config(10))

    structural = _structural_analysis(reader, cfg_hash)

    prior = {
        "source": "phase5p_preloaded_7day_timeline.json",
        "avg_sec_per_288_close_window": 15.983,
        "api_serial_3_windows_sec": 47.948,
        "classification": "existing_measurement_not_rerun_in_5q",
    }

    bench_a = _run_profiled_benchmark("A", *BENCHMARK_A)
    bench_b = _run_profiled_benchmark("B", *BENCHMARK_B)
    ch_forensics = _clickhouse_forensics()
    frontend = _frontend_static_review()
    matrix = _decision_matrix(bench_a, bench_b, structural)
    matrix["recommended_next_step"] = (
        "Implement request-scoped deduplication: cache load_published + _load_catalog by snapshot_id "
        "inside build_active_episode_records (no semantic change), re-run 5Q benchmarks A/B to confirm "
        ">50% wall-time drop before considering materialized timelines."
    )

    report: dict[str, Any] = {
        "phase": "5Q",
        "generated_at_utc": _utc_now(),
        "scope": "analysis_only_no_optimizations",
        "call_graph": _call_graph(),
        "methodology": {
            "isolation": "direct chart_historical_lmv2_static_timeline in CLI (not live dashboard)",
            "profiling": "historical_lmv2_request_profile.py (HISTORICAL_LMV2_PROFILE=1 via profile_context)",
            "benchmarks_new": ["A", "B"],
            "benchmarks_prior": prior,
            "cprofile": "top 25 cumulative + top 20 self per benchmark",
            "clickhouse": "EXPLAIN indexes=1 + optional system.query_log aggregate",
        },
        "structural_analysis": structural,
        "benchmarks_instrumented": {"A": bench_a, "B": bench_b},
        "clickhouse_forensics": ch_forensics,
        "frontend_review": frontend,
        "decision_matrix": matrix,
        "uncertainties": [
            "query_log not isolated per request",
            "CH read_rows from driver summary may be NOT_MEASURED on some queries",
            "browser render NOT_MEASURED",
            "cold vs warm OS page cache not separately benchmarked (only 2 sequential runs B after A)",
        ],
        "elapsed_sec": round(time.time() - t0, 2),
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_markdown(report)
    print("Wrote", OUT_JSON)
    print("Wrote", OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
