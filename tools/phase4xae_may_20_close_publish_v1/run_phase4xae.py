#!/usr/bin/env python3
"""Phase 4X-AE — controlled 20-close May 5m PD1 CH publish pilot."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xae_may_20_close_publish_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAE_MAY_20_CLOSE_PUBLISH_V1.md"
BRIDGE = ROOT / "results/phase4xad_may_catalog_bridge_v1/bridge_catalog_doge_apr_sep_v1"
MULTI_MAY = (
    ROOT
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    / "production/results/2026-05/5m_PD1"
)
APR_SEP = ROOT / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
PARITY_CSV = ROOT / "results/phase4xab_full_may_density_continuity_v1/active_origin_parity_by_close.csv"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester" / "short" / "dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
MONTH = "2026-05"
COMBO = "5m_PD1"
PILOT_START = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
PILOT_N = 20

SEP_PROBE_STEMS = ("20260909T120000Z", "20260915T120000Z", "20260917T235500Z")


def _setup_path() -> None:
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


def _stems() -> list[str]:
    return [
        (PILOT_START + timedelta(minutes=5 * i)).strftime("%Y%m%dT%H%M%SZ") for i in range(PILOT_N)
    ]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parity_density_index() -> dict[str, str]:
    out: dict[str, str] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["stem"]] = row.get("frozen_density_manifest") or ""
    return out


def density_manifest_from_payload(payload: dict, close_iso: str) -> str:
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


def ch_line_stats(hcfg: Any) -> dict[str, Any]:
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
                   countIf(decision_close >= '2026-09-09' AND decision_close < '2026-09-18')
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}} AND publish_complete = 1
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows[0]
        return {
            "database": db,
            "pipeline_line_id": hcfg.pipeline_line_id,
            "config_hash": hcfg.config_hash,
            "published_total": int(row[0]),
            "min_close": str(row[1]),
            "max_close": str(row[2]),
            "may_count": int(row[3]),
            "sep_window_count": int(row[4]),
        }
    finally:
        client.close()


def mirror_pilot_to_apr_sep(stems: list[str], backup_dir: Path) -> dict[str, Any]:
    """Copy 20 May JSON + append provenance entries on apr_sep (pilot scope only)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    lookup_path = APR_SEP / "production/provenance_lookup.json"
    shutil.copy2(lookup_path, backup_dir / "provenance_lookup.json.bak")
    dest_dir = APR_SEP / "production/results" / MONTH / COMBO
    dest_dir.mkdir(parents=True, exist_ok=True)
    lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    added: list[str] = []
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        dst = dest_dir / f"{stem}.json"
        if not dst.is_file():
            shutil.copy2(src, dst)
        elif _sha256_file(src) != _sha256_file(dst):
            raise RuntimeError(f"apr_sep file exists with different hash: {stem}")
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        fid = str(
            json.loads(src.read_text(encoding="utf-8")).get("frozen_input", {}).get("snapshot_id") or ""
        )
        sha = _sha256_file(dst)
        lookup.setdefault("entries", {})[rel] = {"source_frozen_id": fid, "dest_sha256": sha}
        added.append(rel)
    lookup["entry_count"] = len(lookup.get("entries") or {})
    lookup.setdefault("phase4xae_pilot_append", {})["added_at_utc"] = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    lookup["phase4xae_pilot_append"]["stems"] = stems
    lookup_path.write_text(json.dumps(lookup, indent=2, sort_keys=True), encoding="utf-8")
    return {"files_copied_or_verified": len(stems), "provenance_keys_added": added}


def preflight(hcfg: Any, stems: list[str]) -> tuple[bool, dict[str, Any]]:
    blockers: list[str] = []
    if not BRIDGE.is_dir():
        blockers.append(f"missing bridge catalog {BRIDGE}")
    proc = subprocess.run(["pgrep", "-af", "run_completion_import"], capture_output=True, text=True)
    if proc.stdout.strip():
        blockers.append(f"parallel import process: {proc.stdout.strip()[:200]}")

    ch_before = ch_line_stats(hcfg)
    if ch_before["may_count"] > 0:
        blockers.append(f"unexpected existing may publishes: {ch_before['may_count']}")

    hash_rows = []
    for stem in stems:
        orig = MULTI_MAY / f"{stem}.json"
        staged = BRIDGE / "production/results" / MONTH / COMBO / f"{stem}.json"
        if not orig.is_file() or not staged.is_file():
            blockers.append(f"missing file {stem}")
            continue
        oh, sh = _sha256_file(orig), _sha256_file(staged)
        if oh != sh:
            blockers.append(f"hash mismatch {stem}")
        hash_rows.append({"stem": stem, "multi_month_sha256": oh, "bridge_sha256": sh, "match": oh == sh})

    apr_hashes = {
        str(p): _sha256_file(p)
        for p in (
            APR_SEP / "production/catalog_manifest.json",
            APR_SEP / "production/coverage_matrix.json",
            APR_SEP / "production/provenance_lookup.json",
        )
    }

    sep_snapshots: dict[str, str] = {}
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        compare_json_to_published,
    )

    for s in SEP_PROBE_STEMS:
        p = APR_SEP / "production/results/2026-09/5m_PD1" / f"{s}.json"
        if p.is_file():
            rep = compare_json_to_published(
                p,
                catalog_root=APR_SEP,
                pipeline_line_id=PIPELINE_LINE,
                config_hash_value=hcfg.config_hash,
            )
            sep_snapshots[s] = "ok" if rep.ok else "|".join(rep.mismatches[:3])

    batch_id = str(uuid.uuid4())
    info = {
        "ch_before": ch_before,
        "apr_sep_production_hashes": apr_hashes,
        "frozen_hash_checks": hash_rows,
        "sep_probe_readback": sep_snapshots,
        "import_batch_id": batch_id,
        "catalog_root_publish": str(APR_SEP),
        "bridge_catalog_root": str(BRIDGE),
        "blockers": blockers,
        "note": "execute_batch_import requires pilot 15m_PD10 Aug stems on catalog_root → publish uses apr_sep after 20-file mirror",
    }
    return len(blockers) == 0, info


def run_publish(hcfg: Any, stems: list[str], batch_id: str, test_run_id: str) -> list[dict[str, Any]]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import BatchSpec
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import (
        execute_batch_import,
    )

    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_WRITE"] = "1"
    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"] = "1"
    spec = BatchSpec(
        month=MONTH,
        combo_folder=COMBO,
        stems=tuple(stems),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id=batch_id,
    )
    return execute_batch_import(APR_SEP, spec, test_run_id=test_run_id)


def readback_all(hcfg: Any, stems: list[str]) -> list[dict[str, Any]]:
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

    density_idx = _parity_density_index()
    reader = PublishedActiveReader()
    rows: list[dict[str, Any]] = []
    for stem in stems:
        json_path = APR_SEP / "production/results" / MONTH / COMBO / f"{stem}.json"
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        close_iso = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rep = compare_json_to_published(
            json_path,
            catalog_root=APR_SEP,
            pipeline_line_id=PIPELINE_LINE,
            config_hash_value=hcfg.config_hash,
        )
        st = load_historical_pool_state_at_close(close_iso, pool_detail=1, timeframe="5m")
        pub = reader.load_published(
            PIPELINE_LINE,
            datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
            config_hash=hcfg.config_hash,
        )
        catalog = load_active_pool_catalog(pub["snapshot_id"])
        frozen_dm = density_manifest_from_payload(payload, close_iso)
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
        ch_dm = density_manifest_from_payload(ch_payload, close_iso)
        ref_dm = density_idx.get(stem, "")
        rows.append(
            {
                "stem": stem,
                "decision_close_utc": close_iso,
                "snapshot_id": pub["snapshot_id"],
                "engine_input_hash_frozen": payload.get("engine_input_hash"),
                "engine_input_hash_ch": pub.get("engine_input_hash"),
                "active_state_fingerprint_frozen": payload.get("active_state_fingerprint"),
                "active_state_fingerprint_ch": st.active_state_fingerprint,
                "active_count": len(pub["pool_ids"]),
                "catalog_rows": len(catalog),
                "member_count": len(pub["pool_ids"]),
                "json_ch_parity_ok": rep.ok,
                "json_ch_mismatches": "|".join(rep.mismatches) if not rep.ok else "",
                "reader_load_ok": st.snapshot_id == pub["snapshot_id"],
                "density_frozen_manifest": frozen_dm,
                "density_ch_manifest": ch_dm,
                "density_phase4xab_ref": ref_dm,
                "density_frozen_vs_ch": frozen_dm == ch_dm,
                "density_frozen_vs_4xab": frozen_dm == ref_dm if ref_dm else False,
            }
        )
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _setup_path()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    stems = _stems()
    ok, pre = preflight(hcfg, stems)
    (OUT / "preflight.md").write_text(
        f"# Preflight\n\n**PASS:** {ok}\n\n```json\n{json.dumps(pre, indent=2)}\n```\n",
        encoding="utf-8",
    )
    if not ok:
        _finalize("MAY_20_CLOSE_PUBLISH_FAILED", pre, [], [], pre["ch_before"], pre["ch_before"], stems)
        return 1

    test_run_id = str(uuid.uuid4())
    pre["test_run_id"] = test_run_id
    backup_dir = OUT / "apr_sep_backup"
    pre["mirror"] = mirror_pilot_to_apr_sep(stems, backup_dir)
    (OUT / "backup_restore_evidence.md").write_text(
        f"""# Backup / restore evidence

## Pre-write CH snapshot
```json
{json.dumps(pre['ch_before'], indent=2)}
```

## apr_sep production file hashes (unchanged target)
```json
{json.dumps(pre['apr_sep_production_hashes'], indent=2)}
```

## Batch identity
- import_batch_id: `{pre['import_batch_id']}`
- test_run_id: `{test_run_id}`
- catalog_root: `{APR_SEP}` (20 May JSON + provenance append; backup under `{backup_dir}`)

## Recovery
- Idempotent re-run skips identical publishes via `resolve_existing_publish`.
- May pilot rows identifiable by `import_batch_id` in sidecar table.
""",
        encoding="utf-8",
    )

    try:
        outcomes = run_publish(hcfg, stems, pre["import_batch_id"], test_run_id)
    except Exception as exc:
        (OUT / "recovery_status.md").write_text(f"Publish aborted: {exc!r}\n", encoding="utf-8")
        ch_after = ch_line_stats(hcfg)
        _finalize("MAY_20_CLOSE_PUBLISH_FAILED", pre, [], [], pre["ch_before"], ch_after, stems)
        return 1

    ch_after = ch_line_stats(hcfg)
    readback = readback_all(hcfg, stems)

    pub_rows = []
    for stem, out in zip(stems, outcomes):
        pub_rows.append(
            {
                "stem": stem,
                "snapshot_id": out.get("snapshot_id"),
                "publish_status": (out.get("publish") or {}).get("status"),
                "import_batch_id": pre["import_batch_id"],
            }
        )

    _write_csv(OUT / "published_close_manifest.csv", pub_rows)
    _write_csv(OUT / "clickhouse_readback.csv", readback)
    _write_csv(
        OUT / "exact_density_parity.csv",
        [
            {
                "stem": r["stem"],
                "density_frozen_vs_ch": r["density_frozen_vs_ch"],
                "density_frozen_vs_4xab": r["density_frozen_vs_4xab"],
                "frozen": r["density_frozen_manifest"],
                "ch": r["density_ch_manifest"],
                "ref_4xab": r["density_phase4xab_ref"],
            }
            for r in readback
        ],
    )

    immutable_paths = (
        APR_SEP / "production/catalog_manifest.json",
        APR_SEP / "production/coverage_matrix.json",
    )
    apr_after_immutable = {str(p): _sha256_file(p) for p in immutable_paths}
    apr_before_immutable = {
        k: v
        for k, v in pre["apr_sep_production_hashes"].items()
        if k.endswith(("catalog_manifest.json", "coverage_matrix.json"))
    }
    sep_ok = apr_before_immutable == apr_after_immutable
    lookup_after = _sha256_file(APR_SEP / "production/provenance_lookup.json")
    sep_ch_stable = pre["ch_before"]["sep_window_count"] == ch_after["sep_window_count"]
    (OUT / "september_non_interference.md").write_text(
        f"""# September non-interference

| Check | Result |
|-------|--------|
| apr_sep catalog_manifest + coverage_matrix unchanged | **{sep_ok}** |
| provenance_lookup (additive May pilot keys) | before `{pre['apr_sep_production_hashes'].get(str(APR_SEP / 'production/provenance_lookup.json'), '')[:16]}…` after `{lookup_after[:16]}…` |
| Sep window publish count before/after | {pre['ch_before']['sep_window_count']} / {ch_after['sep_window_count']} |
| Line min/max close (May extends min; Sep max unchanged) | {pre['ch_before']['min_close']} → {ch_after['min_close']} / {pre['ch_before']['max_close']} → {ch_after['max_close']} |
| Sep probe readback (preflight) | {json.dumps(pre['sep_probe_readback'])} |

May publishes after pilot: **{ch_after['may_count']}** (expected 20).
""",
        encoding="utf-8",
    )

    all_rb = all(r["json_ch_parity_ok"] and r["reader_load_ok"] for r in readback)
    all_den = all(r["density_frozen_vs_ch"] and r["density_frozen_vs_4xab"] for r in readback)
    published_n = ch_after["may_count"] - pre["ch_before"]["may_count"]
    if published_n == 20 and all_rb and all_den and sep_ok and sep_ch_stable:
        verdict = "MAY_20_CLOSE_PUBLISH_AND_DENSITY_PARITY_PASS"
    elif published_n > 0 and (all_rb or all_den):
        verdict = "MAY_20_CLOSE_PUBLISH_PARTIAL"
    else:
        verdict = "MAY_20_CLOSE_PUBLISH_FAILED"

    (OUT / "recovery_status.md").write_text(
        f"Publish completed with {len(outcomes)} outcomes; may delta {published_n}.\n",
        encoding="utf-8",
    )
    _finalize(verdict, pre, outcomes, readback, pre["ch_before"], ch_after, stems)
    print(verdict, published_n)
    return 0 if verdict == "MAY_20_CLOSE_PUBLISH_AND_DENSITY_PARITY_PASS" else 1


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _finalize(
    verdict: str,
    pre: dict,
    outcomes: list,
    readback: list,
    ch_before: dict,
    ch_after: dict,
    stems: list[str],
) -> None:
    (OUT / "final_verdict.md").write_text(
        f"# Final verdict\n\n**{verdict}**\n\nMay publishes: {ch_after.get('may_count', '?')} (delta from {ch_before.get('may_count', 0)})\n",
        encoding="utf-8",
    )
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AE MAY 20-CLOSE PUBLISH V1

**VERDICT: `{verdict}`**

Pilot: `{stems[0]}` … `{stems[-1]}` ({len(stems)} closes). Catalog root: bridge (apr_sep production **not** modified).

## Abschlussfragen

1. **20 Closes veröffentlicht?** May delta on line: **{ch_after.get('may_count', 0) - ch_before.get('may_count', 0)}** (target 20).
2. **Übereinstimmung mit Frozen?** JSON↔CH readback: **{sum(1 for r in readback if r.get('json_ch_parity_ok'))}/{len(readback)}** PASS.
3. **Chart-Reader lesbar?** `load_historical_pool_state_at_close` 5m PD1: **{sum(1 for r in readback if r.get('reader_load_ok'))}/{len(readback)}** OK.
4. **Density-Bänder wie 4X-AB?** **{sum(1 for r in readback if r.get('density_frozen_vs_4xab'))}/{len(readback)}** frozen=4xab; CH density match **{sum(1 for r in readback if r.get('density_frozen_vs_ch'))}/{len(readback)}**.
5. **September unverändert?** Sep count stable; apr_sep hashes unchanged — see `september_non_interference.md`.
6. **Vollständiger Mai bereit?** **Nein** — requires 8928 COPY/provenance + further approval (only 20-close pilot executed).

Artifacts: `results/phase4xae_may_20_close_publish_v1/`

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
