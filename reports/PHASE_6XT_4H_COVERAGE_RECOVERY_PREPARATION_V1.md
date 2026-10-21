# PHASE 6XT — 4H FROZEN INPUT COVERAGE RECOVERY PREPARATION V1

**VERDICT:** `RECOVERY_REQUIRES_NEW_1M_INPUT`  
(Partial exception documented: **4h PD10** planned 255 closes can use **existing** `84c22453…` only — see §7.)

**Read-only phase.** No imports, exports, queue resume, LMV2 changes, MASTER/CH writes, or snapshot regeneration.

Companion machine artifacts:

`logs/historical_import/overnight_2026/phase6xt_4h_coverage_recovery_prep_v1/`

- `source_inventory_1m.json`
- `planned_4h_coverage_summary.json`
- `global_4h_required_window.json`
- `matrix_4h_PD{1,5,10}_compact.json`

Prior root cause: `reports/PHASE_6XT_4H_INPUT_COVERAGE_ROOT_CAUSE_V1.md`

---

## 1. Executive summary

Outstanding **4h PD1** and **4h PD5** historical LMV2 staging (255 closes each, April-head + September per queue manifest) **cannot** run on any **existing immutable DOGEUSDT 1m** snapshot. Canonical `required_source_start_unix(decision, pd, "4h")` demands **1m data from 2024** (PD1) / **late 2024** (PD5) through each decision close; the longest frozen input (`84c22453…`) begins **2025-09-30 20:00 UTC**.

**ClickHouse** `signal_generator.candles_1m` (read-only probe) holds DOGEUSDT closed 1m from **2025-07-25 13:15** through **2026-10-19** (~649k rows). It does **not** close the 2024 gap. No Binance 1m file bundles were found under this repo (limited search).

**Recovery path:** backfill missing **Bybit linear 1m** into CH (or a dedicated export pipeline) for the proven gap, then **one new immutable export** covering at least **`2024-01-10T20:00:00Z` → `2026-10-01T00:00:00Z` exclusive** (worst-case PD1 over Jan–Sep 2026 4h grid), with manifest + parquet freeze per `immutable_candle_input_v1`. **4h PD10** can proceed on **`84c22453` alone** without new input if queue scope is split — but **PD1/PD5 block a single combined “4h queue”** without new 1m.

---

## 2. Source inventory (evidence-based)

### 2.1 Immutable snapshots (DOGEUSDT 1m, `live_micro_lld_v2_ch_test`)

| snapshot_id (prefix) | range_start | range_end_exclusive | rows | parquet | Role today |
|----------------------|-------------|---------------------|------|---------|------------|
| **84c22453** | 2025-09-30T20:00Z | 2026-10-01T00:00Z | 525 840 | ~11.0 MiB | 5m April-head + Sep; **4h PD10** |
| **cfae2571** | 2025-12-11T00:00Z | 2026-09-01T00:00Z | 380 160 | ~7.6 MiB | 5m multi-month (shorter left edge) |
| **fe549309** | 2026-02-28T20:00Z | 2026-10-01T00:00Z | 308 400 | ~6.4 MiB | 5m September segment |
| **dba0ebaf** (phase11r) | 2026-05-23T05:25Z | 2026-09-30T12:01Z | 187 596 | ~4.2 MiB | Research pilot only |
| **de093fe1** (phase11o) | 2026-03-25T12:00Z | 2026-09-30T12:01Z | 272 161 | ~5.7 MiB | Benchmark |
| **2193f99b** (phase11q) | 2026-09-24 (6h) | — | 360 | ~16 KiB | Micro window |

All listed immutables: `snapshot_format_version=immutable_candle_input_v1`, `COMPLETE`, manifest checksums on disk. **None** extend before **2025-09-30 20:00 UTC**.

Full list: `source_inventory_1m.json` (10 manifests under `live_micro_lld_v2_ch_test`).

### 2.2 ClickHouse (read-only, not immutable)

| Metric | Value |
|--------|--------|
| Table | `signal_generator.candles_1m` |
| Filter | `bybit`, `DOGEUSDT`, `1m`, `is_closed=1` |
| min(open_time) | **2025-07-25 13:15 UTC** |
| max(open_time) | **2026-10-19 13:04 UTC** (probe time) |
| count | **649 430** |
| Rows before `84c22453` range_start | **96 885** (Jul–Sep 2025 gap segment; aligns with prior backfill pilots) |

CH is **mutable** (live collector + backfill). Immutable session **refuses CH fallback** — exports must be frozen separately.

### 2.3 Other sources

| Source | Finding |
|--------|---------|
| Binance 1m files in `pools+ob+delta_bot` | **None found** (glob) |
| Spread-recovery / Freqtrade | Not `candles_1m`; documented in prior forensics — **not usable** for immutable LMV2 session without new pipeline |
| MASTER DB | **Not queried** (out of scope; no writes) |

**Gap:** No verified offline 1m store covering **2024** was discovered in-repo.

---

## 3. Coverage matrix methodology

- **Reference logic:** `phase11l/frozen.py` → `required_source_start_unix(decision_close, pool_detail, "4h")` (same as `canonical_candle_session.load_for_window` TRP + warmup).
- **History-from:** `rolling_history_from_close(decision, 4)` (4 calendar months).
- **PASS:** ∃ immutable DOGE snapshot with `[cover_lo, cover_hi]` ⊇ `[required_source_start_unix, decision_close_unix]`.
- **Planned closes:** 255 per combo from staging manifests `phase6xt_4h_pd{1,5,10}_jan_sep_v1` (April-head + September; May–Aug skipped in prod).

### 3.1 Planned queue scope (255 closes each)

| Combo | PASS (any immutable) | PASS on **assigned** plan snapshots (84c22453 / fe549309) | Worst-case `required_source_start` |
|-------|----------------------|-----------------------------------------------------------|-------------------------------------|
| **4h PD1** | **0 / 255** | **0 / 255** | **2024-04-20T20:00:00Z** (pilot close 2026-04-11) |
| **4h PD5** | **0 / 255** | **0 / 255** | **2024-12-29T20:00:00Z** |
| **4h PD10** | **255 / 255** | **255 / 255** (84c22453 covers full plan) | **2025-10-10T20:00:00Z** (within 84c22453) |

### 3.2 Full Jan–Sep 2026 4h grid (1638 closes / PD) — global envelope

| PD | Earliest `required_source_start` (any close in grid) | Latest in grid |
|----|------------------------------------------------------|----------------|
| 1 | **2024-01-10T20:00:00Z** | 2024-10-08T16:00:00Z |
| 5 | **2024-09-19T20:00:00Z** | 2025-06-18T16:00:00Z |
| 10 | **2025-07-01T20:00:00Z** | 2026-03-30T16:00:00Z |

**Single export must cover (union):** **`2024-01-10T20:00:00Z` ≤ t < `2026-10-01T00:00:00Z`** (decision end + TRP margin) for all three PDs if one snapshot serves all 4h lines.

Per-close rows: `matrix_4h_PD*_compact.json` (samples).

---

## 4. Minimal data acquisition (A–G)

| Item | Result |
|------|--------|
| **A. Earliest 1m per PD** | PD1: 2024-01-10T20:00Z; PD5: 2024-09-19T20:00Z; PD10: 2025-07-01T20:00Z |
| **B. Union for all planned 4h** | **2024-01-10T20:00:00Z** (see global JSON) |
| **C. Usable intervals today** | Immutable: ≥ 2025-09-30T20:00Z; CH: ≥ 2025-07-25T13:15Z |
| **D. Missing** | **~2024-01-10 → 2025-09-30T20:00Z** (relative to 84c22453); CH fills **2025-07-25 → 2025-09-30T20:00** subset only |
| **E. Existing data closes gap?** | **No** for PD1/PD5; **yes for PD10-only** on 84c22453 |
| **F. New immutable snapshot required?** | **Yes** for PD1+PD5; **optional** for PD10-only path |
| **G. Minimal download** | Backfill **Bybit** 1m from **2024-01-10T20:00Z** through **2025-09-30T20:00Z** (≈ **562 days**, ~**808k** minutes upper bound) **plus** verify CH continuity through **2026-10-01**; then **one** new export (do not mutate 84c22453). Prefer `--repair-missing` style gap repair documented in `doge_1m_backfill_*` pilots. |

---

## 5. Frozen snapshot strategy (design only)

### 5.1 Proposed new export (not executed)

| Field | Proposal |
|-------|----------|
| **range_start** | `2024-01-10T20:00:00.000Z` (PD1 global min for Jan–Sep 2026 grid) |
| **range_end_exclusive** | `2026-10-01T00:00:00.000Z` |
| **Provenance** | `signal_generator.candles_1m` after controlled Bybit backfill + `FINAL` select (same semantics as existing manifests) |
| **Integrity** | Minute grid audit: missing/duplicate `open_time`; UTC alignment; `is_closed=1`; `file_checksum` + `ohlcv_content_hash` + `revision_metadata_hash` |
| **Immutability** | New `snapshot_id`; **new directory**; never overwrite `84c22453` / `cfae2571` / `fe549309` |
| **Session** | `ImmutableSnapshotCandleSession` / `ReusedSnapshotSession` unchanged |
| **Disk** | Scale: ~1.4M minutes × ~22 bytes/row compressed → **~25–35 MiB** parquet (extrapolated from 525k rows ≈ 11 MiB) + manifest |
| **Download volume** | ~808k minutes historical backfill (if CH empty before Jul 2025) + export read |

### 5.2 PD10-only fast path (optional scope split)

Use existing **`84c22453`** for all **255** planned PD10 closes (verified). **Does not** help PD1/PD5.

### 5.3 Anti-patterns

- Stitching multiple immutables with different `revision_metadata_hash` in one worker plan.
- Pointing 4h PD1 at `84c22453` without extending 1m (proven fail).
- Live CH reads in production historical workers (refused by design).

---

## 6. Coverage preflight (design only — not implemented)

Run **before** any 4h queue job / Gate B:

1. For each `(timeframe, pool_detail, decision_close_iso)` in plan:  
   `src = required_source_start_unix(decision, pd, tf)`; `end = decision_unix`.
2. Assert chosen snapshot manifest: `range_start ≤ src` and `range_end_exclusive > end`.
3. Optional dense check: sample or full minute presence in parquet index for `[src, end]` (session contract).
4. `symbol == DOGEUSDT`, `interval == 1m`, manifest `COMPLETE`.
5. `ohlcv_content_hash` / `revision_metadata_hash` match plan JSON.
6. Fail closed on **UNKNOWN** (missing manifest, unread parquet).
7. **No** warmup/PD/engine overrides on failure.

**Integration point (minimal):** extend queue `run_pilot_gates_for_job.py` / `prepare_overnight_manifest_4h.py` preflight to call `required_source_start_unix` per close against candidate `snapshot_id` **before** spawning `run_multi_month` (same module path as Gate D today).

---

## 7. Impact on other work

| Area | Impact |
|------|--------|
| **5m PD1/PD5/PD10 staging** | **None** — already validated; uses shorter 1m lookback |
| **15m density / Phase 5S SSOT** | **None** — no code or data touched |
| **4h PD10** | **Unblocked** with **`84c22453`** only; production already has partial **4h_PD10** JSON |
| **4h PD1/PD5** | **Blocked** until new 1m immutable input |
| **Historical ACTIVE origins / backtester** | No change; future 4h snapshots must use correct frozen input |

---

## 8. Storage / time estimates

| Step | Estimate |
|------|----------|
| Bybit backfill ~808k minutes | Hours (rate-limited REST); prior 24h pilot ~minutes wall time scaled |
| CH → parquet export | Minutes (single SELECT) |
| 4h PD1 generate 255 closes | Small CPU once input exists (~minutes–tens of minutes) |
| Full PD1 if policy expanded | Much larger than 255 |

---

## 9. Risks and uncertainties

| Risk | Note |
|------|------|
| CH gaps inside Jul 2025–Oct 2026 | Not fully minute-audited in this phase; export must run gap detection |
| Bybit availability before Jul 2025 | Assumed from prior pilot policy; **not re-downloaded** here |
| Binance / other archives | **UNVERIFIED** outside searched paths |
| `fe549309` left edge (Feb 2026) | Irrelevant for PD1/PD5; PD10 September OK on 84c22453 |
| Combining PD10 with PD1 on one snapshot | Forces full 2024 export anyway if one queue input |

---

## 10. Recommendation — smallest next step

1. **Operator approval** for **read-only** CH gap map (`open_time` holes) on `[2024-01-10, 2026-10-01)` — still no export.
2. **Approved backfill pilot** for minimal prefix ending at `2025-09-30T20:00:00Z` (reuse Signal Generator `backfill_bybit_universe.py` policy).
3. **New immutable export** with manifest; Gate A/B on **one** 4h PD1 + PD5 close before queue resume.
4. **Optional parallel track:** run **4h PD10-only** queue on **`84c22453`** under separate approval (no new 1m).

---

## 11. Closure gate (confirmed)

| Statement | Status |
|-----------|--------|
| No productive imports | **Confirmed** |
| No new 1m export executed | **Confirmed** |
| No queue continuation | **Confirmed** |
| No LMV2 math changes | **Confirmed** |
| No MASTER mutation | **Confirmed** |
| No snapshot regeneration | **Confirmed** |

**STOP** — await separate approval for data acquisition, implementation, or import resume.
