# PHASE 6XT – 4H PD1 IMMUTABLE INPUT COVERAGE ROOT CAUSE V1

**VERDICT:** `LEGITIMATE_INPUT_COVERAGE_GAP`  
(Secondary: **A** — Queue/pilot reused 5m April-head snapshot selection without 4h coverage preflight.)

**Classification (Phase E):** **C** — Original LMV2/TRP requires a longer **1m** window for **4h + PD1** than any frozen DOGE snapshot provides. **Not B** (unix math is consistent). **Not** an accidental units bug.

---

## Executive summary

Gate B for `4h_pd1` fails with `ImmutableInputCoverageError` because the engine requests **1m candles from 2024-04-20 20:00 UTC** through **2026-04-11 00:00 UTC**, while pilot snapshot **`84c22453…`** only covers **2025-09-30 20:00 UTC** → **2026-10-01 00:00 UTC**.

This is **expected behavior** of `CanonicalCandleSession.load_for_window()` when `timeframe="4h"` and `pool_detail=1` (large `warmup_bars × 4h` lookback). **No repository frozen snapshot** spans the required start (verified by scanning `*.manifest.json` under `live_micro_lld_v2_ch_test`).

**5m / 15m** jobs succeeded with the same snapshot family because **`tf_sec` is smaller** (300 / 900), so the same `warmup_bars` only pull **days** of extra 1m history, not **~720 days**.

---

## Confirmed state

| Item | Value |
|------|--------|
| Queue job | `4h_pd1` BLOCKED after Gate B |
| Pilot decision close | `2026-04-11T00:00:00Z` |
| Snapshot | `84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93` |
| Worker exit | 1, 0/1 closes generated |
| Error interval (unix) | `[1713643200, 1775865600]` |
| Snapshot cover (unix) | `[1759262400, 1790812740]` |

---

## Phase A – Call chain to `1713643200`

### 1. Queue / pilot plan

`gates/4h_pd1/gate_b_plan.json` → `run_multi_month.py` `run_worker()` (~527–537):

- `decision_close` = 2026-04-11T00:00:00+00:00  
- `history_from_close` = `rolling_history_from_close(decision, 4)` → **2025-12-11T00:00:00+00:00**  
- `ReusedSnapshotSession(..., timeframe="4h")`  
- `build_active_state_at_decision_close(..., candle_session=session)`

### 2. `active_snapshot/extract.py` (lines 108–111)

Delegates to `candle_session.load_for_window(history_from_close, decision_close, pool_detail)`.

### 3. `canonical_candle_session.py` `load_for_window` (lines 339–357)

For PD1 (`build_live_micro_lld_v2_config(1)`):

| Step | Formula | Value (pilot) |
|------|---------|----------------|
| `warmup_bars` | `volume_lookback + swing_len + 64` | **3594** (3500+30+64, PD1) |
| `tf_sec` | `_timeframe_seconds("4h")` | **14400** |
| `from_unix` | `history_from_close` | 1765411200 (2025-12-11) |
| `to_unix` | `decision_close` | 1775865600 (2026-04-11) |
| `load_start` | `from_unix - warmup_bars * tf_sec` | 1713806400 → 2024-04-21 20:00 UTC* |
| TRP `need` | `expected_source_bars("1m", "4h")` | **240** |
| `src_start_unix` | `load_start - need * 60` | **1713643200** → **2024-04-20 20:00 UTC** |

\*Small drift vs table due to integer truncation order; matches error message bounds.

`ImmutableSnapshotCandleSession._fetch_1m_range` (`immutable_snapshot_candle_session.py` 62–68) raises when `fetch_lo < _cover_lo`.

### 4. Parallel path: `phase11l/frozen.py` `required_source_start_unix` (35–43)

Same arithmetic (used in preflight elsewhere); identical `src_start` for this pilot.

### Components distinguished

| Mechanism | Role in pilot |
|-----------|----------------|
| **4 calendar months** | Sets `history_from_close` = 2025-12-11 |
| **Indicator warmup** | `3594` bars × **4h** bar width → ~597 days before history_from |
| **TRP 1m aggregation** | **240** extra 1m minutes before `load_start` |
| **Origin persistence** | Not the driver of the 2024 date (pool state uses loaded candles) |
| **168h shortcut** | Not used (`rolling_history_from_close(decision, 4)` only) |

---

## Phase B – Reference comparison (2026-04-11 pilot close)

Required `src_start_unix` vs snapshot `84c22453` (cover lo **2025-09-30 20:00 UTC**):

| Combo | TRP 1m need | warmup_bars | tf_sec | src_start (UTC) | Fits 84c22453? |
|-------|-------------|-------------|--------|-----------------|----------------|
| 5m PD1 | 5 | 3594 | 300 | 2025-11-28 12:25 | **Yes** |
| 5m PD5 | 5 | 2076 | 300 | 2025-12-03 18:55 | **Yes** |
| 5m PD10 | 5 | 366 | 300 | 2025-12-09 17:25 | **Yes** |
| 15m PD1 | 15 | 3594 | 900 | 2025-11-03 13:15 | **Yes** |
| 4h PD1 | 240 | 3594 | 14400 | **2024-04-20 20:00** | **No** |
| 4h PD5 | 240 | 2076 | 14400 | **2024-12-29 20:00** | **No** |
| 4h PD10 | 240 | 366 | 14400 | 2025-10-10 20:00 | **Yes** |

**Conclusion:** 4h is not “broken” relative to 5m; **PD1/PD5 on 4h legitimately need far more 1m history** because warmup is measured in **decision-timeframe bar seconds**. Production has **no** `4h_PD1` JSON tree under `doge_multi_month_2026_history/production/results` (0 files) — consistent with never having had covering immutable input.

---

## Phase C – Input coverage

### Snapshot `84c22453…` (pilot)

- `range_start`: 2025-09-30T20:00:00Z  
- `range_end_exclusive`: 2026-10-01T00:00:00Z  
- `row_count`: 525840 (1m closed)  
- Hashes: match `gate_b_plan.json` `price_hash` / `revision_hash`

### Other frozen inputs (documented only)

| Snapshot | range_start | range_end_exclusive | Notes |
|----------|-------------|---------------------|--------|
| `cfae2571…` (multi-month) | 2025-12-11 | 2026-09-01 | **Shorter** than 84c22453 on the left — **worse** for 4h PD1 |
| `fe549309…` (September) | September slice | — | Same class as 84c22453 tail |

**Repo scan:** **no** `DOGEUSDT` `1m` manifest with `range_start <= 2024-04-20T20:00:00Z` and end after pilot decision.

### Why April pilot uses data starting 2025-09-30

The snapshot was built for **5m April-head / September** staging (TRP warmup for **5m** only needs ~2 months of extra 1m before Dec 2025 history_from). It was **never** sized for **4h PD1**’s ~**720-day** 1m span.

### Feasibility of planned 255 `4h_PD1` closes (queue manifest)

Against **only** `84c22453` cover:

- **4h PD1:** **0 / 1638** candidate 4h closes in Jan–Sep 2026 would fit  
- **4h PD5:** **0 / 1638**  
- **4h PD10:** **1098 / 1638** (partial — not the full 255 policy slice without per-close check)

**After root-cause clarity:** the **255-close 4h PD1 staging plan is not causally generable** on existing immutable inputs without **new 1m collection / export** (or abandoning immutable-only policy — out of scope).

---

## Phase D – Historical correctness

- **LMV2 engine code:** unchanged in this investigation (read-only).  
- **4m rolling history:** applied correctly.  
- **No warmup shortening** was applied (failure is honest `ImmutableInputCoverageError`).  
- **No lookahead:** window ends at `decision_close`.  
- **Kausalität:** fixing by swapping snapshot without extending 1m backward would **violate** immutable-session guarantees; any fix must **extend frozen 1m coverage** or **restrict 4h PD1 scope** to closes that fit (none for PD1 on current inputs).

---

## Phase E – Decision matrix

### Primary: **C — Legitimate LMV2 lookback, insufficient immutable coverage**

| Field | Detail |
|-------|--------|
| Root cause files | `canonical_candle_session.py` **350–357**; `phase11l/frozen.py` **35–43**; raise `immutable_snapshot_candle_session.py` **65–68** |
| Parameters | PD1 `warmup_bars=3594`, `tf_sec=14400`, TRP `need=240` |
| Reproduction | Run Gate B plan or trace script in investigation log |
| Minimal fix (proposal only) | Export new immutable 1m snapshot with `range_start <= 2024-04-20T20:00:00Z` through `>= 2026-10-01` for pilot window; re-run Gate A/B on new `snapshot_id`; **do not** reuse `84c22453` for 4h PD1/PD5 |
| Impact 4h PD1/PD5/PD10 | PD1/PD5 **blocked** on all current frozen inputs; PD10 **partially** feasible on `84c22453` |
| Causality risk | Using wrong/shorter snapshot → silent wrong candles (immutable path prevents this by failing) |
| Data collection | **Yes** — ~**720+ days** of 1m for worst-case 4h PD1 closes unless scope is reduced to PD10-only on existing inputs |

### Secondary: **A — Pilot / queue configuration**

`prepare_overnight_manifest_4h.py` + `run_pilot_gates_for_job.py` reused **5m April-head** snapshot (`84c22453`) and Gate B ISO **2026-04-11** without `required_source_start_unix` / manifest coverage check for **`timeframe=4h`**. Gate A only checked production SHA256 samples, not TRP window.

---

## Artifacts (companion)

Investigation outputs referenced from:

`logs/historical_import/overnight_2026/phase6xt_single_worker_auto_nohup_queue_v1/gates/4h_pd1/`

Worker trace:

`staging/phase6xt_4h_pd1_jan_sep_v1/workers/worker_0_status.json`

---

## STOP

No queue resume, no regeneration, no LMV2 edits, no CH writes in this phase. Repair requires **separate operator approval** (likely: new frozen input + updated 4h gate preflight).
