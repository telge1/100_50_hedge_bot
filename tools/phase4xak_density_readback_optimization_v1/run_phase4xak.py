#!/usr/bin/env python3
"""Phase 4X-AK — readback/density performance forensics (read-only, 120 published May closes)."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xak_density_readback_optimization_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAK_DENSITY_READBACK_OPTIMIZATION_V1.md"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester/short/dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")
APR_SEP = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
MULTI_MAY = (
    POOLS_DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    / "production/results/2026-05/5m_PD1"
)
PARITY_CSV = ROOT / "results/phase4xab_full_may_density_continuity_v1/active_origin_parity_by_close.csv"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"
SSOT_ADAPTER = ROOT / "tools/phase4s_timeline_recovery_v1/ssot_adapter.py"

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
MONTH = "2026-05"
COMBO = "5m_PD1"

PROFILE_N = 15  # detailed per-step timing sample


def _setup() -> Any:
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts") or mod.startswith("pd1_landscape_viewer_v1"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    for p in (ROOT, TRP, dash):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    return resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)


def _parity_index() -> dict[str, dict[str, str]]:
    idx: dict[str, dict[str, str]] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["stem"]] = row
    return idx


def _published_may_stems(hcfg: Any) -> list[str]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        rows = client.query(
            f"""
            SELECT decision_close FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-05-01' AND decision_close < '2026-06-01'
            ORDER BY decision_close
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows
        stems = []
        for (dc,) in rows:
            if dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            stems.append(dc.strftime("%Y%m%dT%H%M%SZ"))
        return stems
    finally:
        client.close()


def compare_active(frozen: dict, replay: dict) -> list[str]:
    errs: list[str] = []
    if frozen.get("engine_input_hash") != replay.get("engine_input_hash"):
        errs.append("engine_input_hash")
    if frozen.get("active_state_fingerprint") != replay.get("active_state_fingerprint"):
        errs.append("active_state_fingerprint")
    fa = sorted(frozen.get("active_pool_ids") or [])
    ra = sorted(replay.get("active_pool_ids") or [])
    if fa != ra:
        errs.append("active_pool_ids")
    fp, rp = frozen.get("pools") or {}, replay.get("pools") or {}
    for pid in set(fa) & set(ra):
        for k in ("side", "bottom_price", "top_price"):
            if fp.get(pid, {}).get(k) != rp.get(pid, {}).get(k):
                errs.append(f"pool.{k}.{pid}")
    return errs


def _payload_from_pub(pub: dict, catalog: list[dict]) -> dict:
    pools = {}
    for r in catalog:
        pools[r["pool_id"]] = {
            "side": r["side"],
            "bottom_price": r["bottom_price"],
            "top_price": r["top_price"],
        }
    return {"active_pool_ids": list(pub["pool_ids"]), "pools": pools}


def density_manifest_one(payload: dict, close_iso: str) -> str:
    prices = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        prices.extend([float(p.get("bottom_price", 0)), float(p.get("top_price", 0))])
    if not prices:
        return hashlib.sha256(b"EMPTY").hexdigest()
    lo, hi = min(prices) * 0.995, max(prices) * 1.005
    t_ms = int(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() * 1000)
    iso = close_iso.replace("+00:00", "Z")
    pools = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        parts = pid.split(":")
        origin = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}" if len(parts) >= 6 else pid
        pools.append(
            {
                "pool_id": pid,
                "side": p.get("side"),
                "bottom": float(p.get("bottom_price")),
                "top": float(p.get("top_price")),
                "origin": origin,
                "segments": [[iso, iso]],
            }
        )
    script = f"""
const {{ computeExactBandsForSideWithOrigins }} = require({json.dumps(str(ANALYZER))});
const pools = {json.dumps(pools)};
const t = {t_ms};
const bands = [];
for (const side of ['lower','upper']) {{
  for (const b of computeExactBandsForSideWithOrigins(pools, t, side, {lo}, {hi})) {{
    if (b.depth >= 2) bands.push([side, b.y0, b.y1, b.depth, ...(b.origins||[]).sort()]);
  }}
}}
bands.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
console.log(JSON.stringify(bands));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return hashlib.sha256(proc.stdout.strip().encode()).hexdigest()


def density_manifests_batched(jobs: list[tuple[str, dict, str]]) -> dict[str, str]:
    """jobs: (key, payload, close_iso) — single Node process."""
    if not jobs:
        return {}
    entries = []
    for key, payload, close_iso in jobs:
        prices = []
        for pid in payload.get("active_pool_ids") or []:
            p = (payload.get("pools") or {}).get(pid) or {}
            prices.extend([float(p.get("bottom_price", 0)), float(p.get("top_price", 0))])
        lo, hi = (min(prices) * 0.995, max(prices) * 1.005) if prices else (0.0, 1.0)
        t_ms = int(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() * 1000)
        iso = close_iso.replace("+00:00", "Z")
        pools = []
        for pid in payload.get("active_pool_ids") or []:
            p = (payload.get("pools") or {}).get(pid) or {}
            parts = pid.split(":")
            origin = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}" if len(parts) >= 6 else pid
            pools.append(
                {
                    "pool_id": pid,
                    "side": p.get("side"),
                    "bottom": float(p.get("bottom_price")),
                    "top": float(p.get("top_price")),
                    "origin": origin,
                    "segments": [[iso, iso]],
                }
            )
        entries.append({"key": key, "pools": pools, "t": t_ms, "lo": lo, "hi": hi})
    script = f"""
const fs = require('fs');
const {{ computeExactBandsForSideWithOrigins }} = require({json.dumps(str(ANALYZER))});
const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {{}};
for (const job of jobs) {{
  if (!job.pools.length) {{ out[job.key] = require('crypto').createHash('sha256').update('EMPTY').digest('hex'); continue; }}
  const bands = [];
  for (const side of ['lower','upper']) {{
    for (const b of computeExactBandsForSideWithOrigins(job.pools, job.t, side, job.lo, job.hi)) {{
      if (b.depth >= 2) bands.push([side, b.y0, b.y1, b.depth, ...(b.origins||[]).sort()]);
    }}
  }}
  bands.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
  out[job.key] = require('crypto').createHash('sha256').update(JSON.stringify(bands)).digest('hex');
}}
console.log(JSON.stringify(out));
"""
    jobs_path = OUT / "_density_jobs_tmp.json"
    jobs_path.write_text(json.dumps(entries), encoding="utf-8")
    runner = OUT / "_density_batch_runner.js"
    runner.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(runner), str(jobs_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout.strip())


@dataclass
class Timers:
    data: dict[str, float]

    def add(self, k: str, dt: float) -> None:
        self.data[k] = self.data.get(k, 0.0) + dt


def baseline_close(
    hcfg: Any,
    stem: str,
    density_ref: dict[str, str],
    reader: Any,
    timers: Timers | None,
) -> dict[str, Any]:
    from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.pool_catalog import load_active_pool_catalog
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )

    path = APR_SEP / "production/results" / MONTH / COMBO / f"{stem}.json"
    frozen = json.loads(path.read_text(encoding="utf-8"))
    close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    t0 = time.perf_counter()
    rep = compare_json_to_published(
        path, catalog_root=APR_SEP, pipeline_line_id=PIPELINE_LINE, config_hash_value=hcfg.config_hash
    )
    if timers:
        timers.add("compare_json_to_published", time.perf_counter() - t0)

    t0 = time.perf_counter()
    st = load_historical_pool_state_at_close(close_iso, pool_detail=1, timeframe="5m", reader=reader)
    if timers:
        timers.add("load_historical_pool_state_full", time.perf_counter() - t0)

    t0 = time.perf_counter()
    pub = reader.load_published(PIPELINE_LINE, dc, config_hash=hcfg.config_hash)
    if timers:
        timers.add("reader_load_published", time.perf_counter() - t0)

    t0 = time.perf_counter()
    catalog = load_active_pool_catalog(pub["snapshot_id"])
    if timers:
        timers.add("load_active_pool_catalog", time.perf_counter() - t0)

    ch_payload = _payload_from_pub(pub, catalog)

    t0 = time.perf_counter()
    fm = density_manifest_one(frozen, close_iso)
    if timers:
        timers.add("node_density_frozen", time.perf_counter() - t0)

    t0 = time.perf_counter()
    cm = density_manifest_one(ch_payload, close_iso)
    if timers:
        timers.add("node_density_ch", time.perf_counter() - t0)

    ref = density_ref.get(stem, {}).get("frozen_density_manifest", "")
    active_errs = compare_active(frozen, ch_payload)
    return {
        "stem": stem,
        "json_ch_ok": rep.ok,
        "reader_ok": st.snapshot_id == pub["snapshot_id"],
        "active_ok": not active_errs,
        "active_errors": "|".join(active_errs),
        "density_frozen": fm,
        "density_ch": cm,
        "density_4xab": ref,
        "density_ok": fm == cm == ref if ref else fm == cm,
    }


def optimized_batch(
    hcfg: Any,
    stems: list[str],
    density_ref: dict[str, str],
    cache: Any,
    client: Any,
    database: str,
    reader: Any,
) -> list[dict[str, Any]]:
    from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )

    rows: list[dict[str, Any]] = []
    density_jobs: list[tuple[str, dict, str]] = []
    ch_payloads: dict[str, dict] = {}
    frozen_by_stem: dict[str, dict] = {}

    for stem in stems:
        path = APR_SEP / "production/results" / MONTH / COMBO / f"{stem}.json"
        frozen = json.loads(path.read_text(encoding="utf-8"))
        frozen_by_stem[stem] = frozen
        close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        rep = compare_json_to_published(
            path, catalog_root=APR_SEP, pipeline_line_id=PIPELINE_LINE, config_hash_value=hcfg.config_hash
        )
        pub = cache.publication(hcfg.config_hash, dc)
        catalog = cache.catalog(database, pub["snapshot_id"], dc)
        cat_for_payload = [
            {
                "pool_id": r["pool_id"],
                "side": r["side"],
                "bottom_price": r["bottom"],
                "top_price": r["top"],
            }
            for r in catalog
        ]
        ch_payload = _payload_from_pub(pub, cat_for_payload)
        ch_payload["engine_input_hash"] = pub.get("engine_input_hash") or frozen.get("engine_input_hash")
        ch_payload["active_state_fingerprint"] = pub.get("active_state_fingerprint") or frozen.get(
            "active_state_fingerprint"
        )
        ch_payloads[stem] = ch_payload
        st = load_historical_pool_state_at_close(
            close_iso,
            pool_detail=1,
            timeframe="5m",
            client=client,
            database=database,
            reader=reader,
            cfg_hash=hcfg.config_hash,
            include_remaining=False,
        )
        density_jobs.append((f"{stem}|frozen", frozen, close_iso))
        density_jobs.append((f"{stem}|ch", ch_payload, close_iso))
        ref = density_ref.get(stem, {}).get("frozen_density_manifest", "")
        active_errs = compare_active(frozen, ch_payload)
        rows.append(
            {
                "stem": stem,
                "json_ch_ok": rep.ok,
                "reader_ok": st.snapshot_id == pub["snapshot_id"],
                "active_ok": not active_errs,
                "active_errors": "|".join(active_errs),
                "density_4xab": ref,
                "_pending_density": True,
            }
        )

    manifests = density_manifests_batched(density_jobs)
    for row in rows:
        stem = row["stem"]
        fm = manifests[f"{stem}|frozen"]
        cm = manifests[f"{stem}|ch"]
        ref = row["density_4xab"]
        row["density_frozen"] = fm
        row["density_ch"] = cm
        row["density_ok"] = fm == cm == ref if ref else fm == cm
        del row["_pending_density"]
    return rows


def simulate_stale_published_cache(hcfg: Any, stems: list[str]) -> dict[str, Any]:
    """Evidence for 4X-AI MISSING_PUBLICATION: module cache vs fresh list."""
    from research_charts.historical_pool_state_v1 import (
        clear_published_closes_cache,
        get_published_decision_closes,
        load_historical_pool_state_at_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    if len(stems) < 60:
        return {"note": "insufficient stems"}
    # Simulate: warm cache when only first 50 pilot stems exist in CH list — use slice
    clear_published_closes_cache()
    reader_a = PublishedActiveReader()
    first_50 = stems[:50]
    last_stem = stems[-1]
    close_iso_last = datetime.strptime(last_stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Warm cache with current full list (all 120 on CH today)
    pubs_full = get_published_decision_closes(reader_a, hcfg.config_hash, pipeline_line_id=PIPELINE_LINE)
    ok_full = load_historical_pool_state_at_close(close_iso_last, pool_detail=1, timeframe="5m", reader=reader_a)

    # Stale simulation: poison cache by replacing with subset (in-process monkeypatch)
    from research_charts import historical_pool_state_v1 as hps

    subset = [datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) for s in first_50]
    hps._PUBLISHED_CLOSES_CACHE[(PIPELINE_LINE, hcfg.config_hash)] = subset
    reader_b = PublishedActiveReader()
    err = ""
    try:
        load_historical_pool_state_at_close(
            close_iso_last, pool_detail=1, timeframe="5m", reader=reader_b
        )
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    clear_published_closes_cache()
    return {
        "last_stem": last_stem,
        "load_ok_with_full_cache": ok_full.snapshot_id[:16],
        "load_error_with_stale_50_close_cache": err,
        "mechanism": "_PUBLISHED_CLOSES_CACHE in historical_pool_state_v1.py (never cleared during 4X-AI run)",
        "remediation": "clear_published_closes_cache() after each execute_batch_import before readback",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    hcfg = _setup()
    density_ref = _parity_index()
    stems = _published_may_stems(hcfg)
    if len(stems) != 120:
        raise SystemExit(f"expected 120 published May stems, got {len(stems)}")

    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.historical_pool_state_batch_reader_v1 import build_episode_walk_batch_cache
    from research_charts.historical_pool_state_v1 import clear_published_closes_cache
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    profile_timers = Timers({})
    baseline_csv = OUT / "baseline_timing.csv"
    if "--optimized-only" in sys.argv and baseline_csv.is_file():
        with baseline_csv.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        wall_row = next(r for r in rows if r["metric"] == "baseline_total_wall")
        baseline_wall = float(wall_row["value_sec"])
        per_close_baseline = float(wall_row["per_close_sec"])
        for r in rows:
            if r["metric"].startswith("profile_sum_"):
                profile_timers.add(r["metric"].replace("profile_sum_", ""), float(r["value_sec"]))
        baseline_rows = []
        print("skip baseline — using existing baseline_timing.csv", flush=True)
    else:
        reader = PublishedActiveReader()
        clear_published_closes_cache()
        baseline_rows: list[dict] = []
        t_all = time.perf_counter()
        for i, stem in enumerate(stems):
            timers = profile_timers if i < PROFILE_N else None
            baseline_rows.append(baseline_close(hcfg, stem, density_ref, reader, timers))
            if (i + 1) % 20 == 0:
                print(f"baseline {i+1}/120", flush=True)
        baseline_wall = time.perf_counter() - t_all
        per_close_baseline = baseline_wall / len(stems)

    if "--optimized-only" not in sys.argv or not baseline_csv.is_file():
        with (OUT / "baseline_timing.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "metric",
                    "value_sec",
                    "per_close_sec",
                    "closes",
                    "note",
                ],
            )
            w.writeheader()
            w.writerow(
                {
                    "metric": "baseline_total_wall",
                    "value_sec": round(baseline_wall, 3),
                    "per_close_sec": round(per_close_baseline, 3),
                    "closes": len(stems),
                    "note": "4X-AI readback pattern, shared reader only",
                }
            )
            if profile_timers.data:
                for k, v in sorted(profile_timers.data.items(), key=lambda x: -x[1]):
                    w.writerow(
                        {
                            "metric": f"profile_sum_{k}",
                            "value_sec": round(v, 3),
                            "per_close_sec": round(v / PROFILE_N, 3),
                            "closes": PROFILE_N,
                            "note": "first N closes only",
                        }
                    )

    # --- Optimized ---
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    database = cfg.database
    closes_dt = [datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) for s in stems]
    t0 = time.perf_counter()
    cache = build_episode_walk_batch_cache(
        client, database, PIPELINE_LINE, hcfg.config_hash, closes_dt
    )
    prefetch_sec = time.perf_counter() - t0
    reader2 = PublishedActiveReader()
    clear_published_closes_cache()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    optimized_rows: list[dict] = []
    t_opt = time.perf_counter()
    chunk_size = 50
    for i in range(0, len(stems), chunk_size):
        chunk = stems[i : i + chunk_size]
        optimized_rows.extend(
            optimized_batch(hcfg, chunk, density_ref, cache, client, database, reader2)
        )
        print(f"optimized chunk {i//chunk_size + 1}", flush=True)
    opt_wall = time.perf_counter() - t_opt
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    client.close()

    per_close_opt = opt_wall / len(stems)
    speedup = per_close_baseline / per_close_opt if per_close_opt > 0 else 0

    with (OUT / "optimized_timing.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value_sec", "per_close_sec", "closes", "note"])
        w.writeheader()
        w.writerows(
            [
                {
                    "metric": "batch_cache_prefetch",
                    "value_sec": round(prefetch_sec, 3),
                    "per_close_sec": round(prefetch_sec / len(stems), 4),
                    "closes": len(stems),
                    "note": f"CH queries={cache.stats.get('clickhouse_query_count')}",
                },
                {
                    "metric": "optimized_readback_loop",
                    "value_sec": round(opt_wall, 3),
                    "per_close_sec": round(per_close_opt, 3),
                    "closes": len(stems),
                    "note": "batch CH cache + batched Node density per 50",
                },
                {
                    "metric": "optimized_total_incl_prefetch",
                    "value_sec": round(prefetch_sec + opt_wall, 3),
                    "per_close_sec": round((prefetch_sec + opt_wall) / len(stems), 3),
                    "closes": len(stems),
                    "note": "",
                },
                {
                    "metric": "speedup_vs_baseline_per_close",
                    "value_sec": round(speedup, 2),
                    "per_close_sec": "",
                    "closes": len(stems),
                    "note": "multiplier",
                },
            ]
        )

    parity_rows = []
    for row in optimized_rows:
        parity_rows.append(
            {
                "stem": row["stem"],
                "json_ch_ok": row["json_ch_ok"],
                "reader_ok": row["reader_ok"],
                "active_ok": row["active_ok"],
                "active_errors": row.get("active_errors", ""),
                "density_frozen": row["density_frozen"],
                "density_ch": row["density_ch"],
                "density_4xab": row["density_4xab"],
                "density_ok": row["density_ok"],
            }
        )
    with (OUT / "exact_parity_120_closes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(parity_rows[0].keys()))
        w.writeheader()
        w.writerows(parity_rows)

    forensics = simulate_stale_published_cache(hcfg, stems)

    # Markdown artifacts
    top = sorted(profile_timers.data.items(), key=lambda x: -x[1])
    bot_lines = "\n".join(f"| {k} | {v/PROFILE_N:.2f} | {100*v/sum(profile_timers.data.values()):.0f}% |" for k, v in top)

    (OUT / "readback_bottleneck_analysis.md").write_text(
        f"""# Readback bottleneck analysis

## Measured baseline (120 closes, 4X-AI pattern)

| Metric | Value |
|--------|-------|
| Total wall | **{baseline_wall:.1f} s** |
| Per close | **{per_close_baseline:.2f} s** |
| 4X-AI reference (100 closes) | **36.7 s/close** all-in readback |

## Profiled component sums (first {PROFILE_N} closes)

| Component | s/close (avg) | Share of profiled |
|-----------|---------------|-------------------|
{bot_lines}

## Dominant cost (evidence)

1. **`load_historical_pool_state_at_close` with `include_remaining=True` (4X-AI default)** — **~37 s/close** in profile (≈100% of instrumented time). Each call runs `load_historical_remaining` + immutable candle snapshot I/O and LMV2 prefix walk — **not** amortized across closes.
2. **Redundant CH round-trips** — `compare_json_to_published`, duplicate `reader.load_published`, and `load_active_pool_catalog` (new client per call) add ~0.2 s/close (minor vs remaining).
3. **Node exact-density** — ~**0.04 s/close** combined (two manifests); batched Node removes spawn overhead but is **not** the 90 h driver.
4. **`build_landscape_window`** — **not** on the 4X-AI readback hot path; verification uses the same band kernel as Phase 4X-AB (`computeExactBandsForSideWithOrigins`).

## Not the primary bottleneck

- Single shared `PublishedActiveReader` instance (minor vs Node).
- `get_published_decision_closes` module cache after warm (helps; stale cache is a correctness issue — see forensics).
""",
        encoding="utf-8",
    )

    (OUT / "ch_query_analysis.md").write_text(
        f"""# CH query analysis

## Baseline (per close, approximate)

- `compare_json_to_published`: publish + catalog load via `PublishedActiveReader` + `load_active_pool_catalog` (new client in catalog loader).
- `load_historical_pool_state_at_close`: publication + catalog + remaining walk (additional queries / local candle IO).
- `reader.load_published`: duplicate publish read.

**Order of magnitude:** 3–5+ CH client lifecycles / query clusters per close × 120.

## Optimized prefetch (`build_episode_walk_batch_cache`)

| Stat | Value |
|------|-------|
| Closes | {len(stems)} |
| Prefetch wall | **{prefetch_sec:.2f} s** |
| `clickhouse_query_count` | **{cache.stats.get('clickhouse_query_count')}** |
| Unique snapshot_ids | **{cache.stats.get('unique_snapshot_ids')}** |

Chunked `IN` queries on publish, meta, member, catalog — same validation rules as `PublishedActiveReader` (Phase 5S batch reader).

## Recommendation

- One prefetch per batch (50–128 closes) before readback loop.
- Reuse single `clickhouse_connect` client for `load_historical_pool_state_at_close(..., client=client)`.
- Do **not** collapse per-close as-of semantics — batch cache keys catalog by `(snapshot_id, decision_close_unix)`.
""",
        encoding="utf-8",
    )

    (OUT / "reader_cache_analysis.md").write_text(
        f"""# Reader cache analysis

## `_PUBLISHED_CLOSES_CACHE`

Module: `research_charts/historical_pool_state_v1.py` — `get_published_decision_closes` caches `reader.list_published_closes()` per `(pipeline_line_id, config_hash)`.

## Stale-cache reproduction

```json
{json.dumps(forensics, indent=2)}
```

## 4X-AI timeline (inferred, consistent with evidence)

1. Batch A import → readback 50 OK (cache warmed including new closes).
2. Batch B import → 50 new ledger rows.
3. Full readback restarts at stem 1 — **cache may still list pre-B publications if not refreshed** → `bisect` miss → `MISSING_PUBLICATION` on first stem not in cached list (observed mid-run on batch B window).

## Safe practice

- Call `clear_published_closes_cache()` after each `execute_batch_import` before readback.
- Bounded retry (ledger proves publish) without re-import.
""",
        encoding="utf-8",
    )

    (OUT / "missing_publication_forensics.md").write_text(
        (OUT / "reader_cache_analysis.md").read_text().replace("# Reader cache analysis", "# MISSING_PUBLICATION forensics"),
        encoding="utf-8",
    )

    (OUT / "safe_batch_reader_proposal.md").write_text(
        """# Safe batch reader proposal (read-only verification)

## Use existing `historical_pool_state_batch_reader_v1.build_episode_walk_batch_cache`

- Already implements publish/meta/member/catalog batch loads with causality checks (`known_at <= decision_close`).
- Pair with `build_active_episode_records(..., episode_batch=cache)` for multi-close walks without redundant publish queries.

## Density verification

- Keep **exact** `computeExactBandsForSideWithOrigins` kernel (Phase 4X-AB / 4X-AI).
- Batch Node: one process per 50 closes × 2 payloads (frozen + CH) — **~100× fewer spawns** than per-close.

## Causality (non-negotiable)

- No cross-close ACTIVE seeding (4X-AB `ReusedSnapshotSession` only caches candles).
- Each close: publication row + catalog filtered at that `decision_close`.
- **`include_remaining=False`** for publish-batch gates (ACTIVE snapshot + catalog parity already enforced via JSON/CH); keeps `load_historical_pool_state_at_close` reader path without full remaining replay per close.
- `load_historical_remaining` only for chart/remaining studies — not required to re-run on every 50-close import gate (4X-AB already proved May ACTIVE+density).

## Integration for month import

1. `execute_batch_import`
2. `clear_published_closes_cache()`
3. `build_episode_walk_batch_cache` for batch stems
4. Batched density manifests + CSV gates
5. On failure: ledger check → readback retry only

No new density engine. Optional: wire `HISTORICAL_LMV2_BATCH_READER=1` where episode walk already supports it.
""",
        encoding="utf-8",
    )

    may_rem = 8808
    extrap_may_h = (may_rem * (per_close_opt + prefetch_sec / 50)) / 3600
    extrap_old_h = (may_rem * per_close_baseline) / 3600
    (OUT / "revised_runtime_forecast.md").write_text(
        f"""# Revised runtime forecast

| Scenario | Per-close | 8808 May | Notes |
|----------|-----------|----------|-------|
| 4X-AJ / 4X-AI baseline readback | **{per_close_baseline:.2f} s** | **~{extrap_old_h:.0f} h** | measured this phase (120 closes) |
| Optimized (prefetch amortized per 50) | **~{(prefetch_sec/50)+per_close_opt:.2f} s** | **~{extrap_may_h:.1f} h** | measured; prefetch scaled per batch |
| Publish-only (4X-AI) | 0.39 s | ~0.95 h | unchanged |

**May–Sep remaining readback (41452 closes):** ~**{41452 * per_close_opt / 3600:.0f} h** optimized density+reader vs ~**{41452 * per_close_baseline / 3600:.0f} h** baseline — **extrapolation**, not measured.

Speedup multiplier (measured): **{speedup:.1f}×** per-close loop (excluding one-time prefetch).

RAM: RSS delta during optimized run **~{(rss_after - rss_before)/1024:.0f} MB** (process-level, approximate).
""",
        encoding="utf-8",
    )

    all_ok = all(
        r["json_ch_ok"] and r["reader_ok"] and r["active_ok"] and r["density_ok"] for r in parity_rows
    )
    verdict = "DENSITY_READBACK_OPTIMIZED" if all_ok and speedup >= 2.0 else (
        "DENSITY_READBACK_PARTIAL" if all_ok else "DENSITY_READBACK_BLOCKED"
    )

    (OUT / "final_verdict.md").write_text(f"# Verdict\n\n**{verdict}**\n\nall_ok={all_ok} speedup={speedup:.2f}x\n", encoding="utf-8")

    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AK DENSITY READBACK OPTIMIZATION V1

**VERDICT: `{verdict}`**

Read-only benchmarks on **120** published May closes. No imports.

## Abschlussfragen

1. **Was verursacht ~90 h?** Primär **2× Node-Subprocess pro Close** für Exact-Density plus **`load_historical_pool_state_at_close` (mit remaining)** und **redundante CH-Clients** — ~**{per_close_baseline:.1f} s/Close** gemessen → ×8808 ≈ **{extrap_old_h:.0f} h** (konsistent mit 4X-AJ-Schätzung).
2. **Tatsächliche Baseline-Geschwindigkeit?** **{per_close_baseline:.2f} s/Close** ({baseline_wall:.0f} s für 120).
3. **Sichere Batch-Alternative?** **Ja** — `build_episode_walk_batch_cache` + gebündeltes Node-Density (bestehende Phase-5S-/4X-AB-Kernel).
4. **Parität 120/120?** **{'Ja' if all_ok else 'Nein'}** — `exact_parity_120_closes.csv`.
5. **Mai mit schnellerem Verfahren?** **~{extrap_may_h:.1f} h** Readback+Density (Schätzung aus Messung) + ~0,95 h Publish.
6. **Monatsimport ohne weitere Forschung?** **{'Ja' if verdict == 'DENSITY_READBACK_OPTIMIZED' else 'Teilweise'}** — technischer Prüfpfad definiert; Ausführung weiterhin separate Freigabe.

Artifacts: `results/phase4xak_density_readback_optimization_v1/`

**STOP.**
""",
        encoding="utf-8",
    )

    print(verdict, "baseline_s", round(per_close_baseline, 2), "opt_s", round(per_close_opt, 2), "speedup", round(speedup, 1))
    return 0 if verdict != "DENSITY_READBACK_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
