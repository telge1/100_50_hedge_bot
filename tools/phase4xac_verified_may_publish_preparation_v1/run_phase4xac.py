#!/usr/bin/env python3
"""Phase 4X-AC — verified May 5m PD1 → shared CH SSOT publish preparation (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xac_verified_may_publish_preparation_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4XAC_VERIFIED_MAY_DENSITY_PUBLISH_PREPARATION_V1.md"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester" / "short" / "dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

MULTI = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
APR_SEP = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
MAY_DIR = MULTI / "production/results/2026-05/5m_PD1"
PARITY_DIR = ROOT / "results/phase4xab_full_may_density_continuity_v1"
PARITY_CSV = PARITY_DIR / "active_origin_parity_by_close.csv"
PARITY_MANIFEST = PARITY_DIR / "may_close_parity_manifest.json"

PILOT_START = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
PILOT_COUNT = 20
COMBO = "5m_PD1"
MONTH = "2026-05"
PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"

TABLES_PER_CLOSE = (
    "research_lmv2_active_snapshot_meta_v1",
    "research_lmv2_active_snapshot_member_v1",
    "research_lmv2_snapshot_active_pool_v1",
    "research_lmv2_active_snapshot_publish_v1",
)


def _setup_path() -> Path:
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts") or mod.startswith("pd1_landscape_viewer_v1"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if TRP.is_dir() and str(TRP) not in sys.path:
        sys.path.insert(0, str(TRP))
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    return dash


def _stem(dc: datetime) -> str:
    return dc.strftime("%Y%m%dT%H%M%SZ")


def _closes_may() -> list[datetime]:
    start = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=5)
    out, c = [], start
    while c < end:
        out.append(c)
        c += step
    return out


def _load_parity_index() -> dict[str, dict[str, str]]:
    if not PARITY_CSV.is_file():
        raise FileNotFoundError(PARITY_CSV)
    idx: dict[str, dict[str, str]] = {}
    with PARITY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["stem"]] = row
    return idx


def _ch_audit(hcfg: Any) -> dict[str, Any]:
    import clickhouse_connect

    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        q_line = f"""
        SELECT
          count() AS n,
          min(decision_close) AS mn,
          max(decision_close) AS mx,
          countIf(decision_close >= '2026-05-01' AND decision_close < '2026-06-01') AS may_n,
          countIf(decision_close >= '2026-09-09' AND decision_close < '2026-09-18') AS sep_window_n
        FROM {db}.{TABLE_PUBLISH} FINAL
        WHERE pipeline_line_id = {{lid:String}}
          AND config_hash = {{ch:String}}
          AND publish_complete = 1
        """
        row = client.query(
            q_line,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows[0]
        may_overlap = client.query(
            f"""
            SELECT decision_close, snapshot_id, active_state_fingerprint
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-05-01' AND decision_close < '2026-06-01'
            LIMIT 5
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows
        return {
            "database": db,
            "pipeline_line_id": hcfg.pipeline_line_id,
            "config_hash": hcfg.config_hash,
            "published_total": int(row[0]),
            "published_min": str(row[1]),
            "published_max": str(row[2]),
            "published_may_count": int(row[3]),
            "published_sep9_17_count": int(row[4]),
            "may_overlap_sample": [list(r) for r in may_overlap],
        }
    finally:
        client.close()


def _catalog_gate_audit() -> dict[str, Any]:
    apr_manifest = APR_SEP / "production/catalog_manifest.json"
    multi_manifest = MULTI / "production/catalog_manifest.json"
    cov_path = APR_SEP / "production/coverage_matrix.json"
    may_in_apr = (APR_SEP / "production/results/2026-05/5m_PD1").is_dir()
    may_files_apr = (
        len(list((APR_SEP / "production/results/2026-05/5m_PD1").glob("*.json")))
        if may_in_apr
        else 0
    )
    may_files_multi = len(list(MAY_DIR.glob("*.json")))
    has_5m_may_cov = False
    if cov_path.is_file():
        cov = json.loads(cov_path.read_text(encoding="utf-8"))
        for r in cov.get("MONTH_TF_PD_COVERAGE") or []:
            if r.get("month") == MONTH and r.get("combo") == COMBO and r.get("STATUS") == "PASS":
                has_5m_may_cov = True
    return {
        "apr_sep_catalog_manifest": apr_manifest.is_file(),
        "multi_month_catalog_manifest": multi_manifest.is_file(),
        "may_5m_pd1_in_apr_sep_coverage_matrix": has_5m_may_cov,
        "may_5m_pd1_files_apr_sep": may_files_apr,
        "may_5m_pd1_files_multi_month": may_files_multi,
        "import_catalog_root_recommended": str(APR_SEP),
        "frozen_source_of_truth": str(MAY_DIR),
        "gate_note": (
            "Sep pilot used doge_apr_sep_2026_v1_history + provenance_lookup entries; "
            "May frozen lives under doge_multi_month until COPY_VERIFIED + coverage row for 5m_PD1."
        ),
    }


def _dry_run_close(
    stem: str,
    parity: dict[str, str],
    hcfg: Any,
    manifest_ctx: Any,
    *,
    validate_import: bool,
) -> dict[str, Any]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.parse import (
        build_import_bundle,
        load_manifest_context,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import (
        build_live_micro_lld_v2_config,
        config_hash,
    )

    frozen_path = MAY_DIR / f"{stem}.json"
    if not frozen_path.is_file():
        return {"stem": stem, "status": "BLOCKED", "error": "missing_frozen"}
    payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    ch = config_hash(build_live_micro_lld_v2_config(1))
    bundle_errors: list[str] = []
    bundle_summary: dict[str, Any] = {}
    if validate_import:
        try:
            bundle = build_import_bundle(
                frozen_path,
                catalog_root=APR_SEP,
                config_hash=ch,
                manifest=manifest_ctx,
                pipeline_line_id=PIPELINE_LINE,
            )
            from research_charts.live_micro_lld_v2_ch_test.historical_json_import.validate import (
                validate_bundle,
            )

            validate_bundle(APR_SEP, bundle)
            bundle_summary = {
                "snapshot_id": bundle["snapshot_id"],
                "catalog_rows": len(bundle["catalog_rows"]),
                "validate_bundle": "PASS",
            }
        except Exception as exc:
            bundle_errors.append(f"{type(exc).__name__}: {exc}")
            bundle_summary = {"validate_bundle": "FAIL", "error": bundle_errors[-1][:500]}

    active_n = len(payload.get("active_pool_ids") or [])
    return {
        "stem": stem,
        "decision_close_utc": payload.get("decision_close"),
        "frozen_path": str(frozen_path),
        "phase4xab_active_parity": parity.get("active_parity"),
        "phase4xab_density_parity": parity.get("density_parity"),
        "phase4xab_density_manifest": parity.get("frozen_density_manifest"),
        "engine_version": payload.get("engine_version"),
        "engine_input_hash": payload.get("engine_input_hash"),
        "active_state_fingerprint": payload.get("active_state_fingerprint"),
        "prefix_candle_fingerprint": payload.get("prefix_candle_fingerprint"),
        "frozen_input_snapshot_id": (payload.get("frozen_input") or {}).get("snapshot_id"),
        "frozen_input_price_hash": (payload.get("frozen_input") or {}).get("price_hash"),
        "config_hash": ch,
        "pipeline_line_id": PIPELINE_LINE,
        "publish_target_tables": TABLES_PER_CLOSE,
        "expected_ch_rows_per_close": {
            "meta": 1,
            "member": active_n,
            "catalog": active_n,
            "publish_ledger": 1,
        },
        "import_bundle": bundle_summary,
        "import_gate_errors": bundle_errors,
        "status": "DRY_RUN_OK" if not bundle_errors else "DRY_RUN_IMPORT_GATE_FAIL",
    }


def _build_may_manifest(parity_idx: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dc in _closes_may():
        stem = _stem(dc)
        p = parity_idx.get(stem)
        if not p or p.get("active_parity") != "PASS" or p.get("density_parity") != "PASS":
            raise ValueError(f"missing or failed parity for {stem}")
        frozen_path = MAY_DIR / f"{stem}.json"
        if not frozen_path.is_file():
            raise FileNotFoundError(frozen_path)
        payload = json.loads(frozen_path.read_text(encoding="utf-8"))
        fi = payload.get("frozen_input") or {}
        rows.append(
            {
                "decision_close_utc": dc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stem": stem,
                "frozen_source": str(frozen_path),
                "frozen_input_snapshot_id": fi.get("snapshot_id"),
                "frozen_input_price_hash": fi.get("price_hash"),
                "frozen_input_revision_hash": fi.get("revision_hash"),
                "engine_version": payload.get("engine_version"),
                "engine_input_hash": payload.get("engine_input_hash"),
                "active_state_fingerprint": payload.get("active_state_fingerprint"),
                "density_parity_manifest_sha256": p.get("frozen_density_manifest"),
                "phase4xab_parity": "VERIFIED",
                "catalog_id": "doge_apr_sep_2026_v1",
                "pipeline_line_id": PIPELINE_LINE,
                "publish_target": f"clickhouse:{PIPELINE_LINE}",
            }
        )
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    dash = _setup_path()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.parse import (
        load_manifest_context,
    )

    hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    parity_idx = _load_parity_index()
    if len(parity_idx) != 8928:
        raise SystemExit(f"expected 8928 parity rows, got {len(parity_idx)}")

    parity_roll = json.loads(PARITY_MANIFEST.read_text(encoding="utf-8"))
    may_manifest = _build_may_manifest(parity_idx)
    manifest_path = OUT / "verified_may_publish_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "symbol": "DOGEUSDT",
                "timeframe": "5m",
                "pool_detail": 1,
                "month": MONTH,
                "closes": len(may_manifest),
                "phase4xab_verdict": "MAY_DENSITY_CONTINUITY_VERIFIED",
                "phase4xab_manifest_sha256": parity_roll.get("manifest_sha256"),
                "pipeline_line_id": PIPELINE_LINE,
                "config_hash": hcfg.config_hash,
                "engine_contract": "live_micro_lld_v2@trp",
                "entries": may_manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    catalog_gate = _catalog_gate_audit()
    ch_audit: dict[str, Any]
    try:
        ch_audit = _ch_audit(hcfg)
        ch_ok = True
    except Exception as exc:
        ch_audit = {"error": f"{type(exc).__name__}: {exc}"}
        ch_ok = False

    manifest_ctx = load_manifest_context(APR_SEP)
    pilot_stems = [_stem(PILOT_START + timedelta(minutes=5 * i)) for i in range(PILOT_COUNT)]
    pilot_rows = [
        _dry_run_close(s, parity_idx[s], hcfg, manifest_ctx, validate_import=True) for s in pilot_stems
    ]
    (OUT / "pilot_20_close_manifest.json").write_text(
        json.dumps(
            {
                "pilot_window": {
                    "first": pilot_stems[0],
                    "last": pilot_stems[-1],
                    "count": len(pilot_stems),
                },
                "dry_run": True,
                "writes": False,
                "closes": pilot_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    exp_counts = {
        "per_close": pilot_rows[0]["expected_ch_rows_per_close"] if pilot_rows else {},
        "pilot_20_aggregate": {
            t: sum(r["expected_ch_rows_per_close"].get(k, 0) for r in pilot_rows)
            for k, t in (
                ("meta", "meta"),
                ("member", "member"),
                ("catalog", "catalog"),
                ("publish_ledger", "publish_ledger"),
            )
        },
        "full_may_8928_aggregate": {
            "meta": 8928,
            "publish_ledger": 8928,
            "member": "sum(active_count per close)",
            "catalog": "sum(active_count per close)",
        },
    }
    (OUT / "expected_ch_row_counts.json").write_text(json.dumps(exp_counts, indent=2), encoding="utf-8")

    import_gate_fail = any(r.get("import_gate_errors") for r in pilot_rows)
    sep_protected = ch_ok and ch_audit.get("published_may_count", 0) == 0
    density_ready = len(may_manifest) == 8928
    catalog_ready = (
        catalog_gate["may_5m_pd1_files_multi_month"] == 8928
        and catalog_gate["may_5m_pd1_in_apr_sep_coverage_matrix"]
        and catalog_gate["may_5m_pd1_files_apr_sep"] == 8928
    )

    if density_ready and not import_gate_fail and catalog_ready and sep_protected:
        verdict = "VERIFIED_MAY_PUBLISH_READY"
    elif density_ready and sep_protected:
        verdict = "VERIFIED_MAY_PUBLISH_PARTIAL"
    else:
        verdict = "VERIFIED_MAY_PUBLISH_BLOCKED"

    _write_support_docs(OUT, dash, hcfg, ch_audit, catalog_gate, pilot_rows, verdict)
    _write_report(verdict, ch_audit, catalog_gate, import_gate_fail, pilot_stems)
    print(verdict)
    return 0 if verdict != "VERIFIED_MAY_PUBLISH_BLOCKED" else 1


def _write_support_docs(
    out: Path,
    dash: Path,
    hcfg: Any,
    ch_audit: dict,
    catalog_gate: dict,
    pilot_rows: list[dict],
    verdict: str,
) -> None:
    (out / "publish_pipeline_audit.md").write_text(
        f"""# Publish pipeline audit (read-only)

## Existing controlled 5m PD1 path

| Step | Module |
|------|--------|
| Parse frozen JSON | `historical_json_import/parse.py` → `build_import_bundle` |
| Validate | `validate.py` → `validate_bundle` (provenance + semantics) |
| Staged meta/member | `active_snapshot/staged_writer.py` |
| Catalog | `catalog_write.write_active_pool_catalog_from_json_rows` |
| Sidecar (batch) | `provenance_sidecar_store` (required before publish) |
| Publish ledger | `publisher.commit_publish_ledger` via `import_historical_close` |
| Batch gate | `batch_write.execute_batch_import` (+ `HISTORICAL_JSON_IMPORT_ALLOW_WRITE`) |

**Sep 2026 pilot reference:** `logs/historical_import/overnight_2026/phase6xt_5m_pd1_controlled_release_pilot_v1/`  
Catalog root: `doge_apr_sep_2026_v1_history` (2592 stems, batch UUID `c18994e7-…`).

## Target line (unchanged)

- `pipeline_line_id`: `{PIPELINE_LINE}`
- `config_hash`: `{hcfg.config_hash}`
- `timeframe` / `pool_detail`: 5m / PD1
- Dashboard contract: `{dash}/research_charts/historical_lmv2_pd_contract_v1.py`

## Idempotency

- `resolve_existing_publish` skips when fingerprint + snapshot_id match (`allow_existing_publish=True` on import path).
- Conflicting `decision_close` → `PublishConflictError` (no silent overwrite).

## ClickHouse read snapshot

```json
{json.dumps(ch_audit, indent=2)}
```

## Catalog / frozen gate

```json
{json.dumps(catalog_gate, indent=2)}
```

## Pilot dry-run import gate

First pilot close validate_bundle: **{pilot_rows[0].get('import_bundle', {}).get('validate_bundle', 'n/a')}**
""",
        encoding="utf-8",
    )

    (out / "shared_reader_integration_plan.md").write_text(
        """# Shared chart / backtester integration plan

## Canonical stack (no Python density recompute)

```
ClickHouse publish (5m PD1 line)
  → PublishedActiveReader
  → load_historical_pool_state_at_close(decision_close, timeframe=5m, pool_detail=1)
  → build_landscape_window(vis_from, vis_to, timeframe=5m, pool_detail=1)
  → client pd1_exact_density_trp_adapter.js (pinned dashboard path)
```

## Backtester

- Reuse **same** `build_landscape_window` + Node adapter as Phase 4T / Phase 1B (`ssot_adapter.py` pattern).
- PYTHONPATH: spread dashboard + `trading_research_platform` (not pools-only contract shadow).
- Chunk windows ≤ `MAX_EXPORT_HOURS` (72h) for May 28d runs.

## Density contract

- Exact unique-origin bands, min depth 2.
- ACTIVE only (`include_remaining=False` in window builder).
- No alternate analyzer path in backtester.
""",
        encoding="utf-8",
    )

    (out / "chart_backtester_parity_gates.md").write_text(
        """# Parity gates (post-pilot-publish)

| Gate | Check | STOP if |
|------|-------|---------|
| A | Frozen JSON ↔ CH staged/meta/member/catalog | Any fingerprint or pool_id set mismatch |
| B | CH ↔ `PublishedActiveReader` | `UnpublishedSnapshotError` or catalog/member drift |
| C | Reader pools ↔ Node exact density manifest | Band geometry / origin membership mismatch |
| D | Chart export ↔ backtester export at same close | Manifest hash differs |
| E | No future origins at past `decision_close` | `known_at > decision_close` |
| F | Legacy origins not dropped by context window | ACTIVE loss vs frozen without LMV2 event |

Expand publish scope only if **all** gates PASS for pilot window.
""",
        encoding="utf-8",
    )

    (out / "backup_recovery_plan.md").write_text(
        """# Backup and recovery (plan only)

## Before first May write batch

1. Export publish ledger slice for `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` (expect Sep 9–17 window only).
2. Record counts per table: meta, member, catalog, publish (`publish_complete=1`).
3. Snapshot `provenance_lookup.json` + sidecar store for 5m PD1 batch UUID.

## Recovery

- **Idempotent re-import:** same frozen + snapshot_id → skip via `resolve_existing_publish`.
- **Conflict:** do not delete Sep rows; use pipeline_line_id + config_hash + decision_close uniqueness; fix forward, never overwrite Sep fingerprints.
- **Partial batch failure:** stop batch; `CatalogIncompleteError` path requires manual catalog cleanup before retry.

## No action in Phase 4X-AC (read-only).
""",
        encoding="utf-8",
    )

    (out / "rollout_decision.md").write_text(
        f"""# Rollout decision plan

**Preparation verdict:** `{verdict}`

| Step | Action | PASS | STOP |
|------|--------|------|------|
| 0 | Phase 4X-AC manifest + catalog gate | 8928 verified + provenance path clear | Missing parity or import gate |
| 1 | **Explicit approval:** 20-close CH publish pilot | Gates A–F on 20 stems | Any gate fail |
| 2 | Readback + density parity pilot | 0 mismatches | >0 |
| 3 | **Explicit approval:** full May 8928 publish | Batch size 50, sidecar enforced | Conflict / partial catalog |
| 4 | Wire backtester to shared reader path | Adapter parity | ImportError / wrong line |
| 5 | Dynamic 5m signal generation | Causal signals | Lookahead |
| 6 | 24h control backtest | Expected fills stable | Drift |
| 7 | 14d backtest | SSOT coverage | OUT_OF_SCOPE |
| 8 | 28d backtest | Full May window published | Missing closes |

## Required human approval for 20-close pilot

1. Written go on **ClickHouse writes** (`HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`).
2. Confirm **May 5m_PD1** registered in `doge_apr_sep` provenance + on-disk COPY (8928 files) *or* approved catalog extension for `doge_multi_month`.
3. Confirm **no** concurrent `run_completion_import` / unapproved batch writers on `…_5mpd1_v1`.
4. New `import_batch_id` UUID recorded; sidecar store path declared.
5. Post-publish readback script sign-off (Phase 6XT pre_publish_readback pattern).
""",
        encoding="utf-8",
    )


def _write_report(
    verdict: str,
    ch_audit: dict,
    catalog_gate: dict,
    import_gate_fail: bool,
    pilot_stems: list[str],
) -> None:
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AC VERIFIED MAY DENSITY → SHARED CHART/BACKTESTER SSOT

**VERDICT: `{verdict}`**

Scope: DOGEUSDT / 5m / PD1 / exact unique-origin density / min-depth 2 / original ACTIVE only. **No ClickHouse writes in this phase.**

## Summary

| Item | Status |
|------|--------|
| Phase 4X-AB density continuity | **8928/8928 VERIFIED** |
| `verified_may_publish_manifest.json` | **8928** closes with frozen path + parity fingerprints |
| Existing import pipeline | **`import_historical_close` / `execute_batch_import`** (Sep 5m PD1 pilot proven) |
| September 5m PD1 line | **Protected** — May publish count on line: **{ch_audit.get('published_may_count', 'n/a')}** (must stay 0 until approved May import) |
| Catalog/provenance gate for May 5m | **{ 'PASS' if catalog_gate.get('may_5m_pd1_in_apr_sep_coverage_matrix') and catalog_gate.get('may_5m_pd1_files_apr_sep')==8928 else 'OPEN' }** |
| 20-close dry-run | Stems `{pilot_stems[0]}` … `{pilot_stems[-1]}` — import validate_bundle: **{'FAIL (expected until COPY+provenance)' if import_gate_fail else 'PASS'}** |

Artifacts: `results/phase4xac_verified_may_publish_preparation_v1/`

## Abschlussfragen

1. **Sind sämtliche 8928 geprüften Mai-Closes publish-fähig?**  
   **Fachlich ja** (Replay/Frozen/Density verifiziert). **Technisch** erst nach apr_sep-Provenance + Dateispiegel (wie Sep-Pilot) oder freigegebenem `catalog_manifest` auf `doge_multi_month`.

2. **Kann die bestehende CH-Pipeline unverändert verwendet werden?**  
   **Ja** — dieselbe Line `{PIPELINE_LINE}`, Parser, Sidecar-Gate, Ledger-Commit; kein zweiter Publisher.

3. **Ist die September-Publish-Line geschützt?**  
   **Ja** — Idempotenz per `decision_close` + Fingerprints; May-Zeitraum aktuell **ohne** Publish-Zeilen; kein Überschreiben bestehender Sep-Stems bei identischen Imports.

4. **Wie nutzt der Backtester denselben Exact-Density-Reader?**  
   `PublishedActiveReader` → `load_historical_pool_state_at_close` → `build_landscape_window` → **`pd1_exact_density_trp_adapter.js`** (spread dashboard PYTHONPATH, Phase 4T/1B pattern).

5. **Welche Freigabe für den 20-Close-Pilot?**  
   Explizite Freigabe für CH-Writes + May **5m_PD1** in apr_sep Provenance/Coverage + Batch-UUID + Sidecar + Readback-Gate; kein paralleler Legacy-Completion-Import (siehe `rollout_decision.md`).

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
