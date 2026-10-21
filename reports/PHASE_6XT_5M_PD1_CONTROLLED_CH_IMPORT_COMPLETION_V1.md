# PHASE 6XT — 5M PD1 CONTROLLED CH IMPORT COMPLETION V1

**VERDICT:** `5M_PD1_CH_IMPORT_COMPLETE_PASS`

Artifacts:

`logs/historical_import/overnight_2026/phase6xt_5m_pd1_ch_import_completion_v1/`

- `run_completion_import.py`, `run.log`, `progress.json`, `batches.jsonl`
- `baseline_start.json`, `baseline_end.json`, `final_validation.json`

Prior: pilot `phase6xt_5m_pd1_isolated_ch_import_pilot_v1/` (50 closes).

---

## 1. Inventory test fix

**Test:** `research_charts/tests/test_batch_import_phase4a.py::test_catalog_inventory_matches_manifest`

| Assertion (before) | Issue |
|--------------------|--------|
| `disk_total_json == 53802` | **56394** on disk after adding **2592** Sep `5m_PD1` JSON files under `production/results/` |

**Root cause:** `catalog_manifest.json` and `coverage_matrix.json` still describe **bulk** combos only (**53802**). Isolated **5m_PD1** September files were copied in the controlled **file** import without updating the coverage matrix (by design — avoids `MISSING != 0` aborting mixed catalog discovery).

**Minimal fix:** Keep manifest/coverage at **53802**; assert `disk_total_json == 53802 + 2592` and `by_combo_disk["5m_PD1"] == 2592`. No integrity checks removed.

---

## 2. Test results (pre-import)

| Suite | Result |
|-------|--------|
| `test_batch_import_phase4a.py` + `test_historical_lmv2_pd_contract_v1.py` + `test_phase4k_prewrite_and_multibatch.py` | **26 passed** |

---

## 3. Preflight

| Check | Result |
|-------|--------|
| Plan closes | **2592** (`2026-09-09T00:00:00Z` … `2026-09-17T23:55:00Z`) |
| `validate_bundle` all stems | **0** errors |
| Frozen input | **`fe549309…`** |
| Pilot already in CH | **50** publishes |
| Todo before completion run | **2542** |
| 15m line before | **17568** |
| 5m line before | **50** |
| Write gates | `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`, `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1` |

---

## 4. Batch execution

| Item | Value |
|------|--------|
| **Orchestration** | `run_completion_import.py` |
| **test_run_id (stable)** | `a8f3c2e1-4b5d-4a6e-9f0c-1d2e3f4a5b6c` |
| **import_batch_id pattern** | `phase6xt_5m_pd1_ch_import_completion_v1_{run_uuid}_batch{NN}` |
| **Batches run (completion)** | **51** (50×50 + 1×42) |
| **Wall time** | ~**856 s** (~14.3 min) |
| **Per-batch** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |
| **Failures** | **0** (stopped on first failure — none) |

**First completion batch:** stems `20260909T041000Z` … (continues after pilot `…040500Z`).  
**Last completion batch:** ends at `20260917T235500Z` (42 closes).  
Full per-batch log: `batches.jsonl` (**51** lines).

**Pilot batch (unchanged):** `phase6xt_5m_pd1_ch_import_pilot_v1_77f0bcf8-…` — 50 closes `20260909T000000Z`–`20260909T040500Z`.

---

## 5. Imported closes (total)

| Segment | Closes |
|---------|--------:|
| Context (before 2026-09-16 00:00Z) | **2016** |
| Visible tile (2026-09-16 … 2026-09-17 23:55Z) | **576** |
| **Total publish on `…_5mpd1_v1`** | **2592** |

---

## 6. ClickHouse readback

**Full-line readback** (`run_pilot_readback`, all **2592** stems, CH parity):

| Metric | Result |
|--------|--------|
| Failed stems | **0** |
| Unique `snapshot_id` | **2592** |

---

## 7. Member / catalog totals

| Layer | ClickHouse | Expected (from JSON plans) |
|-------|------------|----------------------------|
| publish / meta / sidecar | **2592** each | **2592** |
| member / catalog | **440098** each | **440098** |

---

## 8. Provenance

- Catalog files + `provenance_lookup.json` / `provenance_index.json` (file phase, batch `13d8eb76-…`).
- CH sidecar: **2592** rows (`research_lmv2_snapshot_import_provenance_v1`), one per snapshot.

---

## 9. 15m baseline

| Metric | Before completion | After |
|--------|-------------------|-------|
| `…_15mpd1_v1` publish | **17568** | **17568** |
| Ref `2026-09-14T21:45:00Z` | `3c6bacc2…` / `71cc7b3e…` | **unchanged** |

---

## 10. Side effects

| Item | Changed? |
|------|----------|
| Viewer / landscape / SSOT | **No** |
| `coverage_matrix.json` | **No** |
| Other pipeline lines | **No** |
| `historical_batch_config` (prior phase) | `ISOLATED_IMPORT_COMBOS` only |

---

## 11. Recovery / idempotency

- Progress: `progress.json` + append-only `batches.jsonl`.
- Resume: re-run script reads CH `published_stems`, imports only **missing** stems; `resolve_existing_publish` skips completed snapshots.
- **No duplicate publishes:** final **2592** unique snapshot IDs = **2592** closes.

---

## 12. Verdict

**`5M_PD1_CH_IMPORT_COMPLETE_PASS`**

**STOP** — no viewer switch, no SSOT/rendering changes, no further historical scope without new freigabe.
