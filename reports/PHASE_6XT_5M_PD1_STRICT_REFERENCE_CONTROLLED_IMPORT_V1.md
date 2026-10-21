# PHASE 6XT — 5M PD1 STRICT REFERENCE CONTROLLED IMPORT V1

**VERDICT:** `5M_PD1_CONTROLLED_IMPORT_PASS`

**Scope:** DOGEUSDT `5m` PD1 — **2592** closes (`2026-09-09T00:00:00Z` … `2026-09-17T23:55:00Z`), byte-identical staging → catalog `production/results/2026-09/5m_PD1/`, provenance index registration. **No** ClickHouse import, **no** publish ledger, **no** coverage-matrix change, **no** queue change.

Artifacts:

`logs/historical_import/overnight_2026/phase6xt_5m_pd1_strict_reference_controlled_import_v1/`

- `preflight.json` (blocked attempt — see §2)
- `import_result.json`
- `baseline_15m_before.json` / `baseline_15m_after.json`
- `provenance_index.json.backup`

Reference plan: `reports/PHASE_6XT_5M_PD1_STRICT_REFERENCE_PILOT_PLAN_V1.md`

---

## 1. Executive summary

| Item | Result |
|------|--------|
| Catalog root | `doge_apr_sep_2026_v1_history` |
| Source (read-only) | `doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1/` |
| Target | `…/doge_apr_sep_2026_v1_history/production/results/2026-09/5m_PD1/` |
| Files in scope | **2592/2592** present |
| Staging ↔ target SHA256 | **2592/2592** match |
| `provenance_lookup.json` | **2592** keys for scope; **unchanged** this run (SHA256 stable) |
| `provenance_index.json` | **+2592** entries registered (batch below) |
| 15m publish line | **17568** before/after |
| 5m publish line `…_5mpd1_v1` | **0** before/after |
| `coverage_matrix.json` | **unchanged** (SHA256 stable) |

**Note:** At the first preflight in this session, all **2592** target files and lookup keys **already existed** (likely from an earlier copy outside this report). That run **stopped** per rule “do not overwrite.” A second pass **re-validated** every file against staging and **only appended** `provenance_index.json` (lookup was already complete).

---

## 2. Preflight

### 2.1 First attempt — `5M_PD1_CONTROLLED_IMPORT_BLOCKED`

- Batch id (unused): `phase6xt_5m_pd1_strict_reference_sep_window_v1_95f71a55-31d4-4f36-aeb4-e0c7f059ae31`
- Failure: every scope stem already in `provenance_lookup` and on disk under `production/results/2026-09/5m_PD1/` → no silent overwrite.

### 2.2 Final preflight — PASS

| Check | Result |
|-------|--------|
| Close count / grid | **2592**, 5m step, no gaps |
| Context / visible split | **2016** + **576** |
| Source files | **2592/2592** |
| Duplicates | **0** |
| `frozen_input.snapshot_id` | **`fe549309…`** (all parsed in validation pass) |
| Dest vs source hash | **0** mismatches |
| Lookup vs file hash + frozen id | **2592/2592** OK |
| Index duplicate `dest` | **0** (before append) |
| 15m CH count | **17568** |
| 5m line empty | **0** rows |
| `config_hash` at ref close | **`4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9`** |

---

## 3. Write scope performed

| Action | This run |
|--------|----------|
| Byte copy `copy2` | **No** (files already present; hashes verified) |
| `provenance_lookup.json` | **No write** |
| `provenance_index.json` | **+2592** entries, `copied_count` **44654 → 47246** |
| `coverage_matrix.json` | **No write** |
| ClickHouse | **No write** |

---

## 4. Batch / provenance

| Field | Value |
|-------|--------|
| **Batch UUID** | `13d8eb76-1016-4f55-aa48-587f05967465` |
| **import_batch_id** | `phase6xt_5m_pd1_strict_reference_sep_window_v1_13d8eb76-1016-4f55-aa48-587f05967465` |
| **catalog_id** | `doge_apr_sep_2026_v1` |
| Index entries added | **2592** (each with `import_batch_id`, `batch_uuid`, `catalog_id`) |
| `batches[]` record | Appended with scope metadata and note `files already on disk and in provenance_lookup` |

Each index row follows existing P4/P5 shape: `COPY_VERIFIED`, `source_catalog` `doge_multi_month_2026_history`, `source_frozen_id` / `source_frozen_id_expected` = `fe549309…`, `canonical_input_id` = `84c22453…`.

**Sample target path / hash (pilot window start):**

- `production/results/2026-09/5m_PD1/20260909T000000Z.json` — SHA256 verified equal to staging twin (full set in validation loop).

---

## 5. Post-validation

| Check | Result |
|-------|--------|
| Files on disk | **2592/2592** |
| Hash vs staging | **2592/2592** |
| Index rows for scope | **2592/2592** (`dest` keys) |
| Lookup verify (spot) | `ProvenanceLookup.verify_file` OK on sample stems |
| Visible tile closes | **576/576** files present |
| Context closes | **2016/2016** files present |
| 15m baseline | Unchanged — see §6 |
| Foreign prod trees | 15m / 4h paths not modified |
| Queue / CH 5m publish | Unchanged |

---

## 6. 15m baseline (ClickHouse read-only)

| Metric | Before | After |
|--------|--------|-------|
| `…_15mpd1_v1` publish count | **17568** | **17568** |
| `…_5mpd1_v1` publish count | **0** | **0** |
| Ref `2026-09-14T21:45:00Z` snapshot_id | `3c6bacc268daa59b24f239fb3ad804cb4dc45a53a1b6a92536055f66d7d0dc4c` | same |
| Ref active_state_fingerprint | `71cc7b3ee8f4f0ea7e802fab08eead62c96d30d6a567f5d4e5365b960e9415a0` | same |
| Ref prefix_candle_fingerprint | `4da21e346a9b4b014bbeb97594611423` | same |
| config_hash at ref | `4aeb36fe…` | same |

---

## 7. Side effects and risks

| Item | Status |
|------|--------|
| Production 15m JSON files | **Unchanged** |
| LMV2 engine / generator | **Not run** |
| Viewer / dashboard | **Not switched** |
| Phase 5S SSOT | **Not touched** |
| Historical import queue | **Not touched** |

**Risks / follow-ups:**

- **CH import** for `…_5mpd1_v1` is still **out of scope** — files + file provenance index only.
- **Lookup vs index:** Lookup already contained all 2592 paths before index append; if lookup was built by a prior full-tree rebuild, index is now aligned for `validate_provenance` / `build_provenance_lookup_entries` consumers.
- **Re-run idempotency:** Re-executing index append would hit `index_already` and must **STOP** (by design).

---

## 8. Resume procedure

1. Read `import_result.json` and `copy_log.jsonl` (if present from a future partial copy).
2. For each stem in the 2592 window: if dest missing → `copy2` from staging + log line; if dest exists → compare SHA256 to staging; **never** overwrite on mismatch.
3. Only when **2592/2592** hashes match and lookup keys OK → append **missing** index rows (single batch UUID).
4. Do **not** register index if file count &lt; 2592.

---

## 9. Verdict

**`5M_PD1_CONTROLLED_IMPORT_PASS`**

Catalog files for the approved September 5m-PD1 strict-reference window are in place and provenance-indexed under batch **`13d8eb76-1016-4f55-aa48-587f05967465`**. No ClickHouse publish, no viewer activation.

**STOP** — await separate freigabe for CH batch import / viewer if desired.
