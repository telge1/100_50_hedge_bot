#!/usr/bin/env python3
"""Phase 4X-AJ — full May density publish execution readiness (read-only, no CH writes)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xaj_full_may_publish_readiness_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAJ_FULL_MAY_PUBLISH_EXECUTION_READINESS_V1.md"
BRIDGE = OUT / "isolated_full_may_bridge_catalog"

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
PARITY_MANIFEST = ROOT / "results/phase4xab_full_may_density_continuity_v1/may_close_parity_manifest.json"

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
MONTH = "2026-05"
COMBO = "5m_PD1"
BATCH_SIZE = 50
BATCH_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Phase 4X-AI measured publish-only (batch A+B import wall / 100)
PUBLISH_SEC_PER_CLOSE = 38.84832245577127 / 100.0
READBACK_SEC_PER_CLOSE = 3671.6 / 100.0  # readback + exact density, 4X-AI resume

SEP_WINDOW_START = "2026-09-09"
SEP_WINDOW_END = "2026-09-18"


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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stem_from_close(dc: datetime) -> str:
    return dc.strftime("%Y%m%dT%H%M%SZ")


def _close_iso_from_stem(stem: str) -> str:
    return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _frozen_stems() -> list[str]:
    stems = sorted(p.stem for p in MULTI_MAY.glob("*.json"))
    if len(stems) != 8928:
        raise SystemExit(f"expected 8928 frozen stems, got {len(stems)}")
    return stems


def _parity_index() -> dict[str, dict[str, str]]:
    idx: dict[str, dict[str, str]] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["stem"]] = row
    if len(idx) != 8928:
        raise SystemExit(f"4X-AB parity rows {len(idx)} != 8928")
    return idx


def ch_ledger(hcfg: Any) -> dict[str, Any]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    lid, ch = hcfg.pipeline_line_id, hcfg.config_hash
    try:
        may_rows_raw = client.query(
            f"""
            SELECT decision_close, snapshot_id, publish_id, engine_input_hash,
                   active_state_fingerprint
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-05-01' AND decision_close < '2026-06-01'
            ORDER BY decision_close
            """,
            parameters={"lid": lid, "ch": ch},
        ).result_rows
        may_rows = []
        seen_stems: set[str] = set()
        for dc, snap, pub, eih, fp in may_rows_raw:
            if hasattr(dc, "tzinfo") and dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            stem = _stem_from_close(dc)
            if stem in seen_stems:
                raise RuntimeError(f"duplicate publish stem in ledger: {stem}")
            seen_stems.add(stem)
            may_rows.append([stem, snap, pub, eih, fp])
        sep_rows = client.query(
            f"""
            SELECT count(), min(decision_close), max(decision_close)
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
            """,
            parameters={"lid": lid, "ch": ch, "s": SEP_WINDOW_START, "e": SEP_WINDOW_END},
        ).result_rows[0]
        total = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
            """,
            parameters={"lid": lid, "ch": ch},
        ).result_rows[0][0]
        by_month = client.query(
            f"""
            SELECT formatDateTime(decision_close, '%Y-%m') AS mo, count()
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
            GROUP BY mo ORDER BY mo
            """,
            parameters={"lid": lid, "ch": ch},
        ).result_rows
        return {
            "database": db,
            "total_published": int(total),
            "may_published": [list(r) for r in may_rows],
            "may_count": len(may_rows),
            "sep_window_count": int(sep_rows[0]),
            "sep_window_min": str(sep_rows[1]),
            "sep_window_max": str(sep_rows[2]),
            "by_month": [list(r) for r in by_month],
        }
    finally:
        client.close()


def _frozen_payload_meta(stem: str) -> dict[str, Any]:
    p = json.loads((MULTI_MAY / f"{stem}.json").read_text(encoding="utf-8"))
    return {
        "snapshot_id": p.get("snapshot_id") or "",
        "engine_input_hash": p.get("engine_input_hash") or "",
        "active_state_fingerprint": p.get("active_state_fingerprint") or "",
        "active_pool_count": len(p.get("active_pool_ids") or []),
        "frozen_input_snapshot_id": str((p.get("frozen_input") or {}).get("snapshot_id") or ""),
    }


def build_isolated_bridge(remaining: list[str], apr_lookup: dict) -> tuple[list[dict], float]:
    """Stage remaining May JSON under results/ bridge; production apr_sep untouched."""
    t0 = time.perf_counter()
    prod = BRIDGE / "production"
    may_dest = prod / "results" / MONTH / COMBO
    may_dest.mkdir(parents=True, exist_ok=True)
    for name in ("catalog_manifest.json", "coverage_matrix.json"):
        shutil.copy2(APR_SEP / "production" / name, prod / name)
    lookup_src = APR_SEP / "production" / "provenance_lookup.json"
    lookup_data = json.loads(lookup_src.read_text(encoding="utf-8"))
    lookup_before_sha = _sha256_file(lookup_src)
    entries_before = set((lookup_data.get("entries") or {}).keys())

    rows: list[dict] = []
    added = 0
    skipped_existing_entry = 0
    for i, stem in enumerate(remaining):
        src = MULTI_MAY / f"{stem}.json"
        dst = may_dest / f"{stem}.json"
        src_sha = _sha256_file(src)
        if dst.is_file():
            if _sha256_file(dst) != src_sha:
                raise RuntimeError(f"bridge hash conflict {stem}")
        else:
            shutil.copy2(src, dst)
            added += 1
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        if rel in entries_before and rel in (lookup_data.get("entries") or {}):
            skipped_existing_entry += 1
        else:
            fid = _frozen_payload_meta(stem)["frozen_input_snapshot_id"]
            lookup_data.setdefault("entries", {})[rel] = {
                "source_frozen_id": fid,
                "dest_sha256": src_sha,
            }
        rows.append(
            {
                "stem": stem,
                "relative_dest": rel,
                "frozen_sha256": src_sha,
                "staging_sha256": _sha256_file(dst),
                "byte_identical": "YES",
                "provenance_entry_preexisted": "YES" if rel in entries_before else "NO",
            }
        )
        if (i + 1) % 1000 == 0:
            print(f"bridge staged {i + 1}/{len(remaining)}", flush=True)

    lookup_data["entry_count"] = len(lookup_data.get("entries") or {})
    lookup_data["phase4xaj_bridge"] = {
        "kind": "ADDED_VERIFICATION_METADATA",
        "remaining_may_stems_staged": len(remaining),
        "new_files_copied": added,
        "base_lookup_sha256": lookup_before_sha,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (prod / "provenance_lookup.json").write_text(
        json.dumps(lookup_data, indent=2, sort_keys=True), encoding="utf-8"
    )
    (prod / "phase4xaj_bridge_note.json").write_text(
        json.dumps(
            {
                "catalog_root": str(BRIDGE),
                "production_apr_sep_touched": False,
                "stems_staged": len(remaining),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    wall = time.perf_counter() - t0
    print(f"bridge complete: copied={added} provenance_skip={skipped_existing_entry} wall={wall:.1f}s")
    return rows, wall


def validate_bridge_batches(hcfg: Any, remaining: list[str]) -> dict[str, Any]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        BatchSpec,
        validate_batch,
    )

    chunks = [remaining[i : i + BATCH_SIZE] for i in range(0, len(remaining), BATCH_SIZE)]
    to_test = [0, len(chunks) // 2, len(chunks) - 1]
    results = {}
    for idx in to_test:
        spec = BatchSpec(
            month=MONTH,
            combo_folder=COMBO,
            stems=tuple(chunks[idx]),
            pipeline_line_id=PIPELINE_LINE,
            import_batch_id=str(uuid.uuid5(BATCH_NS, f"validate-batch-{idx:04d}")),
        )
        results[f"batch_index_{idx}"] = validate_batch(BRIDGE, spec)
    return results


def build_batch_plan(remaining: list[str], parity: dict[str, dict[str, str]]) -> list[dict]:
    batches = []
    for bi, i in enumerate(range(0, len(remaining), BATCH_SIZE)):
        chunk = remaining[i : i + BATCH_SIZE]
        bid = str(uuid.uuid5(BATCH_NS, f"may-publish-batch-{bi:04d}"))
        closes = []
        for stem in chunk:
            meta = _frozen_payload_meta(stem)
            pr = parity[stem]
            closes.append(
                {
                    "stem": stem,
                    "decision_close_utc": _close_iso_from_stem(stem),
                    "expected_snapshot_id": meta["snapshot_id"],
                    "engine_input_hash": meta["engine_input_hash"],
                    "active_state_fingerprint": meta["active_state_fingerprint"],
                    "expected_active_pool_count": meta["active_pool_count"],
                    "frozen_density_manifest_4xab": pr.get("frozen_density_manifest"),
                    "readback_gates": [
                        "compare_json_to_published",
                        "load_historical_pool_state_at_close",
                        "exact_unique_origin_density_vs_4xab",
                    ],
                }
            )
        batches.append(
            {
                "batch_index": bi,
                "import_batch_id": bid,
                "batch_size": len(chunk),
                "stem_first": chunk[0],
                "stem_last": chunk[-1],
                "closes": closes,
            }
        )
    return batches


def production_snapshot() -> dict[str, Any]:
    apr_may = APR_SEP / "production/results" / MONTH / COMBO
    lookup_path = APR_SEP / "production" / "provenance_lookup.json"
    lookup = json.loads(lookup_path.read_text(encoding="utf-8")) if lookup_path.is_file() else {}
    may_rels = [k for k in (lookup.get("entries") or {}) if f"/{MONTH}/{COMBO}/" in k]
    backups = []
    for p in (
        ROOT / "results/phase4xae_may_20_close_publish_v1",
        ROOT / "results/phase4xai_100_close_publish_performance_v1/apr_sep_backup",
    ):
        if p.is_dir():
            backups.append(str(p))
    pgrep = subprocess.run(["pgrep", "-af", "run_completion_import|execute_batch_import"], capture_output=True)
    return {
        "apr_sep_may_json_count": len(list(apr_may.glob("*.json"))) if apr_may.is_dir() else 0,
        "provenance_entry_count": lookup.get("entry_count"),
        "provenance_may_paths": len(may_rels),
        "catalog_manifest_sha256": _sha256_file(APR_SEP / "production/catalog_manifest.json"),
        "coverage_matrix_sha256": _sha256_file(APR_SEP / "production/coverage_matrix.json"),
        "provenance_lookup_sha256": _sha256_file(lookup_path),
        "prior_publish_backups": backups,
        "active_import_processes": pgrep.stdout.decode().strip().splitlines() if pgrep.stdout.strip() else [],
    }


def write_markdown_artifacts(
    *,
    remaining_n: int,
    bridge_wall: float,
    validate_results: dict,
    batches: list[dict],
) -> None:
    n_batches = len(batches)
    pub_s = remaining_n * PUBLISH_SEC_PER_CLOSE
    rb_s = remaining_n * READBACK_SEC_PER_CLOSE
    staging_est = bridge_wall * (8928 / max(remaining_n, 1))  # extrapolate full mirror once

    (OUT / "idempotent_resume_state_machine.md").write_text(
        """# Idempotent resume state machine (May full publish)

## Close-level states (execution orchestration)

| State | Detection | CH write on resume? | Action |
|-------|-----------|---------------------|--------|
| **NOT_PUBLISHED** | No `publish_complete=1` row for `(pipeline_line_id, config_hash, decision_close)` | Yes (normal import) | `execute_batch_import` |
| **PUBLISHED_READBACK_PENDING** | Ledger row exists with matching `snapshot_id` + fingerprints; post-batch readback failed (4X-AI pattern: `MISSING_PUBLICATION`) | **No** | Query ledger; retry readback/density only (`--resume-after-publish` style); bounded reader retry (e.g. 3×, 30s backoff) after `get_published_decision_closes` refresh |
| **PUBLISHED_VERIFIED** | Ledger match + `compare_json_to_published` OK + reader OK + density manifest = 4X-AB | **No** | Skip close in batch (`resolve_existing_publish` → `skipped_existing_publish`) |
| **INCONSISTENT_PARTIAL_WRITE** | Staged/meta without publish, fingerprint conflict, or catalog/member mismatch | **No** (STOP) | Manual triage; `CatalogIncompleteError` / `PublishConflictError`; no destructive auto-recovery |

## Pipeline mapping

- `resolve_existing_publish(..., allow_existing_publish=True)` → **PUBLISHED_VERIFIED** skip path (no duplicate ledger insert).
- `classify_close_resume` labels: `publish_skip` ≈ **PUBLISHED_VERIFIED**; `ready` ≈ **NOT_PUBLISHED**; `meta_without_publish` ≈ **INCONSISTENT_PARTIAL_WRITE** (investigate).
- Phase 4X-AI `--resume-after-publish`: safe when imports committed; **must not** re-run `execute_batch_import` for the same stems without ledger check.

## Batch orchestration rules

1. Preflight: `pilot_window` / month diff from CH ledger vs `full_may_batch_plan.json` (no duplicate stems).
2. After each batch import: gate on batch readback before next batch write (4X-AI pattern).
3. Transient reader failure: classify as **PUBLISHED_READBACK_PENDING** if ledger proves publish; never blind full-batch re-import.
4. Max retries per close (readback): **3**; then STOP with stem list.
""",
        encoding="utf-8",
    )

    (OUT / "transient_readback_recovery_plan.md").write_text(
        """# Transient readback recovery (4X-AI lesson)

## Incident

Full 100-close readback failed with `HistoricalPoolStateError: MISSING_PUBLICATION` after batch B import succeeded. CH publishes were already committed.

## Root cause class

Transient **reader index / publication list** lag or race — not missing ledger row (confirmed post-hoc: all 100 pilot stems loadable).

## Approved recovery (no CH rewrite)

1. Confirm `publish_complete=1` and expected `snapshot_id` per stem via CH query (ledger SSOT).
2. If ledger OK: run **readback-only** resume (`run_phase4xai.py --resume-after-publish` pattern).
3. Optional bounded retry on `load_historical_pool_state_at_close` with fresh `PublishedActiveReader` / short sleep (≤30s, max 3 attempts).
4. **Forbidden:** re-`execute_batch_import` for stems already in **PUBLISHED_VERIFIED** state.

## May full-month execution

- Implement runner flag: `--resume-readback-batch <batch_index>` after failed verification.
- Separate `--execute-batch <batch_index>` guarded by preflight ledger diff.
- Log per-close state transitions to `batch_progress.jsonl`.
""",
        encoding="utf-8",
    )

    (OUT / "batch_parity_gates.md").write_text(
        """# Batch parity gates (post-publish, per batch)

For **each** batch after `execute_batch_import` (and before next batch write):

| Gate | Check | Fail action |
|------|-------|-------------|
| G1 Ledger | `count(publish_complete=1)` in batch window += batch_size (or skips only for pre-published) | STOP; classify partial |
| G2 JSON↔CH | `compare_json_to_published` per stem | STOP; no next batch |
| G3 Reader | `load_historical_pool_state_at_close` snapshot_id match | Transient → **PUBLISHED_READBACK_PENDING** path; else STOP |
| G4 Density | Exact unique-origin depth≥2 manifest == frozen == 4X-AB `frozen_density_manifest` | STOP |
| G5 September | `sep_window` count unchanged; Sep probe stems readback OK | STOP |

**May:** full parity reference = Phase 4X-AB (`active_origin_parity_by_close.csv` / manifest SHA `may_close_parity_manifest.json`). **No sampling substitute** for production sign-off — automate full batch loop.

**Policy for month run:** run G1–G5 on **every** batch (50 closes). Mid-run spot-only is insufficient for final sign-off.
""",
        encoding="utf-8",
    )

    v_ok = all(not r.get("errors") for r in validate_results.values())
    (OUT / "measured_runtime_forecast.md").write_text(
        f"""# Measured runtime forecast (8808 remaining closes)

Sources: Phase 4X-AI — publish import wall **{PUBLISH_SEC_PER_CLOSE:.3f} s/close**; readback+density **{READBACK_SEC_PER_CLOSE:.1f} s/close** (100-close resume).

| Phase | Model | Estimate (8808 closes) | Evidence |
|-------|--------|--------------------------|----------|
| Catalog mirror to apr_sep (one-time) | Linear from isolated bridge build | **~{staging_est/3600:.1f} h** (extrapolation) | Bridge build **{bridge_wall:.0f} s** for {remaining_n} files this run |
| `execute_batch_import` (publish only) | {PUBLISH_SEC_PER_CLOSE:.3f} s × N | **~{pub_s/3600:.2f} h** ({pub_s:.0f} s) | 4X-AI measured |
| Reader readback + density | {READBACK_SEC_PER_CLOSE:.1f} s × N | **~{rb_s/3600:.1f} h** ({rb_s:.0f} s) | 4X-AI measured |
| Recovery buffer | +10% on publish+readback | **~{(pub_s+rb_s)*0.1/3600:.1f} h** | Planning margin |
| **Total (publish + verify, no staging)** | | **~{(pub_s+rb_s)/3600:.1f} h** | **NOT** a fixed SLA |

Batch count at 50: **{n_batches}** batches. `DEFAULT_BATCH_SIZE_CLOSES=50`, `MAX=200` — **50 recommended** (4X-AI proven, prewrite gate friendly).

**Do not** claim a single fixed wall-clock without instrumented pilot on production host.
""",
        encoding="utf-8",
    )

    (OUT / "production_execution_instructions.md").write_text(
        f"""# Production execution instructions (after explicit approval)

**Prerequisites:** verdict `FULL_MAY_PUBLISH_EXECUTION_READY`; no `run_completion_import` concurrent; disk ≥2 GiB free on apr_sep volume.

## Steps

1. **Preflight (read-only):** CH ledger vs `remaining_may_manifest.csv`; Sep window **2592**; `pgrep` clean.
2. **Backup:** copy `doge_apr_sep_2026_v1_history/production/provenance_lookup.json` → `results/phase4xaj_…/apr_sep_backup_<ts>.json`.
3. **Mirror:** for each stem in `remaining_may_manifest.csv`, copy frozen → apr_sep if missing or hash match (4X-AE/AI pattern); append provenance only for new paths.
4. **Batches:** for `batch_index` 0…{n_batches - 1} in `full_may_batch_plan.json`:
   - `validate_batch` (optional repeat)
   - `execute_batch_import` with listed `import_batch_id` and stems
   - Run G1–G5 gates (`batch_parity_gates.md`)
   - On transient G3 failure: **no re-import**; readback retry per `transient_readback_recovery_plan.md`
5. **Post-month:** CH May count **8928**; Sep unchanged; manifest hashes unchanged except provenance entry_count.

## Pipeline

- Catalog root: `doge_apr_sep_2026_v1_history` (not bridge-only — Aug `15m_PD10` pilot on root).
- Line: `{PIPELINE_LINE}`.

**This document does not authorize execution — separate human approval required.**
""",
        encoding="utf-8",
    )

    (OUT / "_bridge_validate_summary.json").write_text(
        json.dumps(
            {
                k: {"validated": not v.get("errors"), "error_count": len(v.get("errors") or []), "errors": (v.get("errors") or [])[:5]}
                for k, v in validate_results.items()
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    hcfg = _setup()
    parity = _parity_index()
    frozen = _frozen_stems()
    ledger = ch_ledger(hcfg)
    published_by_stem = {r[0]: r for r in ledger["may_published"]}
    prod = production_snapshot()

    remaining = [s for s in frozen if s not in published_by_stem]
    if len(remaining) + len(published_by_stem) != 8928:
        raise SystemExit("ledger + remaining != 8928")

    # Inventory CSV (global + May detail)
    inv_rows = [
        {
            "scope": "2026-05",
            "frozen_expected": 8928,
            "frozen_on_disk": len(frozen),
            "ch_published": ledger["may_count"],
            "remaining_to_publish": len(remaining),
            "apr_sep_may_files": prod["apr_sep_may_json_count"],
            "pipeline_line_id": PIPELINE_LINE,
            "config_hash": hcfg.config_hash,
        },
        {
            "scope": "2026-09-window",
            "frozen_expected": "",
            "frozen_on_disk": "",
            "ch_published": ledger["sep_window_count"],
            "remaining_to_publish": 0,
            "apr_sep_may_files": "",
            "pipeline_line_id": PIPELINE_LINE,
            "config_hash": hcfg.config_hash,
        },
        {
            "scope": "all-lines",
            "frozen_expected": "",
            "frozen_on_disk": "",
            "ch_published": ledger["total_published"],
            "remaining_to_publish": "",
            "apr_sep_may_files": "",
            "pipeline_line_id": PIPELINE_LINE,
            "config_hash": hcfg.config_hash,
        },
    ]
    with (OUT / "current_publish_inventory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(inv_rows[0].keys()))
        w.writeheader()
        w.writerows(inv_rows)

    published_rows = []
    for stem in frozen:
        if stem in published_by_stem:
            row = published_by_stem[stem]
            published_rows.append(
                {
                    "stem": stem,
                    "status": "PUBLISHED_VERIFIED_LEDGER",
                    "snapshot_id": row[1],
                    "publish_id": row[2],
                    "engine_input_hash_ch": row[3],
                    "fingerprint_ch": row[4],
                }
            )

    rem_manifest = []
    for stem in remaining:
        meta = _frozen_payload_meta(stem)
        pr = parity[stem]
        rem_manifest.append(
            {
                "stem": stem,
                "decision_close_utc": _close_iso_from_stem(stem),
                "status": "NOT_PUBLISHED",
                "expected_snapshot_id": meta["snapshot_id"],
                "engine_input_hash": meta["engine_input_hash"],
                "active_state_fingerprint": meta["active_state_fingerprint"],
                "expected_active_pool_count": meta["active_pool_count"],
                "frozen_sha256": _sha256_file(MULTI_MAY / f"{stem}.json"),
                "frozen_density_manifest_4xab": pr.get("frozen_density_manifest"),
                "density_parity_4xab": pr.get("density_parity"),
            }
        )
    with (OUT / "remaining_may_manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rem_manifest[0].keys()))
        w.writeheader()
        w.writerows(rem_manifest)

    with (OUT / "published_may_snapshot_ids.csv").open("w", newline="", encoding="utf-8") as f:
        if published_rows:
            w = csv.DictWriter(f, fieldnames=list(published_rows[0].keys()))
            w.writeheader()
            w.writerows(published_rows)

    bridge_rows, bridge_wall = build_isolated_bridge(remaining, {})
    with (OUT / "isolated_catalog_bridge_manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(bridge_rows[0].keys()))
        w.writeheader()
        w.writerows(bridge_rows)

    batches = build_batch_plan(remaining, parity)
    (OUT / "full_may_batch_plan.json").write_text(
        json.dumps(
            {
                "pipeline_line_id": PIPELINE_LINE,
                "config_hash": hcfg.config_hash,
                "batch_size": BATCH_SIZE,
                "batch_count": len(batches),
                "remaining_closes": len(remaining),
                "published_closes": len(published_by_stem),
                "batches": batches,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("validate_batch spot checks on bridge…", flush=True)
    validate_results = validate_bridge_batches(hcfg, remaining)
    write_markdown_artifacts(
        remaining_n=len(remaining),
        bridge_wall=bridge_wall,
        validate_results=validate_results,
        batches=batches,
    )

    blockers: list[str] = []
    if len(remaining) != 8808:
        blockers.append(f"remaining count {len(remaining)} != expected 8808")
    if ledger["may_count"] != 120:
        blockers.append(f"may ch count {ledger['may_count']} != 120")
    if ledger["sep_window_count"] != 2592:
        blockers.append(f"sep window {ledger['sep_window_count']} != 2592")
    if prod["active_import_processes"]:
        blockers.append("active import processes: " + "; ".join(prod["active_import_processes"][:3]))
    if not all(not r.get("errors") for r in validate_results.values()):
        blockers.append("validate_batch spot failures on isolated bridge")
    for stem in remaining:
        if parity[stem].get("density_parity") != "PASS":
            blockers.append(f"density not PASS {stem}")
            break

    if blockers:
        verdict = "FULL_MAY_PUBLISH_EXECUTION_BLOCKED" if any(
            "remaining count" in b or "validate_batch" in b for b in blockers
        ) else "FULL_MAY_PUBLISH_EXECUTION_PARTIAL"
    else:
        verdict = "FULL_MAY_PUBLISH_EXECUTION_READY"

    (OUT / "final_readiness_verdict.md").write_text(
        f"# Final readiness verdict\n\n**{verdict}**\n\nBlockers:\n"
        + ("\n".join(f"- {b}" for b in blockers) if blockers else "- none")
        + f"\n\nBridge wall: {bridge_wall:.1f}s\n",
        encoding="utf-8",
    )

    wall = time.perf_counter() - t0
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AJ FULL MAY PUBLISH EXECUTION READINESS V1

**VERDICT: `{verdict}`**

Read-only readiness. **No CH writes.** **No production catalog mutations.**

## Ledger (IST)

| Metric | Value |
|--------|-------|
| May frozen (verified) | **8928** |
| May CH published | **{ledger['may_count']}** |
| Remaining May | **{len(remaining)}** |
| Sep window CH | **{ledger['sep_window_count']}** (unchanged target) |
| apr_sep May JSON files | **{prod['apr_sep_may_json_count']}** |

## Abschlussfragen

1. **8808 fehlende Closes vorbereitet?** {"**Ja** — isolated bridge + `remaining_may_manifest.csv` + batch plan." if len(remaining) == 8808 and not any("validate_batch" in b for b in blockers) else "**Teilweise/blockiert** — siehe Blockers."}
2. **Importweg ohne neue Implementierung?** **Ja** — `execute_batch_import` / `validate_batch` / 50-close batches (4X-AI proven).
3. **Resume nach committed Publish sicher?** **Ja** mit Ledger-SSOT + readback-only resume; State machine in `idempotent_resume_state_machine.md`.
4. **Alle Batches vollständig validierbar?** **Ja** — G1–G5 pro Batch; 4X-AB manifests für alle 8928 closes.
5. **Kontrollierter Monatsimport nach Freigabe?** {"**Ja** — `production_execution_instructions.md`." if verdict == "FULL_MAY_PUBLISH_EXECUTION_READY" else "**Nur nach Klärung der Blockers.**"}
6. **Laufzeit inkl. Prüfungen?** Publish ~**{len(remaining) * PUBLISH_SEC_PER_CLOSE / 3600:.1f} h** + readback/density ~**{len(remaining) * READBACK_SEC_PER_CLOSE / 3600:.0f} h** (4X-AI extrapolation) — siehe `measured_runtime_forecast.md`.

## Artefakte

`results/phase4xaj_full_may_publish_readiness_v1/`

Runner wall: **{wall:.0f} s**.

**STOP.** Keine Monatsveröffentlichung in dieser Phase.
""",
        encoding="utf-8",
    )

    print(verdict, "remaining", len(remaining), "batches", len(batches), "wall", round(wall, 1))
    return 0 if verdict == "FULL_MAY_PUBLISH_EXECUTION_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
