#!/usr/bin/env python3
"""Phase 4X-AI — controlled 100-close May publish + performance benchmark."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xai_100_close_publish_performance_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAI_100_CLOSE_PUBLISH_PERFORMANCE_V1.md"

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
MONTH = "2026-05"
COMBO = "5m_PD1"
PILOT_START = datetime(2026, 5, 1, 13, 40, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 5, 1, 21, 55, tzinfo=timezone.utc)
SEP_PROBE = ("20260909T120000Z", "20260915T120000Z", "20260917T235500Z")


def _setup() -> Any:
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
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    return resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)


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


def _parity_density() -> dict[str, str]:
    idx: dict[str, str] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["stem"]] = row.get("frozen_density_manifest") or ""
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


def ch_stats(hcfg: Any) -> dict[str, Any]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        row = client.query(
            f"""
            SELECT count(), min(decision_close), max(decision_close),
                   countIf(decision_close >= '2026-05-01' AND decision_close < '2026-06-01'),
                   countIf(decision_close >= '2026-09-09' AND decision_close < '2026-09-18'),
                   countIf(decision_close >= '2026-05-01T13:40:00' AND decision_close <= '2026-05-01T21:55:00')
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}} AND publish_complete = 1
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows[0]
        return {
            "total": int(row[0]),
            "min_close": str(row[1]),
            "max_close": str(row[2]),
            "may_count": int(row[3]),
            "sep_window": int(row[4]),
            "pilot_window_count": int(row[5]),
        }
    finally:
        client.close()


def mirror_to_apr_sep(stems: list[str], backup_dir: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    backup_dir.mkdir(parents=True, exist_ok=True)
    lookup_path = APR_SEP / "production/provenance_lookup.json"
    shutil.copy2(lookup_path, backup_dir / "provenance_lookup.json.bak")
    dest = APR_SEP / "production/results" / MONTH / COMBO
    dest.mkdir(parents=True, exist_ok=True)
    lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        dst = dest / f"{stem}.json"
        if not dst.is_file():
            shutil.copy2(src, dst)
        elif _sha256_file(src) != _sha256_file(dst):
            raise RuntimeError(f"hash conflict {stem}")
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        fid = str(json.loads(src.read_text(encoding="utf-8")).get("frozen_input", {}).get("snapshot_id") or "")
        lookup.setdefault("entries", {})[rel] = {"source_frozen_id": fid, "dest_sha256": _sha256_file(dst)}
    lookup["entry_count"] = len(lookup.get("entries") or {})
    lookup.setdefault("phase4xai_append", {})["stems"] = stems
    lookup.setdefault("phase4xai_append", {})["at_utc"] = datetime.now(timezone.utc).isoformat()
    lookup_path.write_text(json.dumps(lookup, indent=2, sort_keys=True), encoding="utf-8")
    return {"mirror_sec": round(time.perf_counter() - t0, 3), "stems": len(stems)}


def run_batch(hcfg: Any, stems: list[str], batch_id: str, test_run_id: str) -> tuple[list[dict], float, int]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import BatchSpec
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import execute_batch_import

    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_WRITE"] = "1"
    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"] = "1"
    spec = BatchSpec(
        month=MONTH,
        combo_folder=COMBO,
        stems=tuple(stems),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id=batch_id,
    )
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    outcomes = execute_batch_import(APR_SEP, spec, test_run_id=test_run_id)
    wall = time.perf_counter() - t0
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return outcomes, wall, rss_after - rss_before


def readback_stems(hcfg: Any, stems: list[str], density_ref: dict[str, str]) -> tuple[list[dict], float]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )
    from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.pool_catalog import (
        load_active_pool_catalog,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()
    rows: list[dict] = []
    t0 = time.perf_counter()
    for stem in stems:
        path = APR_SEP / "production/results" / MONTH / COMBO / f"{stem}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rep = compare_json_to_published(
            path, catalog_root=APR_SEP, pipeline_line_id=PIPELINE_LINE, config_hash_value=hcfg.config_hash
        )
        dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        st = load_historical_pool_state_at_close(close_iso, pool_detail=1, timeframe="5m")
        pub = reader.load_published(PIPELINE_LINE, dc, config_hash=hcfg.config_hash)
        catalog = load_active_pool_catalog(pub["snapshot_id"])
        ch_payload = {
            "active_pool_ids": list(pub["pool_ids"]),
            "pools": {
                r["pool_id"]: {
                    "side": r["side"],
                    "bottom_price": r["bottom_price"],
                    "top_price": r["top_price"],
                }
                for r in catalog
            },
        }
        fm = density_manifest(payload, close_iso)
        cm = density_manifest(ch_payload, close_iso)
        ref = density_ref.get(stem, "")
        rows.append(
            {
                "stem": stem,
                "json_ch_ok": rep.ok,
                "reader_ok": st.snapshot_id == pub["snapshot_id"],
                "engine_input_hash": payload.get("engine_input_hash"),
                "engine_input_hash_ch": pub.get("engine_input_hash"),
                "fingerprint_frozen": payload.get("active_state_fingerprint"),
                "fingerprint_ch": st.active_state_fingerprint,
                "snapshot_id": pub["snapshot_id"],
                "active_count": len(pub["pool_ids"]),
                "json_ch_errors": "|".join(rep.mismatches) if not rep.ok else "",
                "density_frozen": fm,
                "density_ch": cm,
                "density_4xab": ref,
                "density_ok": fm == cm and (not ref or fm == ref),
            }
        )
    return rows, time.perf_counter() - t0


def preflight(hcfg: Any, stems: list[str], density_ref: dict[str, str]) -> tuple[bool, dict]:
    blockers: list[str] = []
    if subprocess.run(["pgrep", "-af", "run_completion_import"], capture_output=True).stdout.strip():
        blockers.append("run_completion_import running")
    du = shutil.disk_usage(APR_SEP)
    if du.free < 2 * 1024**3:
        blockers.append(f"disk free < 2GiB: {du.free}")
    ch = ch_stats(hcfg)
    if ch["pilot_window_count"] > 0:
        blockers.append(f"pilot window already has {ch['pilot_window_count']} publishes")
    immutable_before = {
        "catalog_manifest": _sha256_file(APR_SEP / "production/catalog_manifest.json"),
        "coverage_matrix": _sha256_file(APR_SEP / "production/coverage_matrix.json"),
    }
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        if not src.is_file():
            blockers.append(f"missing {stem}")
        if stem not in density_ref:
            blockers.append(f"no 4xab row {stem}")
    sep_probe = {}
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )

    for s in SEP_PROBE:
        p = APR_SEP / "production/results/2026-09/5m_PD1" / f"{s}.json"
        if p.is_file():
            r = compare_json_to_published(
                p, catalog_root=APR_SEP, pipeline_line_id=PIPELINE_LINE, config_hash_value=hcfg.config_hash
            )
            sep_probe[s] = r.ok
    return len(blockers) == 0, {
        "blockers": blockers,
        "ch": ch,
        "immutable_before": immutable_before,
        "sep_probe": sep_probe,
        "disk_free_gb": round(du.free / 1024**3, 2),
    }


def _load_preflight_pre() -> dict:
    text = (OUT / "preflight.md").read_text(encoding="utf-8")
    start = text.index("```json\n") + len("```json\n")
    end = text.index("\n```", start)
    return json.loads(text[start:end])


def resume_after_publish() -> int:
    """Complete readback/report when import batches finished but full readback crashed."""
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("batch_a_result.json", "batch_b_result.json", "preflight.md"):
        if not (OUT / name).is_file():
            raise SystemExit(f"missing {name} for resume")
    hcfg = _setup()
    stems = _stems()
    density_ref = _parity_density()
    pre = _load_preflight_pre()
    ba = json.loads((OUT / "batch_a_result.json").read_text(encoding="utf-8"))
    bb = json.loads((OUT / "batch_b_result.json").read_text(encoding="utf-8"))
    wall_a, wall_b = float(ba["wall_sec"]), float(bb["wall_sec"])
    timing: list[dict] = [{"phase": "resume_after_publish", "batch": "n/a", "sec": 0}]
    rb_all, rb_sec = readback_stems(hcfg, stems, density_ref)
    timing.append({"phase": "readback_density_reader", "batch": "ALL", "sec": round(rb_sec, 3)})
    ch_after = ch_stats(hcfg)
    immutable_after = {
        "catalog_manifest": _sha256_file(APR_SEP / "production/catalog_manifest.json"),
        "coverage_matrix": _sha256_file(APR_SEP / "production/coverage_matrix.json"),
    }
    with (OUT / "timing_breakdown.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["phase", "batch", "sec", "per_close_sec", "rss_delta_kb"], extrasaction="ignore")
        w.writeheader()
        w.writerows(timing)
    _write_csv(OUT / "ch_readback.csv", rb_all)
    _write_csv(
        OUT / "exact_density_parity.csv",
        [{k: r[k] for k in ("stem", "density_frozen", "density_ch", "density_4xab", "density_ok")} for r in rb_all],
    )
    _write_csv(
        OUT / "resource_measurements.csv",
        [
            {"metric": "batch_a_wall_sec", "value": wall_a},
            {"metric": "batch_b_wall_sec", "value": wall_b},
            {"metric": "readback_all_sec", "value": rb_sec},
        ],
    )
    publish_wall = wall_a + wall_b
    per_close = publish_wall / 100
    may_delta = ch_after["may_count"] - pre["ch"]["may_count"]
    remaining_may = 8928 - ch_after["may_count"]
    extrap_may_h = remaining_may * per_close / 3600
    extrap_all_h = 41452 * per_close / 3600
    (OUT / "throughput_forecast.md").write_text(
        f"""# Throughput forecast (EXTRAPOLATION)

Measured publish wall (batch A+B import only): **{publish_wall:.1f} s** for **100** closes → **{per_close:.2f} s/close**.

Readback+density for 100 closes: **{rb_sec:.1f} s** (not included in publish rate).

| Metric | Value |
|--------|-------|
| Throughput (publish only) | **{3600/per_close:.0f} closes/hour** (extrapolation) |
| Remaining May closes | **{remaining_may}** → **~{extrap_may_h:.1f} h** publish-only |
| Remaining May–Sep (41452 baseline) | **~{extrap_all_h:.1f} h** publish-only |

**NOT_VERIFIED** for other months until Jun–Sep density gates complete. Batch/month effects may differ.
""",
        encoding="utf-8",
    )
    (OUT / "september_non_interference.md").write_text(
        f"""# September non-interference

| Check | Before | After |
|-------|--------|-------|
| sep_window publishes | {pre['ch']['sep_window']} | {ch_after['sep_window']} |
| catalog_manifest hash | unchanged | {immutable_after['catalog_manifest'] == pre['immutable_before']['catalog_manifest']} |
| coverage_matrix hash | unchanged | {immutable_after['coverage_matrix'] == pre['immutable_before']['coverage_matrix']} |
| sep_probe readback | {pre['sep_probe']} | (re-check if needed) |

May pilot window publishes: **{ch_after['pilot_window_count']}** (expected 100).
May delta this run: **{may_delta}**.
""",
        encoding="utf-8",
    )
    all_ok = (
        may_delta == 100
        and ch_after["pilot_window_count"] == 100
        and all(r["json_ch_ok"] and r["reader_ok"] and r["density_ok"] for r in rb_all)
        and ch_after["sep_window"] == pre["ch"]["sep_window"]
        and immutable_after == pre["immutable_before"]
    )
    verdict = "MAY_100_CLOSE_PUBLISH_PERFORMANCE_PASS" if all_ok else "MAY_100_CLOSE_PUBLISH_PARTIAL"
    if may_delta < 100 and not all(r["json_ch_ok"] for r in rb_all):
        verdict = "MAY_100_CLOSE_PUBLISH_FAILED"
    _finalize(
        verdict,
        pre,
        {"A": ba["outcomes"], "B": bb["outcomes"]},
        ch_after,
        rb_all,
        timing,
        ch_after,
        per_close,
        publish_wall,
    )
    print(verdict, "may_delta", may_delta, "per_close_sec", round(per_close, 2))
    return 0 if verdict == "MAY_100_CLOSE_PUBLISH_PERFORMANCE_PASS" else 1


def main() -> int:
    if "--resume-after-publish" in sys.argv:
        return resume_after_publish()
    OUT.mkdir(parents=True, exist_ok=True)
    hcfg = _setup()
    stems = _stems()
    batch_a, batch_b = stems[:50], stems[50:]
    density_ref = _parity_density()
    ok, pre = preflight(hcfg, stems, density_ref)
    (OUT / "preflight.md").write_text(
        f"# Preflight\n\nOK={ok}\n\n```json\n{json.dumps(pre, indent=2)}\n```\n", encoding="utf-8"
    )
    if not ok:
        _finalize("MAY_100_CLOSE_PUBLISH_FAILED", pre, {}, {}, [], [], None)
        return 1

    backup = OUT / "apr_sep_backup"
    mirror_meta = mirror_to_apr_sep(stems, backup)
    batch_a_id = str(uuid.uuid4())
    batch_b_id = str(uuid.uuid4())
    run_a_id = str(uuid.uuid4())
    run_b_id = str(uuid.uuid4())

    timing: list[dict] = []
    timing.append({"phase": "catalog_mirror_100", "batch": "all", "sec": mirror_meta["mirror_sec"]})

    outcomes_a, wall_a, drss_a = run_batch(hcfg, batch_a, batch_a_id, run_a_id)
    timing.append(
        {
            "phase": "execute_batch_import_total",
            "batch": "A",
            "sec": round(wall_a, 3),
            "per_close_sec": round(wall_a / 50, 3),
            "rss_delta_kb": drss_a,
        }
    )
    (OUT / "batch_a_result.json").write_text(
        json.dumps({"import_batch_id": batch_a_id, "test_run_id": run_a_id, "outcomes": outcomes_a, "wall_sec": wall_a}, indent=2, default=str),
        encoding="utf-8",
    )

    rb_a, rb_a_sec = readback_stems(hcfg, batch_a, density_ref)
    timing.append({"phase": "readback_density_reader", "batch": "A", "sec": round(rb_a_sec, 3)})
    if not all(r["json_ch_ok"] and r["reader_ok"] and r["density_ok"] for r in rb_a):
        _finalize("MAY_100_CLOSE_PUBLISH_FAILED", pre, {"A": outcomes_a}, {}, rb_a, timing, ch_stats(hcfg))
        return 1

    ch_mid = ch_stats(hcfg)
    if ch_mid["sep_window"] != pre["ch"]["sep_window"]:
        _finalize("MAY_100_CLOSE_PUBLISH_FAILED", pre, {"A": outcomes_a}, {}, rb_a, timing, ch_mid)
        return 1

    outcomes_b, wall_b, drss_b = run_batch(hcfg, batch_b, batch_b_id, run_b_id)
    timing.append(
        {
            "phase": "execute_batch_import_total",
            "batch": "B",
            "sec": round(wall_b, 3),
            "per_close_sec": round(wall_b / 50, 3),
            "rss_delta_kb": drss_b,
        }
    )
    (OUT / "batch_b_result.json").write_text(
        json.dumps({"import_batch_id": batch_b_id, "test_run_id": run_b_id, "outcomes": outcomes_b, "wall_sec": wall_b}, indent=2, default=str),
        encoding="utf-8",
    )

    rb_all, rb_sec = readback_stems(hcfg, stems, density_ref)
    timing.append({"phase": "readback_density_reader", "batch": "ALL", "sec": round(rb_sec, 3)})

    ch_after = ch_stats(hcfg)
    immutable_after = {
        "catalog_manifest": _sha256_file(APR_SEP / "production/catalog_manifest.json"),
        "coverage_matrix": _sha256_file(APR_SEP / "production/coverage_matrix.json"),
    }

    with (OUT / "timing_breakdown.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["phase", "batch", "sec", "per_close_sec", "rss_delta_kb"], extrasaction="ignore")
        w.writeheader()
        w.writerows(timing)

    _write_csv(OUT / "ch_readback.csv", rb_all)
    _write_csv(
        OUT / "exact_density_parity.csv",
        [{k: r[k] for k in ("stem", "density_frozen", "density_ch", "density_4xab", "density_ok")} for r in rb_all],
    )
    _write_csv(
        OUT / "resource_measurements.csv",
        [
            {"metric": "batch_a_wall_sec", "value": wall_a},
            {"metric": "batch_b_wall_sec", "value": wall_b},
            {"metric": "batch_a_rss_delta_kb", "value": drss_a},
            {"metric": "batch_b_rss_delta_kb", "value": drss_b},
            {"metric": "readback_all_sec", "value": rb_sec},
        ],
    )

    publish_wall = wall_a + wall_b
    per_close = publish_wall / 100
    may_delta = ch_after["may_count"] - pre["ch"]["may_count"]
    remaining_may = 8928 - ch_after["may_count"]
    extrap_may_h = remaining_may * per_close / 3600
    extrap_all_h = 41452 * per_close / 3600

    (OUT / "throughput_forecast.md").write_text(
        f"""# Throughput forecast (EXTRAPOLATION)

Measured publish wall (batch A+B import only): **{publish_wall:.1f} s** for **100** closes → **{per_close:.2f} s/close**.

Readback+density for 100 closes: **{rb_sec:.1f} s** (not included in publish rate).

| Metric | Value |
|--------|-------|
| Throughput (publish only) | **{3600/per_close:.0f} closes/hour** (extrapolation) |
| Remaining May closes | **{remaining_may}** → **~{extrap_may_h:.1f} h** publish-only |
| Remaining May–Sep (41452 baseline) | **~{extrap_all_h:.1f} h** publish-only |

**NOT_VERIFIED** for other months until Jun–Sep density gates complete. Batch/month effects may differ.
""",
        encoding="utf-8",
    )

    (OUT / "september_non_interference.md").write_text(
        f"""# September non-interference

| Check | Before | After |
|-------|--------|-------|
| sep_window publishes | {pre['ch']['sep_window']} | {ch_after['sep_window']} |
| catalog_manifest hash | unchanged | {immutable_after['catalog_manifest'] == pre['immutable_before']['catalog_manifest']} |
| coverage_matrix hash | unchanged | {immutable_after['coverage_matrix'] == pre['immutable_before']['coverage_matrix']} |
| sep_probe readback | {pre['sep_probe']} | (re-check if needed) |

May pilot window publishes: **{ch_after['pilot_window_count']}** (expected 100).
May delta this run: **{may_delta}**.
""",
        encoding="utf-8",
    )

    all_ok = (
        may_delta == 100
        and all(r["json_ch_ok"] and r["reader_ok"] and r["density_ok"] for r in rb_all)
        and ch_after["sep_window"] == pre["ch"]["sep_window"]
        and immutable_after == pre["immutable_before"]
    )
    verdict = "MAY_100_CLOSE_PUBLISH_PERFORMANCE_PASS" if all_ok else "MAY_100_CLOSE_PUBLISH_PARTIAL"
    if may_delta < 100 and not all(r["json_ch_ok"] for r in rb_all):
        verdict = "MAY_100_CLOSE_PUBLISH_FAILED"

    _finalize(verdict, pre, {"A": outcomes_a, "B": outcomes_b}, ch_after, rb_all, timing, ch_after, per_close, publish_wall)
    print(verdict, "may_delta", may_delta, "per_close_sec", round(per_close, 2))
    return 0 if verdict == "MAY_100_CLOSE_PUBLISH_PERFORMANCE_PASS" else 1


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _finalize(verdict, pre, batches, ch_after, rb, timing, ch_final, per_close=None, publish_wall=None):
    (OUT / "final_verdict.md").write_text(f"# Final verdict\n\n**{verdict}**\n", encoding="utf-8")
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AI 100-CLOSE PUBLISH & PERFORMANCE

**VERDICT: `{verdict}`**

## Summary
- Window: `20260501T134000Z` … `20260501T215500Z`
- Publish wall (2×50): **{publish_wall or 'n/a'}** s; **{per_close or 'n/a'}** s/close (import path)
- Parity: see `ch_readback.csv`, `exact_density_parity.csv`

Artifacts: `results/phase4xai_100_close_publish_performance_v1/`

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
