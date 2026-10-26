#!/usr/bin/env python3
"""Phase 4X-AG — May–Sep density SSOT rollout performance plan (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xag_density_rollout_plan_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE4XAG_DENSITY_ROLLOUT_PERFORMANCE_PLAN_V1.md"
LMV2 = ROOT / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test"
MULTI = LMV2 / "doge_multi_month_2026_history/production/results"
APR = LMV2 / "doge_apr_sep_2026_v1_history/production/results"
SEP_STAGING = LMV2 / "doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1"
SEP_FLAT = LMV2 / "doge_september_2026_history/production/results/5m_PD1"

LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
CONFIG_HASH = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"
MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09")
EXPECTED_FROZEN = {"2026-05": 8928, "2026-06": 8640, "2026-07": 8928, "2026-08": 8928, "2026-09": 8640}
PHASE4AE_WALL_MS = 746164
PHASE4AE_CLOSES = 20


def _setup():
    spread = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
    trp = Path("/home/telgenbuescher/projects/trading_research_platform")
    for p in (ROOT, trp, spread):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def frozen_count(month: str) -> tuple[int, str]:
    if month == "2026-09":
        for label, base in (
            ("multi_month_staging", SEP_STAGING),
            ("doge_september_flat", SEP_FLAT),
            ("apr_sep_publish_copy", APR / month / "5m_PD1"),
        ):
            if base.is_dir():
                return len(list(base.glob("*.json"))), label
        return 0, "missing"
    d = MULTI / month / "5m_PD1"
    return (len(list(d.glob("*.json"))), str(d)) if d.is_dir() else (0, "missing")


def ch_inventory() -> dict:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        by_month = client.query(
            f"""
            SELECT formatDateTime(decision_close, '%Y-%m') AS mo, count() AS n,
                   min(decision_close), max(decision_close)
            FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
            GROUP BY mo ORDER BY mo
            """,
            parameters={"lid": LINE, "ch": CONFIG_HASH},
        ).result_rows
        total = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
            """,
            parameters={"lid": LINE, "ch": CONFIG_HASH},
        ).result_rows[0][0]
        return {"database": db, "total_published": int(total), "by_month": [list(r) for r in by_month]}
    finally:
        client.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _setup()
    ch = ch_inventory()
    ch_by_m = {str(r[0]): int(r[1]) for r in ch["by_month"]}

    rows = []
    total_frozen = 0
    total_published = 0
    total_remaining = 0
    for m in MONTHS:
        fc, src = frozen_count(m)
        pub = ch_by_m.get(m, 0)
        apr_n = len(list((APR / m / "5m_PD1").glob("*.json"))) if (APR / m / "5m_PD1").is_dir() else 0
        rem = max(0, fc - pub)
        total_frozen += fc
        total_published += pub
        total_remaining += rem
        rows.append(
            {
                "month": m,
                "frozen_on_disk": fc,
                "frozen_source": src,
                "apr_sep_catalog_files": apr_n,
                "ch_published": pub,
                "remaining_to_publish": rem,
                "expected_frozen": EXPECTED_FROZEN[m],
                "frozen_count_ok": fc == EXPECTED_FROZEN[m],
            }
        )

    with (OUT / "current_publish_inventory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    per_close_all_in = PHASE4AE_WALL_MS / 1000.0 / PHASE4AE_CLOSES
    _write_artifacts(rows, ch, total_frozen, total_published, total_remaining, per_close_all_in)
    verdict = "DENSITY_ROLLOUT_PLAN_READY" if total_remaining == 41452 else "DENSITY_ROLLOUT_PLAN_PARTIAL"
    _write_report(verdict, rows, ch, total_remaining, per_close_all_in)
    print(verdict, "remaining", total_remaining)
    return 0


def _write_artifacts(rows, ch, tf, tp, tr, per_close_all_in):
    (OUT / "import_pipeline_performance_analysis.md").write_text(
        f"""# Import pipeline performance analysis

## Observed (Phase 4X-AE, NOT per-phase instrumented)

| Metric | Value |
|--------|-------|
| Wall clock (terminal) | **{PHASE4AE_WALL_MS/1000:.1f} s** (~12.4 min) for **{PHASE4AE_CLOSES}** closes |
| All-in per close (publish + mirror + readback + density) | **{per_close_all_in:.1f} s** — **NOT_VERIFIED** as import-only |
| Comment in `historical_batch_config.py` | ~3 s / 10 closes (older pilot, different scope) — **not comparable** |

## Pipeline steps per close (`import_historical_close`)

1. `resolve_existing_publish` (CH read, idempotent skip)
2. `write_active_snapshot_staged` (meta + members INSERT)
3. `write_active_pool_catalog_from_json_rows` (~N active pools)
4. Sidecar `write_and_verify` + `require_before_publish`
5. `commit_publish_ledger`

Batch wrapper (`execute_batch_import`): per stem re-validates bundle; preflight scans pilot Aug `15m_PD10` stems on **catalog_root** (requires `doge_apr_sep` tree).

## Cost classes (planning)

| Class | Examples | 4X-AE evidence |
|-------|----------|----------------|
| **FIXED_OVERHEAD** | PYTHONPATH, CH client connect, `pilot_snapshot_ids_from_catalog` (10 stems), productive prewrite gate | Once per batch run |
| **PER_BATCH_COST** | `validate_batch`, `historical_import_write_lock`, scope CH layer audit | Scales with batch count |
| **PER_CLOSE_COST** | staged + catalog (~300+ rows) + sidecar + publish ledger | **Dominant** (inferred, NOT_VERIFIED) |
| **READBACK_COST** | `compare_json_to_published` + optional Node density manifest | 4X-AE did full readback×20 — **not required** on every batch in production |

**Conclusion:** Do **not** linearly extrapolate 12 min/20 to 41k closes without a **100-close instrumented pilot**.
""",
        encoding="utf-8",
    )

    batch_rows = []
    for size in (20, 50, 100, 500, 1000):
        supported = size <= 200
        note = "DEFAULT=50, MAX=200 in historical_batch_config.py"
        if size > 200:
            note = "Exceeds MAX_BATCH_SIZE_CLOSES — would need code/config change (out of scope)"
        batch_rows.append(
            {
                "batch_size": size,
                "supported_without_code_change": supported,
                "notes": note,
                "estimated_batches_for_41452": (41452 + size - 1) // size if supported else "",
            }
        )
    with (OUT / "batch_size_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(batch_rows[0].keys()))
        w.writeheader()
        w.writerows(batch_rows)

    (OUT / "catalog_bridge_compatibility.md").write_text(
        """# Catalog bridge compatibility (May–Sep)

## Proven (4X-AD / 4X-AE)

- Frozen under `doge_multi_month…/production/results/<month>/5m_PD1/` (May–Aug).
- Publish requires files under `doge_apr_sep_2026_v1_history/production/results/…` + `provenance_lookup` entry per path.
- `execute_batch_import` **cannot** use bridge-only root (missing Aug `15m_PD10` pilot JSON on root).

## Per month

| Month | Frozen source | apr_sep files today | CH published | Bridge compatible |
|-------|---------------|---------------------|--------------|-------------------|
| 2026-05 | multi_month prod | 20 (pilot) | 20 | **Yes** — extend COPY + provenance for remaining 8908 |
| 2026-06 | multi_month prod | 0 | 0 | **Yes** — same mechanism; **no** month-specific code change |
| 2026-07 | multi_month prod | 0 | 0 | **Yes** |
| 2026-08 | multi_month prod | 0 | 0 | **Yes** |
| 2026-09 | staging/september 8640; apr_sep 2592 (Sep 9–17) | 2592 | 2592 | **Partial** — only 2592 stems on apr_sep; full month bridge must not overwrite Sep 9–17 hashes; idempotent skip for overlap |

## Guards preserved

- `validate_bundle` → provenance SHA + frozen_input_id
- `catalog_manifest` / `coverage_matrix` unchanged (additive provenance only)
- `resolve_existing_publish` / `PublishConflictError` on fingerprint conflict
- No change to September snapshot_ids already on ledger

## September full month

8640 frozen exist on disk (`staging/…/2026-09` or `doge_september_2026_history`). **6048** stems outside published window still need bridge + publish; overlap **2592** must idempotently skip.
""",
        encoding="utf-8",
    )

    (OUT / "monthly_density_validation_plan.md").write_text(
        """# Monthly density validation plan (Jun–Sep)

**May:** `MAY_DENSITY_CONTINUITY_VERIFIED` — 8928/8928 (Phase 4X-AB). No full replay required before publish beyond spot gates.

**Jun–Aug / Sep remainder:** NOT full-month verified. Before month-scale CH publish:

1. Reuse `tools/phase4xab_full_may_density_continuity_v1/run_phase4xab.py` pattern per month (configurable month dir) — **read-only** replay vs frozen.
2. Gate: `MAY_DENSITY_CONTINUITY_VERIFIED` equivalent per month OR explicit `PARTIAL` with documented stem ranges.
3. Publish batches only for months passing gate (or pilot strips within month).

**Efficiency:** Single `ReusedSnapshotSession` per month; manifest SHA rollup (`may_close_parity_manifest.json` style); do **not** re-run during 4X-AG.

**This phase:** no replays executed.
""",
        encoding="utf-8",
    )

    (OUT / "resume_recovery_plan.md").write_text(
        """# Resume and recovery

## Existing mechanisms

- `validate_batch` / `BatchSpec` with `import_batch_id` (UUID)
- `resolve_existing_publish` → `skipped_existing_publish` when snapshot_id + fingerprints match
- `classify_close_resume` in `batch_runner.py` (publish_skip / ready / partial states)
- `historical_import_write_lock` — single writer per host
- `productive_prewrite_gate` + `assess_scope_ch_states` — blocks unsafe partial CH state
- Sidecar table — canonical original provenance per `(snapshot_id, pipeline_line_id)`

## Planned operations

1. **Deterministic batch manifest** — stem list + expected snapshot_ids from `validate_batch` output (store under `results/phase4x*_…/`).
2. **Progress** — query CH ledger for published stems per month; diff against target manifest.
3. **STOP** on `compare_json_to_published` failure or density manifest mismatch (sample or full batch per policy).
4. **Backup** — copy `provenance_lookup.json` before each provenance append (4X-AE pattern).
5. **No parallel** `run_completion_import` or unapproved batch writers (preflight `pgrep`).

## Partial write

- `CatalogIncompleteError` — manual cleanup required; do not blind resume.
- Orphan member without catalog — `ProductivePrewriteGateError`
""",
        encoding="utf-8",
    )

    (OUT / "shared_reader_parity_plan.md").write_text(
        """# Shared reader / density parity plan

## SSOT path (unchanged)

```
historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1 (CH publish)
  → PublishedActiveReader
  → load_historical_pool_state_at_close(timeframe=5m, pool_detail=1)
  → build_landscape_window (≤72h chunks)
  → pd1_exact_density_trp_adapter.js (pinned dashboard)
```

## Consumers

| Consumer | Requirement |
|----------|-------------|
| Research chart | Already uses scope resolver + CH pools |
| Backtester | Phase 4T `ssot_adapter` — spread PYTHONPATH + same window builder |

## Per-close gates (post-publish sample or batch)

1. `compare_json_to_published` (frozen JSON ↔ CH)
2. Density manifest SHA: frozen pools vs CH catalog-derived pools (Phase 4X-AE / 4X-AB analyzer)
3. Optional: `build_landscape_window` smoke on pilot window

## Fingerprints

- `active_state_fingerprint`, `engine_input_hash`, `snapshot_id` per close
- Rollup: SHA256 over sorted `stem|manifest` lines (4X-AB style)
""",
        encoding="utf-8",
    )

    (OUT / "next_100_close_pilot_plan.md").write_text(
        """# Next 100-close performance pilot (NOT EXECUTED)

## Scope

- **Stems:** `20260501T134000Z` … (100 consecutive 5m slots after existing 20-close pilot)
- **Line:** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`
- **Catalog:** apr_sep mirror (100 COPY + provenance append) — same as 4X-AE

## Instrumentation (required)

Split wall time:

1. Mirror + provenance append
2. `execute_batch_import` only (50+50 batches or one batch of 100 if manifest allows ≤200)
3. Readback sample (e.g. 10 closes full + 90 snapshot_id only)
4. Density manifests (sample 10)

## Success

- Throughput closes/min documented
- Sep ledger count unchanged except +100 May
- No `PublishConflictError`

## Approval

Explicit user sign-off + `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` + `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`
""",
        encoding="utf-8",
    )

    (OUT / "rollout_schedule.md").write_text(
        """# Rollout schedule (plan only)

| Phase | Action | Prerequisite | Stop if |
|-------|--------|--------------|---------|
| **A** | 100-close performance pilot | User approval; Jun–Sep density gate not required for perf-only May stems | Throughput anomaly; readback fail |
| **B** | Analyze pilot timings | Phase A artifacts | Cannot bound per-close cost |
| **C** | Month batch publish (May remainder → Aug) | Month density VERIFIED; provenance bridge | Any gate fail |
| **D** | CH/reader/density parity samples | Phase C batch | Manifest mismatch |
| **E** | Chart + backtester on shared reader | Phase D PASS on coverage window | OUT_OF_SCOPE / ImportError |

## User approval gates

- 100-close pilot
- Each month bulk publish (or rolling 5k batches)
- Sep full-month bridge (overlap with 2592)
- Backtester production wiring
""",
        encoding="utf-8",
    )

    low = per_close_all_in * 41452 / 3600
    mid_batches = 41452 / 50 * (PHASE4AE_WALL_MS / 1000.0 / PHASE4AE_CLOSES * 50) / 3600
    (OUT / "runtime_estimates.md").write_text(
        f"""# Runtime estimates

**All extrapolations: NOT_VERIFIED until Phase A (100-close pilot).**

| Model | Assumption | Wall time for ~41,452 remaining |
|-------|------------|----------------------------------|
| Naive linear (4X-AE all-in) | {per_close_all_in:.1f} s/close | **{low:.0f} h** |
| Import-only guess | 25 s/close (hypothesis) | **{25*41452/3600:.0f} h** |
| Optimistic comment | 0.3 s/close (10 closes / 3 s) | **{0.3*41452/3600:.1f} h** — likely stale |
| Batch overhead | 50 closes/batch, 4X-AE rate | **~{mid_batches:.0f} h** (same rate, more batch fixed cost not modeled) |

**Reliable measurement:** instrument Phase A (100 closes) with separated timers for import vs readback.
""",
        encoding="utf-8",
    )


def _write_report(verdict, rows, ch, tr, per_close):
    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AG DENSITY ROLLOUT PERFORMANCE PLAN V1

**VERDICT: `{verdict}`**

**Scope:** Analysis/plan only — **no CH writes, no imports.**

CH ledger (read-only): **{ch['total_published']}** publishes on `{LINE}`.

## Abschlussfragen

1. **Fehlende Closes?** **{tr}** remaining vs **44,064** frozen total (**{ch['total_published']}** already published: May **20**, Sep window **2592**). See `current_publish_inventory.csv`.
2. **Hauptzeitfaktor?** Per-close **CH staged + catalog + sidecar + ledger** (inferred); 4X-AE **~{per_close:.1f} s/close all-in** including readback/density — import fraction **NOT_VERIFIED**.
3. **Batch-Größen?** **DEFAULT 50**, **MAX 200** (`historical_batch_config.py`); controlled manifests often use 50.
4. **Vollständiger Mai–Sep Publish?** **NOT_VERIFIED** — naive linear **~{per_close*41452/3600:.0f} h**; measure via **100-close pilot** first.
5. **Zuverlässige Speed-Messung?** Phase **A** with split timers (import / readback / density sample).
6. **Bridge alle Monate?** **Ja** May–Aug; **Sep** mit Overlap-Idempotenz für bestehende **2592** (details `catalog_bridge_compatibility.md`).
7. **Doppelimport verhindern?** `resolve_existing_publish`, sidecar canonical fields, `PublishConflictError`, write lock, prewrite gate.
8. **Jun–Sep Density?** Wiederverwendung Phase-4X-AB-Runner pro Monat vor Bulk-Publish (`monthly_density_validation_plan.md`).
9. **Vor Backtester?** May–Sep publish coverage + reader/density parity gates + `build_landscape_window` chunking (Phase E).
10. **Nächster Schritt?** Explizite Freigabe **100-close performance pilot** (`next_100_close_pilot_plan.md`).

Artifacts: `results/phase4xag_density_rollout_plan_v1/`

**STOP.**
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
