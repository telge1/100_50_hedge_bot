# PHASE BT-D2 — Backtester Exact Unique-Origin Depth-2 Contract V1

**Date:** 2026-10-25  
**Mode:** Read-only forensics. No backtester code changes, no productive backtest, no SSOT/chart edits.

**Binding research scope (user decision):**

| Parameter | Value |
|-----------|--------|
| Symbol | DOGEUSDT |
| Timeframe | 5m |
| Pool detail | PD1 (`pool_detail=1`) |
| Density method | Exact Unique-Origin Density (Chart **B**) |
| Min unique origins | 2 (Depth slider = **2**) |

**Reference instant (parity anchor):** `2026-09-25T11:00:00Z`  
**Expected band counts:** 39 upper · 179 lower · **218** total (max unique-origin density **9**).

---

## VERDICT

### `DEPTH2_INTEGRATION_REQUIRED`

`min_depth: 2` exists in strategy JSON and in TP/lifecycle helpers, but the **production backtest bar-walk does not load or enforce Exact-Density bands from `build_landscape_window`**. Landscape geometry is **pre-materialized** (Phase 2A/3B signals, Phase 3A pilot timeline) or **raw ACTIVE members** (Historical LMV2 bridge). There is **no fail-closed** guard if `density_method` / `min_unique_origins` is missing or wrong.

---

## 1. Backtester data path (actual)

### 1.1 Primary strict pool-landscape backtester (Phase 4D V2)

| Stage | Path / module | What it uses |
|-------|----------------|--------------|
| Entry | `tools/phase4d_pool_landscape_backtester_v2/run_phase4d_backtest.py` | Loads `strategy_config_v2.json`, Phase 3A prices CSV |
| Signals | `signal_provider.py` | **Phase 2A** `candidates.csv` + per-candidate JSON profiles; **Phase 3B** confirmation CSVs |
| Prices | `results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.csv` | OHLC from **one** Phase 3A SSOT window build |
| Pool continuity | `pool_zone_tracker.py` | Phase 3A `upper_pool_lifecycle.csv` / `lower_pool_lifecycle.csv` (`depth==2` events) |
| Strategy | `strategy_rules.py` | Entry at pool edges; TP from `landscape_intervals` with `depth==2` |
| Engine | `engine.py` | No landscape fetch; no `min_depth` validation |

**Phase 3A window (frozen):** `visible_from_utc` … `visible_to_utc` = **2026-09-11T12:00:00Z** … **2026-09-12T16:00:00Z** (`research_config_v1.json`). The parity anchor **2026-09-25** is **outside** this pilot span.

### 1.2 Where Depth-2 geometry enters signals (indirect)

| Step | Path | Mechanism |
|------|------|-----------|
| SSOT pools (once per Phase 3A/2A window) | `tools/phase1b_depth2_landscape_pilot_v1/run_pilot.py` → `build_landscape_window` | `pd1_landscape_viewer_v1/historical_window_v1.py`, `WARMUP_HOURS=168` |
| Exact density | `tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js` | Loads production `pd1_exact_density_trp_adapter.js`; `buildDepth2Intervals` → **`chart.depth2`** (`poolsToExactOriginZones` → `mergeExactOriginSegments` → `filterSegmentsByMinOrigins(…, 2)`) |
| Short scan | `tools/phase2a_5m_pd1_depth2_short_candidates_v1/timeline_scanner.js` | Picks **upper** bands from `display_intervals` (chart path), stores `landscape_intervals` in candidate profile |

This chain **matches the Research Chart algorithm class**, but it is **not re-run per backtest bar** and **not tied** to the Jun–Sep published timeline used by the chart today.

### 1.3 Historical LMV2 backtester input plane (Phase 6B/6C)

| Module | Delivers |
|--------|----------|
| `backtester/short/dashboard/research_charts/historical_lmv2_backtester_adapter_v1.py` | ACTIVE **members** at `decision_close` |
| `historical_lmv2_backtester_input_bridge_v1.py` | Bridge metadata; **`scanner_pool_equivalence: NOT_CLAIMED`** |

**No** `computeExactBandsForSide`, **no** `min_unique_origins` filter on this path.

### 1.4 Other backtest-related code (not Exact-Density SSOT)

- `backtester/short/dashboard/research_charts/dynamic_liquidity_landscape_v1` / `analyze_side` — used in research audits (`run_audit.py`), **cluster-style** metrics, **not** the Chart B exact-origin sweep.
- `nested_ask_pool_backtester.py`, `pool_signals_backtester.py` — legacy engine pool loaders (`load_engine_pools_at`), **out of scope** for PD1 Exact Density.

---

## 2. Depth parameter in the backtester today

| Location | Field | Runtime enforcement |
|----------|--------|---------------------|
| `tools/phase4d_pool_landscape_backtester_v2/strategy_config_v2.json` | `"min_depth": 2` | **Documented only** — `engine.py` never reads it |
| `strategy_rules.py` | `depth2_intervals()`, `select_*_tp_depth2` | Filters **pre-attached** `landscape_intervals` where `depth==2` |
| `pool_zone_tracker.py` | lifecycle `depth==2` | Continuity vs Phase 3A lifecycle, not live band recompute |
| `tools/phase4m_touch_rejection_market_backtester_v1/market_engine.py` | `ENTRY_POOL_DEPTH2_LOST` | Depth-2 **lifecycle** loss, not missing config guard |

**Missing (required for BT-D2):**

- `density_method` / `min_unique_origins` in config schema
- Fail-closed if `min_unique_origins != 2` or method ≠ exact unique-origin
- Per-`decision_close` band manifest comparable to chart export
- Run manifest fields (see §7)

---

## 3. Canonical Research Chart density chain (contract source)

**Do not reimplement geometry in Python for production parity; pin the adapter.**

| Step | Implementation |
|------|----------------|
| ACTIVE pools as-of | `historical_window_v1.build_landscape_window` → published reader → `load_historical_pool_state_at_close`; segments clipped to window; **168h** context (`context_from = max(data_lo, vis_from − 168h)`) |
| Active at instant | `poolActiveAt(pool, tMs)` on segment `[start,end]` inclusive |
| Per-side sweep | `computeExactBandsForSide(pools, t, side, yMin, yMax)` in `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_density_trp_adapter.js` |
| Depth metric | Count of **distinct `origin` IDs** among overlapping ACTIVE pools (multiple pools same origin → **once**) |
| Time column | `poolsToExactOriginZones` with `causalDecisionCloseMs`, `slotMinutes=5` for 5m |
| Merge | `mergeExactOriginSegments` |
| Depth slider = 2 | `filterSegmentsByMinOrigins(segments, minOrigins)` with `minOrigins >= 2` |
| UI wiring | `pd1_exact_origin_density_research_v1.js` → `getMinOrigins()` / Depth slider |

**Forensic cross-check (Phase 1B):** `analyzer_depth2_landscape.js` compares sweep forensic bands vs `chart.depth2` (`compareParity`); pilot `parity_check.json` reported **177** intervals at S1 as-of with `chart_path_parity.ok: true`.

**Read-only reference export at 2026-09-25T11:00:00Z:**

- `results/phase_density_chart_parity_20260925_1100/pools.json`
- Generator: `tools/phase_density_chart_parity_20260925_1100/extract_bands.py` (SSOT window `2026-09-23T11:00:00Z` … `2026-09-25T11:05:00Z`, evaluate `t_draw = floor5m(11:00)`)

---

## 4. Technical contract (binding for backtester integration)

1. **Inputs:** Same `build_landscape_window(timeframe="5m", pool_detail=1)` pool payloads as `POST /live-charts/viewer/api/window` (published ACTIVE SSOT only; no lookahead).
2. **As-of:** For each decision close `t`, use the same `tMs` / `causalDecisionCloseMs` semantics as Chart B (documented in reference `meta.timestamp_semantics`).
3. **Sides:** Compute **upper** and **lower** independently.
4. **Bands:** After merge + `filterSegmentsByMinOrigins(…, 2)`, use **only** bands with ≥ **2** unique origin IDs; upper/lower price bounds must match chart rectangles (bottom/top).
5. **Origins:** Deduplicate by **`origin`**, not `pool_id`.
6. **Warmup:** Minimum **168h** pool context; origins older than the visible window remain ACTIVE if segments say so (no 168h origin amnesia).
7. **Prohibited:** `clusters_by_dc`, `analyze_side` cluster mass, unfiltered single pools, or silent default to depth 1.
8. **Fail-closed:** If `density_method` ≠ `exact_unique_origin` or `min_unique_origins` ≠ `2`, or band export cannot be produced → **abort run** with explicit error (no fallback to raw pools).

---

## 5. Does the backtester use unfiltered pools or clusters today?

| Use case | Geometry |
|----------|----------|
| Phase 4D entry pool | **Single upper pool** from Phase 2A scan (chosen from depth-2 `display_intervals` at signal time), not full band set per bar |
| Phase 4D TP | Subset of **cached** `landscape_intervals` with `depth==2` |
| Phase 3A timeline counts | `landscape_timeline.js` via same Phase 1B adapter (depth-2 display intervals per bar **within pilot window only**) |
| Phase 6C bridge | **Unaggregated ACTIVE members** — not density bands |
| Research `analyze_side` | **Different** landscape model — not Chart B |

**Conclusion:** The strict backtester does **not** consume the **full Exact-Density band field** (218 bands at Sep-25) on each bar. It uses **discrete signal-attached intervals** and **lifecycle depth-2 tracking**, which are **related** but **not guaranteed** to stay byte-identical to a live chart recompute after publish scope changes.

---

## 6. Automatic parity test (prepared)

**Tool:** `tools/phase_bt_d2_backtester_depth2_contract_v1/run_depth2_parity.py`

| Gate | Description |
|------|-------------|
| G1 | Reference `pools.json` vs fresh `extract_bands.py` recompute — all sides, edges, `unique_origin_count`, `origin_ids` |
| G2 | Reference vs `--backtester-manifest` (optional JSON) — **currently fails closed** with `BACKTESTER_DENSITY_MANIFEST_MISSING` |

**Run (read-only, requires CH/SSOT access like other landscape tools):**

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot
PYTHONPATH=... # same as dashboard acceptance: spread dashboard + TRP + HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d2_backtester_depth2_contract_v1/run_depth2_parity.py
```

**Output:** `results/phase_bt_d2_backtester_depth2_contract_v1/parity_report.json`

**Expected G1:** PASS (39 / 179 / 218, full edge + origin parity).  
**Expected G2 today:** BLOCKED until backtester emits manifests.

**Future backtester manifest shape (minimal):**

```json
{
  "density_method": "exact_unique_origin",
  "min_unique_origins": 2,
  "timeframe": "5m",
  "pool_detail": 1,
  "decision_close_utc": "2026-09-25T11:00:00Z",
  "pipeline_line_id": "...",
  "config_hash": "...",
  "adapter_sha256": "...",
  "upper": [ { "lower_price", "upper_price", "unique_origin_count", "origin_ids" } ],
  "lower": [ ... ]
}
```

---

## 7. Configuration, run manifest, and results traceability

| Layer | Required fields |
|-------|------------------|
| **Strategy / run config** | `density_method: "exact_unique_origin"`, `min_unique_origins: 2`, `timeframe: "5m"`, `pool_detail: 1`, `pool_pd: "PD1"` |
| **Run manifest** (start of backtest) | Copy of above + `pipeline_line_id`, `config_hash`, `landscape_warmup_hours: 168`, `density_adapter_path` or `adapter_content_hash`, `published_data_to` / `from` |
| **Per decision close** (or on-demand cache) | `exact_density_bands.json` slice or embedded hash in signal row |
| **Results / summary** | `density_contract_version: "BT-D2-v1"`, `min_unique_origins`, `parity_gate_ref` (e.g. `phase_density_chart_parity_20260925_1100`), G1/G2 status |

**Fail-closed startup checks (integration):**

1. Assert `density_method` and `min_unique_origins` present and equal to contract.
2. Assert `build_landscape_window` import + publish line matches viewer (`historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` for 5m PD1).
3. On first decision close in range, run G1-equivalent self-test vs pinned reference or live recompute; STOP on mismatch.

---

## 8. Deviations vs Research Chart (2026-09-25)

| Topic | Chart / reference | Backtester today |
|-------|-------------------|------------------|
| Data reach | Published closes through **2026-09-30** (post 4X publish) | Phase 3A/4D pilot **Sep 11–12** only |
| Per-bar bands | Recomputed from SSOT for each `t` | Frozen at signal generation or absent |
| Band count @ 11:00 Sep 25 | **218** | **Not produced** by 4D engine |
| `min_depth` in JSON | N/A (UI slider) | Present but **not enforced** in engine |
| Fail-closed | API `OUT_OF_SCOPE` / viewer errors | No density contract errors |

---

## 9. Minimal implementation plan (after separate approval)

1. **Density provider module** (new, isolated): `decision_close` → `build_landscape_window` chunk policy → Node subprocess `analyzer_depth2_landscape.js` `buildDepth2Intervals` **or** subprocess pinned adapter — return `display_intervals` only.
2. **Wire Phase 4D+** to call provider each bar (or precompute timeline CSV for full May–Sep) instead of static Phase 2A `landscape_intervals` for TP/entry validation.
3. **Startup + manifest:** BT-D2 fields; abort if missing.
4. **Enable G2** in `run_depth2_parity.py` against exported manifest; add CI gate from `chart_backtester_parity_gates.md` Gate D.
5. **Do not touch** `pd1_exact_density_trp_adapter.js`, Pool SSOT, or Original Short V2 without explicit phase.

**Estimated touch surface:** new `tools/…/density_provider_v1.py`, changes to `run_phase4d_backtest.py` / future market engine loader, tests only in `phase_bt_d2_*` until approved.

---

## 10. Why not `DEPTH2_ALREADY_ENFORCED` or `DEPTH2_BLOCKED`

- **Not ALREADY_ENFORCED:** Config label `min_depth: 2` does not equate to chart Exact-Density band SSOT per bar; no manifest, no fail-closed, pilot data span ≠ research span.
- **Not BLOCKED:** Canonical chain, reference artifact, Phase 1B/2A adapter reuse, and publish completion make integration **feasible** without new algorithms.

---

## References

- `reports/DOGEUSDT_5M_PD1_DEPTH2_LANDSCAPE_PARITY_AUDIT_V1.md` — `DEPTH2_LANDSCAPE_PARITY_READY`
- `reports/DOGEUSDT_PHASE4W_CHART_BACKTESTER_DATA_PARITY_AUDIT_V1.md`
- `results/phase4xac_verified_may_publish_preparation_v1/chart_backtester_parity_gates.md`
- `reports/PHASE_DENSITY_CHART_PARITY_20260925_1100.md`

**STOP** — report complete; no backtester modifications in this phase.
