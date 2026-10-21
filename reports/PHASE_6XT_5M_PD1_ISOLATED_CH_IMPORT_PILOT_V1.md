# PHASE 6XT — 5M PD1 ISOLATED CH IMPORT PILOT V1

**VERDICT:** `5M_PD1_CH_PILOT_PASS`

**Scope:** One batch of **50** chronological 5m PD1 closes → ClickHouse line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`. No second batch, no viewer/SSOT changes.

Artifacts:

`logs/historical_import/overnight_2026/phase6xt_5m_pd1_isolated_ch_import_pilot_v1/pilot_run.json`

---

## 1. Minimal import-guard diff

**File:** `backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/historical_batch_config.py`

Aligned with spread dashboard — **only** isolated `5m_PD1` recognition in `combo_for_folder` (no change to `STRATEGY_COMBOS`, no coverage-matrix logic, no provenance rules):

```python
# Isolated import combos (excluded from bulk STRATEGY_COMBOS dry-runs).
ISOLATED_IMPORT_COMBOS: tuple[StrategyCombo, ...] = (StrategyCombo("5m", 1),)

def combo_for_folder(folder: str) -> StrategyCombo | None:
    for c in (*STRATEGY_COMBOS, *ISOLATED_IMPORT_COMBOS):
        if c.folder == folder:
            return c
    return None
```

**Not imported** from spread: Gate-A May constants (`PHASE6XT_5M_PD1_PILOT_*` for `20260501T000000Z`) — out of pilot scope.

---

## 2. Read-only tests (pre-write)

| Check | Result |
|-------|--------|
| `combo_for_folder("5m_PD1")` | `StrategyCombo("5m", 1)` |
| `pytest` `test_batch_import_phase4a` (gates, validate paths) + `test_historical_lmv2_pd_contract_v1` | **13 pass**; `test_catalog_inventory_matches_manifest` **fail** — disk count **56394** vs fixture **53802** (= **+2592** Sep `5m_PD1` catalog files from controlled file import; **not** caused by guard diff) |
| All **2592** stems on disk + `validate_bundle` | **0** errors |
| Frozen input | **`fe549309…`** only |
| `validate_batch` (50 stems) | **0** errors |
| 5m publish line before write | **0** |
| 15m publish line before write | **17568** |
| Ref 15m `2026-09-14T21:45:00Z` | Unchanged (see §8) |

Env gates (required for write): `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`, `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`.

---

## 3. Pilot batch (write)

| Field | Value |
|-------|--------|
| **import_batch_id** | `phase6xt_5m_pd1_ch_import_pilot_v1_77f0bcf8-08a1-40c3-84e5-d1eaf694a271` |
| **batch_uuid** | `77f0bcf8-08a1-40c3-84e5-d1eaf694a271` |
| **test_run_id** | same UUID |
| **pipeline_line_id** | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| **month / combo** | `2026-09` / `5m_PD1` |
| **Close count** | **50** |
| **First stem** | `20260909T000000Z` (`2026-09-09T00:00:00Z`) |
| **Last stem** | `20260909T040500Z` (`2026-09-09T04:05:00Z`) |
| **Pipeline** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |
| **Wall time** | ~**23 s** |

### 3.1 Stems (50)

`20260909T000000Z` … `20260909T040500Z` (5m steps, full list in `pilot_run.json` → `stems`).

### 3.2 Target tables (existing schema)

| Table | Role |
|-------|------|
| `research_lmv2_active_snapshot_meta_v1` | Meta |
| `research_lmv2_active_snapshot_member_v1` | Members |
| `research_lmv2_snapshot_active_pool_v1` | Catalog |
| `research_lmv2_snapshot_import_provenance_v1` | Sidecar |
| `research_lmv2_active_snapshot_publish_v1` | Publish ledger |

### 3.3 Expected vs actual row counts (batch)

| Rows | Planned (`summarize_plans`) | ClickHouse (sum over 50 `snapshot_id`s) |
|------|---------------------------|----------------------------------------|
| publish / meta / sidecar | **50** | **50** |
| member / catalog | **9341** | **9341** |

---

## 4. Source provenance

- **Catalog:** `doge_apr_sep_2026_v1_history`
- **Paths:** `production/results/2026-09/5m_PD1/{stem}.json`
- **Lookup/index:** from controlled import batch `13d8eb76-…` (file phase)
- **Sidecar:** `catalog_manifest_id` = `doge_apr_sep_2026_v1`, `provenance_lookup_version` from live lookup file

---

## 5. ClickHouse readback

- `run_batch_readback_gate`: **50/50** parity reports `ok=True` (informational `provenance:relative_dest=…` notes only).
- Sidecar readback: completed for batch (`require_sidecar=True`).
- **Idempotency:** Re-run same `BatchSpec` would hit `resolve_existing_publish` skip path for same snapshot/fingerprint.

---

## 6. 15m baseline (after)

| Metric | Before | After |
|--------|--------|-------|
| `…_15mpd1_v1` publish | **17568** | **17568** |
| `…_5mpd1_v1` publish | **0** | **50** |
| Ref 15m snapshot / fingerprint | `3c6bacc2…` / `71cc7b3e…` | **unchanged** |

---

## 7. Side effects

| Item | Changed? |
|------|----------|
| 15m publish/catalog | **No** |
| Other pipeline lines | **No** |
| `coverage_matrix.json` | **No** |
| Viewer / landscape / SSOT | **No** |
| Remaining **2542** closes on 5m line | **Not imported** (by design) |

---

## 8. Recommendations (full import — separate freigabe)

1. Continue **51 remaining batches** (50+50+…+42) on same line with **new** `import_batch_id` per orchestration policy; readback after each batch.
2. Keep `ISOLATED_IMPORT_COMBOS` only — do not add 5m to bulk `STRATEGY_COMBOS` dry-runs without review.
3. Update or supersede `test_catalog_inventory_matches_manifest` disk total when 5m Sep files are in catalog (fixture drift).
4. **Chart binding** still requires SSOT/viewer work (`5M_PD1_PIPELINE_CHANGES_REQUIRED`); CH data alone does not switch the research chart to 5m PD1 pools.

---

## 9. Verdict

**`5M_PD1_CH_PILOT_PASS`**

**STOP** — no batch 2, no viewer, no full 2592 import without explicit approval.
