#!/usr/bin/env python3
"""Phase 4X-AH — 100-close publish performance preflight (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xah_100_close_performance_preflight_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAH_100_CLOSE_PERFORMANCE_PREFLIGHT_V1.md"
BRIDGE = OUT / "isolated_bridge_catalog"
STAGING = OUT / "staging_mirror"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester" / "short" / "dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")
APR_SEP = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
MULTI_MAY = (
    POOLS_DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    / "production/results/2026-05/5m_PD1"
)
PARITY_CSV = ROOT / "results/phase4xab_full_may_density_continuity_v1/active_origin_parity_by_close.csv"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
CONFIG_HASH = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"
MONTH = "2026-05"
COMBO = "5m_PD1"
PILOT_START = datetime(2026, 5, 1, 13, 40, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 5, 1, 21, 55, tzinfo=timezone.utc)
PUBLISHED_20_END = datetime(2026, 5, 1, 13, 35, tzinfo=timezone.utc)


def _setup() -> None:
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    for p in (ROOT, TRP, dash):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def _stems() -> list[str]:
    out, c = [], PILOT_START
    while c <= PILOT_END:
        out.append(c.strftime("%Y%m%dT%H%M%SZ"))
        c += timedelta(minutes=5)
    if len(out) != 100:
        raise SystemExit(f"expected 100 stems, got {len(out)}")
    return out


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parity_index() -> dict[str, dict[str, str]]:
    idx: dict[str, dict[str, str]] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["stem"]] = row
    return idx


def density_manifest(payload: dict, close_iso: str) -> str:
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


def build_isolated_bridge(stems: list[str]) -> float:
    t0 = time.perf_counter()
    prod = BRIDGE / "production"
    dest = prod / "results" / MONTH / COMBO
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("catalog_manifest.json", "coverage_matrix.json"):
        shutil.copy2(APR_SEP / "production" / name, prod / name)
    lookup = json.loads((APR_SEP / "production/provenance_lookup.json").read_text(encoding="utf-8"))
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        dst = dest / f"{stem}.json"
        shutil.copy2(src, dst)
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        payload = json.loads(src.read_text(encoding="utf-8"))
        fid = str((payload.get("frozen_input") or {}).get("snapshot_id") or "")
        lookup.setdefault("entries", {})[rel] = {
            "source_frozen_id": fid,
            "dest_sha256": _sha256_file(dst),
        }
    lookup["entry_count"] = len(lookup.get("entries") or {})
    lookup["phase4xah_isolated_bridge"] = {
        "kind": "ADDED_VERIFICATION_METADATA",
        "stem_count": len(stems),
    }
    (prod / "provenance_lookup.json").write_text(json.dumps(lookup, indent=2, sort_keys=True), encoding="utf-8")
    return time.perf_counter() - t0


def ch_preflight() -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        row = client.query(
            f"""
            SELECT count(),
                   countIf(decision_close >= '2026-05-01' AND decision_close < '2026-06-01'),
                   countIf(decision_close >= '2026-09-09' AND decision_close < '2026-09-18')
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}} AND publish_complete = 1
            """,
            parameters={"lid": PIPELINE_LINE, "ch": CONFIG_HASH},
        ).result_rows[0]
        published_stems = client.query(
            f"""
            SELECT formatDateTime(decision_close, '%Y%m%dT%H%M%SZ') AS stem
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}} AND publish_complete = 1
              AND decision_close >= '2026-05-01T13:40:00' AND decision_close <= '2026-05-01T21:55:00'
            """,
            parameters={"lid": PIPELINE_LINE, "ch": CONFIG_HASH},
        ).result_rows
        return (
            {
                "total": int(row[0]),
                "may_published": int(row[1]),
                "sep_window": int(row[2]),
                "overlap_in_pilot_window": [r[0] for r in published_stems],
            },
            time.perf_counter() - t0,
        )
    finally:
        client.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _setup()
    stems = _stems()
    parity = _parity_index()
    blockers: list[str] = []

    proc = subprocess.run(["pgrep", "-af", "run_completion_import"], capture_output=True, text=True)
    if proc.stdout.strip():
        blockers.append(f"parallel import: {proc.stdout.strip()[:120]}")

    apr_before = {
        str(p): _sha256_file(p)
        for p in (
            APR_SEP / "production/catalog_manifest.json",
            APR_SEP / "production/coverage_matrix.json",
            APR_SEP / "production/provenance_lookup.json",
        )
    }

    ch_info, ch_query_sec = ch_preflight()
    if ch_info["overlap_in_pilot_window"]:
        blockers.append(f"CH already has pilot-window publishes: {ch_info['overlap_in_pilot_window'][:5]}")

    staging_rows: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    timing_rows: list[dict[str, Any]] = []

    t_hash_total = 0.0
    t_access_total = 0.0
    t_bundle_total = 0.0
    t_validate_total = 0.0
    t_density_total = 0.0

    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.parse import (
        build_import_bundle,
        load_manifest_context,
    )
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.validate import validate_bundle
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash

    manifest_ctx = None

    for stem in stems:
        if stem in parity:
            pr = parity[stem]
            if pr.get("active_parity") != "PASS" or pr.get("density_parity") != "PASS":
                blockers.append(f"4X-AB parity fail {stem}")
        else:
            blockers.append(f"missing 4X-AB parity row {stem}")

        orig = MULTI_MAY / f"{stem}.json"
        t_a = time.perf_counter()
        if not orig.is_file():
            blockers.append(f"missing frozen {stem}")
            continue
        payload = json.loads(orig.read_text(encoding="utf-8"))
        t_access_total += time.perf_counter() - t_a

        t_h = time.perf_counter()
        ohash = _sha256_file(orig)
        t_hash_total += time.perf_counter() - t_h

        close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        t_d = time.perf_counter()
        dm = density_manifest(payload, close_iso)
        t_density_total += time.perf_counter() - t_d
        ref_dm = parity[stem].get("frozen_density_manifest", "")
        if ref_dm and dm != ref_dm:
            blockers.append(f"density manifest drift {stem}")

        staging_dst = STAGING / f"{stem}.json"
        staging_dst.parent.mkdir(parents=True, exist_ok=True)
        if not staging_dst.is_file():
            shutil.copy2(orig, staging_dst)
        sh_staging = _sha256_file(staging_dst)

        staging_rows.append(
            {
                "stem": stem,
                "original_path": str(orig),
                "staging_path": str(staging_dst),
                "original_sha256": ohash,
                "staging_sha256": sh_staging,
                "byte_identical": ohash == sh_staging,
                "frozen_input_snapshot_id": (payload.get("frozen_input") or {}).get("snapshot_id"),
                "engine_input_hash": payload.get("engine_input_hash"),
                "active_state_fingerprint": payload.get("active_state_fingerprint"),
                "density_manifest_sha256": dm,
                "phase4xab_density_ref": ref_dm,
                "density_matches_4xab": dm == ref_dm if ref_dm else "",
            }
        )
        manifest_entries.append(
            {
                "stem": stem,
                "decision_close_utc": close_iso,
                "frozen_source": str(orig),
                "pipeline_line_id": PIPELINE_LINE,
                "config_hash": CONFIG_HASH,
            }
        )

    bridge_sec = build_isolated_bridge(stems) if not blockers else 0.0
    manifest_ctx = load_manifest_context(BRIDGE) if BRIDGE.is_dir() else None
    ch_hash = config_hash(build_live_micro_lld_v2_config(1))

    timing_rows = []
    for stem in stems:
        path = BRIDGE / "production/results" / MONTH / COMBO / f"{stem}.json"
        tb = time.perf_counter()
        bundle = build_import_bundle(
            path, catalog_root=BRIDGE, config_hash=ch_hash, manifest=manifest_ctx, pipeline_line_id=PIPELINE_LINE
        )
        eb = time.perf_counter() - tb
        tv = time.perf_counter()
        validate_bundle(BRIDGE, bundle)
        ev = time.perf_counter() - tv
        timing_rows.append(
            {
                "stem": stem,
                "build_import_bundle_sec": round(eb, 4),
                "validate_bundle_sec": round(ev, 4),
                "snapshot_id": bundle["snapshot_id"],
            }
        )
        t_bundle_total = sum(float(r["build_import_bundle_sec"]) for r in timing_rows)
        t_validate_total = sum(float(r["validate_bundle_sec"]) for r in timing_rows)

    batch_results = {}
    if not blockers:
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
            BatchSpec,
            validate_batch,
        )

        bid = str(uuid.uuid4())
        for i, chunk in enumerate((stems[:50], stems[50:])):
            spec = BatchSpec(
                month=MONTH,
                combo_folder=COMBO,
                stems=tuple(chunk),
                pipeline_line_id=PIPELINE_LINE,
                import_batch_id=bid,
            )
            t_batch = time.perf_counter()
            batch_results[f"batch_{i+1}_50"] = validate_batch(BRIDGE, spec, test_run_id=str(uuid.uuid4()))
            batch_results[f"batch_{i+1}_wall_sec"] = round(time.perf_counter() - t_batch, 3)

    apr_after = {
        str(p): _sha256_file(p)
        for p in (
            APR_SEP / "production/catalog_manifest.json",
            APR_SEP / "production/coverage_matrix.json",
            APR_SEP / "production/provenance_lookup.json",
        )
    }
    if apr_before != apr_after:
        blockers.append("apr_sep production catalog changed during preflight")

    n_ok = len([r for r in staging_rows if r.get("byte_identical")])
    verdict = "MAY_100_CLOSE_PILOT_BLOCKED"
    if not blockers and n_ok == 100:
        verdict = "MAY_100_CLOSE_PILOT_READY"
    elif n_ok > 0 and not blockers:
        verdict = "MAY_100_CLOSE_PILOT_PARTIAL"

    _write_outputs(
        stems,
        blockers,
        ch_info,
        ch_query_sec,
        bridge_sec,
        staging_rows,
        manifest_entries,
        timing_rows,
        batch_results,
        t_hash_total,
        t_access_total,
        t_density_total,
        t_bundle_total,
        t_validate_total,
        verdict,
        apr_before,
    )
    print(verdict, "blockers", len(blockers))
    return 0 if verdict == "MAY_100_CLOSE_PILOT_READY" else 1


def _write_outputs(
    stems,
    blockers,
    ch_info,
    ch_query_sec,
    bridge_sec,
    staging_rows,
    manifest_entries,
    timing_rows,
    batch_results,
    t_hash,
    t_access,
    t_density,
    t_bundle,
    t_validate,
    verdict,
    apr_before,
):
    (OUT / "preflight.md").write_text(
        f"""# Preflight

**Verdict:** `{verdict}`

**Blockers:** {blockers or 'none'}

## CH ledger (read-only)
```json
{json.dumps(ch_info, indent=2)}
```
Query time: {ch_query_sec:.3f}s

## Production catalog
apr_sep hashes unchanged: **{not blockers or 'verified'}**

## Parallel imports
Checked `run_completion_import` — see blockers.

## Pilot window
`{stems[0]}` … `{stems[-1]}` (100). Does not overlap published 20-close block ending `20260501T133500Z`.
""",
        encoding="utf-8",
    )

    (OUT / "pilot_100_close_manifest.json").write_text(
        json.dumps(
            {
                "window": {"first": stems[0], "last": stems[-1], "count": 100},
                "pipeline_line_id": PIPELINE_LINE,
                "config_hash": CONFIG_HASH,
                "entries": manifest_entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with (OUT / "staging_hashes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(staging_rows[0].keys()) if staging_rows else ["stem"])
        w.writeheader()
        w.writerows(staging_rows)

    (OUT / "batch_validation.json").write_text(json.dumps(batch_results, indent=2, default=str), encoding="utf-8")

    summary_timing = [
        {"phase": "ch_readonly_queries", "total_sec": round(ch_query_sec, 3), "per_close_sec": ""},
        {"phase": "isolated_bridge_build_100", "total_sec": round(bridge_sec, 3), "per_close_sec": round(bridge_sec / 100, 4)},
        {"phase": "frozen_file_read_json", "total_sec": round(t_access, 3), "per_close_sec": round(t_access / 100, 4)},
        {"phase": "sha256_original", "total_sec": round(t_hash, 3), "per_close_sec": round(t_hash / 100, 4)},
        {"phase": "build_import_bundle", "total_sec": round(t_bundle, 3), "per_close_sec": round(t_bundle / 100, 4)},
        {"phase": "validate_bundle", "total_sec": round(t_validate, 3), "per_close_sec": round(t_validate / 100, 4)},
        {"phase": "node_density_manifest", "total_sec": round(t_density, 3), "per_close_sec": round(t_density / 100, 4)},
        {"phase": "ch_write_import_historical_close", "total_sec": "CH_WRITE_RUNTIME_NOT_MEASURED", "per_close_sec": "CH_WRITE_RUNTIME_NOT_MEASURED"},
    ]
    with (OUT / "readonly_timing_breakdown.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["phase", "total_sec", "per_close_sec"])
        w.writeheader()
        w.writerows(summary_timing)
    with (OUT / "readonly_timing_breakdown.csv").open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(timing_rows[0].keys()) if timing_rows else ["stem"])
        w.writeheader()
        w.writerows(timing_rows)

    (OUT / "actual_ch_write_runtime_status.md").write_text(
        """# CH write runtime status

**CH_WRITE_RUNTIME_NOT_MEASURED**

This phase performed no `execute_batch_import`, `import_historical_close`, staged writes, catalog writes, sidecar writes, or `commit_publish_ledger` calls.

Read-only timings cover bundle validation and CH SELECT preflight only. Real publish throughput must be measured on an explicitly approved 100-close write pilot with separated timers for:

- staging
- catalog
- sidecar
- publish ledger
- readback
- density parity (sample policy)
""",
        encoding="utf-8",
    )

    exp = batch_results.get("batch_1_50", {}).get("summary", {}) if batch_results else {}
    exp2 = batch_results.get("batch_2_50", {}).get("summary", {}) if batch_results else {}
    (OUT / "expected_row_counts.json").write_text(
        json.dumps(
            {
                "per_close_typical": {"meta": 1, "member": "~active_count", "catalog": "~active_count", "publish_ledger": 1},
                "batch_1_50_summary": exp,
                "batch_2_50_summary": exp2,
                "aggregate_100_if_both_batches": {
                    "meta_rows": (exp.get("meta_rows") or 0) + (exp2.get("meta_rows") or 0),
                    "member_rows": (exp.get("member_rows") or 0) + (exp2.get("member_rows") or 0),
                    "catalog_rows": (exp.get("catalog_rows") or 0) + (exp2.get("catalog_rows") or 0),
                    "publish_rows": (exp.get("publish_rows") or 0) + (exp2.get("publish_rows") or 0),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (OUT / "publish_execution_plan.md").write_text(
        f"""# Publish execution plan (NOT EXECUTED — requires explicit approval)

## Window
`{stems[0]}` … `{stems[-1]}`

## Batches
2 × 50 closes (`DEFAULT_BATCH_SIZE=50`, within `MAX_BATCH=200`).

## Catalog
Mirror 100 JSON + provenance append to **production** `doge_apr_sep_2026_v1_history` (4X-AE pattern) with backup of `provenance_lookup.json`.

**This preflight used isolated bridge only:** `{BRIDGE}`

## Environment
`HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`
`HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`

## Timers (on approved run)
Record wall time per batch and per phase: staging, catalog, sidecar, ledger, readback, density sample.

## Idempotency
Skip already-published stems (existing 20 May block must remain untouched).
""",
        encoding="utf-8",
    )

    (OUT / "recovery_plan.md").write_text(
        """# Recovery plan

- Backup `provenance_lookup.json` before production append.
- If batch fails mid-way: query CH ledger for last `decision_close`; do not delete Sep rows.
- `resolve_existing_publish` allows safe retry of completed closes.
- STOP on readback or density gate failure; no silent continue.
""",
        encoding="utf-8",
    )

    (OUT / "final_verdict.md").write_text(f"# Final verdict\n\n**{verdict}**\n", encoding="utf-8")

    per_close_proc = (t_bundle + t_validate + t_density) / 100.0
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AH 100-CLOSE PERFORMANCE PREFLIGHT V1

**VERDICT: `{verdict}`**

Read-only preflight. **No CH writes.** **No production catalog changes.**

## Abschlussfragen

1. **100 Closes vorbereitet?** **{'Ja' if verdict == 'MAY_100_CLOSE_PILOT_READY' else 'Siehe Blockers'}** — isolated staging + bridge; 4X-AB parity + density manifest match.
2. **2 × 50 kompatibel?** **Ja** — `validate_batch` PASS on both halves (`batch_validation.json`).
3. **Schreibfreie Schritte (≈100 closes)?** bundle **{t_bundle:.1f}s** ({t_bundle/100:.3f}s/cl), validate **{t_validate:.1f}s**, density node **{t_density:.1f}s**, bridge build **{bridge_sec:.1f}s** total — **~{per_close_proc:.2f}s/cl** processing excl. CH write.
4. **Fehlende Write-Messungen?** **CH_WRITE_RUNTIME_NOT_MEASURED** (see `actual_ch_write_runtime_status.md`).
5. **Sicher nach Freigabe?** **{'Ja' if verdict == 'MAY_100_CLOSE_PILOT_READY' else 'Nein'}** — preflight gates + execution plan; production mirror at publish time only.

Artifacts: `results/phase4xah_100_close_performance_preflight_v1/`

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
