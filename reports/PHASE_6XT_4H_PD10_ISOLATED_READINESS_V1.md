# PHASE 6XT — 4H PD10 ISOLATED RECOVERY READINESS V1

**VERDICT:** `4H_PD10_READY_FOR_SEPARATE_APPROVAL`

**Read-only phase.** No snapshot generation, 1m downloads, ClickHouse writes, publishes, queue restarts, or lock manipulation was performed.

Machine artifacts:

`logs/historical_import/overnight_2026/phase6xt_4h_pd10_isolated_readiness_v1/`

- `inventory_summary.json`
- `per_close_coverage.json` (255 rows)
- `isolation_analysis.json`

Prior inputs:

- `reports/PHASE_6XT_4H_COVERAGE_RECOVERY_PREPARATION_V1.md`
- `reports/PHASE_6XT_4H_INPUT_COVERAGE_ROOT_CAUSE_V1.md`

---

## 1. Executive summary

The **255** planned **DOGEUSDT 4h PD10** closes (75 April-head + 180 September, per queue manifest) are **fully covered** by existing immutable 1m inputs. **None** of the 255 stems exist in production or staging today (true gap **255/255**). The blocked auto-queue job **`4h_pd1`** (immutable coverage for PD1) does **not** prevent a **separate** overnight run for **4h PD10 only**, using the established **`overnight_4h_pd_nohup.sh`** + **`overnight_supervisor.py`** path — **without** resuming or reindexing the queue.

**Not yet done (execution prerequisites, not data blockers):** pilot gates A/B/C/resume for `4h_pd10`, and creation of `gate_d_approved_4h_pd10.json`.

---

## 2. Data inventory

| Item | Value |
|------|--------|
| Symbol | DOGEUSDT |
| Timeframe / PD | **4h** / **PD10** (`combo`: `4h_PD10`) |
| Planned closes | **255** (75 × 2026-04-11 … 2026-04-23 head, 180 × 2026-09) |
| Staging root | `doge_multi_month_2026_history/staging/phase6xt_4h_pd10_jan_sep_v1` |
| Staging result files (`4h_PD10`) | **0** |
| Production `4h_PD10` files (all months) | **497** (May–Aug complete months; **0** stem overlap with the 255 plan) |
| Duplicates in plan | **0** |
| Fehlbestand (generate target) | **255** (all missing in both prod and staging) |

### 2.1 Immutable inputs (assigned in plans)

| Segment | Snapshot ID | range_start → range_end_exclusive | price_hash (prefix) | revision_hash (prefix) |
|---------|-------------|-------------------------------------|---------------------|-------------------------|
| April head | `84c22453…` | 2025-09-30T20:00Z → 2026-10-01T00:00Z | `d489aa68…` | `9a3ba306…` |
| September | `fe549309…` | 2026-02-28T20:00Z → 2026-10-01T00:00Z | `6dad41b8…` | `bb2c9d85…` |

Plans (unchanged):

- `…/phase6xt_4h_pd10_jan_sep_v1/run/plan_april_84c22453.json`
- `…/phase6xt_4h_pd10_jan_sep_v1/run/plan_september_fe549309.json`

**Note:** Recovery prep showed **`84c22453` alone** covers all 255 closes for PD10 warmup math; the manifest still assigns **fe549309** for September (same pattern as 5m jobs). Per-close validation: **255/255** pass on **assigned** snapshots and **255/255** pass on **84c22453-only** envelope.

### 2.2 Coverage envelope (PD10 / 4h)

| Metric | Value |
|--------|--------|
| Worst `required_source_start` (255 closes) | **2025-10-10T20:00:00Z** |
| Latest decision close | **2026-09-30T20:00:00Z** |
| `coverage_assigned_pass` | **255 / 255** |
| `coverage_84c22453_only_pass` | **255 / 255** |

Full per-close rows: `per_close_coverage.json`.

---

## 3. Original configuration evidence (Task A)

| Check | Evidence |
|-------|----------|
| Pipeline | `run_multi_month.py` worker + `build_active_state_at_decision_close` (immutable session) — **no alternate generator** |
| Timeframe | `4h` (manifest, plans, `_step_minutes` = 240 in pilot runner) |
| Pool detail | **10** |
| Config hash | **`bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2`** — matches `build_live_micro_lld_v2_config(10)` / manifest |
| Warmup / history | `rolling_history_from_close(decision_close, 4)` (manifest `warmup_policy`) |
| PD10 preset (engine) | `warmup_bars`: **366**; swing_len 2; volume_lookback 300; zone_extension 15; etc. (`preset_matrix_entry(10)`) |
| Candle history | Immutable parquet only (`immutable_candle_input_v1`); **no CH fallback** in session |
| Causal as-of | Decision closes are 4h bar **close** timestamps; loader uses `required_source_start_unix(decision, 10, "4h")` |
| Production guard | `staging/…/run/production_guard_baseline.json` — sample `2026-08/4h_PD10/20260815T120000Z.json` SHA256 frozen at queue build |
| Production sample parity | Existing prod snapshots: `timeframe=4h`, `pool_detail=10`, `history_from_close` = rolling 4 months; **older prod rows may show CH-backed `frozen_input`** — new staging outputs will record immutable `snapshot_id` / price hashes per `_validate_file` (same as successful **5m** staging) |

**ACTIVE-origin / pool bounds:** No staging-specific overrides in `run_multi_month.py` for 4h PD10; engine config comes from `LiveMicroLldV2Config(pool_detail=10)` only.

---

## 4. Task B — 255 closes validation summary

For each planned close, validation recorded:

- `required_source_start` (1m, TRP + PD10 4h warmup)
- decision close as end bound (inclusive minute)
- `history_from_close` (4 calendar months)
- assigned-snapshot coverage flag
- `in_production` / `in_staging` (path: `results/{YYYY-MM}/4h_PD10/{stem}.json`, stem `YYYYMMDDTHHMMSSZ`)
- no overwrite risk: **0** files exist at those paths today

**Duplicates:** none across April + September plans.

---

## 5. Isolation from blocked queue (Task C)

### 5.1 Queue state (observed)

| Field | Value |
|-------|--------|
| `queue_run_id` | `phase6xt_auto_queue_20261020T173116Z` |
| `status` | **BLOCKED** |
| `current_job_index` | **3** → **`4h_pd1`** |
| `jobs_completed` | `5m_pd1_monitor`, `5m_pd5`, `5m_pd10` |
| Block reason | Pilot gates failed — `ImmutableInputCoverageError` (PD1 needs 1m from **2024** on pilot close 2026-04-11) |
| `auto_queue_supervisor` | **not running** |
| Stale `pd1_supervisor_pid` 1245524 | **dead** |
| Global `.historical_lmv2_single_job.lock` | **absent** |
| 4h `overnight.lock` | **none active** |

### 5.2 Safe isolated path

| Requirement | Assessment |
|-------------|------------|
| Keep queue **BLOCKED** | Yes — do **not** call `resume_auto_queue` until PD1 input strategy is approved |
| No job-index / gate bypass | Isolated run uses **`overnight_4h_pd_nohup.sh 10 start`** directly, **not** queue advancement |
| No lock removal | Only stale dead PIDs may be cleaned by existing scripts when starting; no manual lock stripping required now |
| Max one LMV2 worker | `historical_lmv2_safety.sh acquire` sets global lock; supervisor runs single worker plan |
| 5m staging untouched | Separate staging tree `phase6xt_4h_pd10_jan_sep_v1` |
| 15m research chart | No paths touched |

**Caveat:** `historical_lmv2_safety.sh` **only** scans `phase6xt_5m_pd*` locks/workers; 4h isolation relies on **global lock** + per-staging `overnight.lock` in `overnight_4h_pd_nohup.sh`. This matches how 4h was designed in the queue manifest.

**Queue resume is the wrong tool:** Resuming the auto-queue would **retry `4h_pd1`** and never reach `4h_pd10` without a PD1 fix or forbidden index skip.

---

## 6. Safety gates (Task D)

Same toolchain as 5m jobs:

| Gate | 4h PD10 readiness |
|------|-------------------|
| **A — Production guard** | Baseline present under `staging/…/run/production_guard_baseline.json`; supervisor aborts if sample SHA changes |
| **B/C — Pilot + validation** | `run_pilot_gates_for_job.py --timeframe 4h --pool-detail 10` — **not executed yet**; for PD10, gate B close `2026-04-11T00:00:00+00:00` on `84c22453` is **expected to pass** (PD1 failed on same close only due to PD1 warmup) |
| **Resume determinism** | Same script as 5m (delete last pilot file, regenerate, compare SHA256) |
| **D — Operator approval file** | `gate_d_approved_4h_pd10.json` — **missing** until pilots PASS + preflight |
| **E — Scope** | `operator_gate_e_freigabe.json` already lists **`4h_PD10 policy-ok missing staging`** |
| **Overnight** | `overnight_4h_pd_nohup.sh` requires Gate D + `SAFETY_OK` + disk ≥ 15G |
| **Immutable coverage** | Enforced in worker (`ImmutableInputCoverageError`) — pre-validated for all **255** plan closes |
| **Readback** | Queue `auto_queue_supervisor` readback applies to **queue-driven** jobs; isolated run should use same **manifest + file-count** readback pattern as 5m PD10 job report before any publish (publish still **out of scope**) |

**No gates were disabled or weakened in this phase.**

---

## 7. Minimal execution plan (Task E — do not run until approved)

**Scope:** DOGEUSDT, 4h PD10, 255 closes, April-head + September 2026, Original LMV2, existing immutables, staging-only.

1. **Preflight (read-only checks)**  
   - `historical_lmv2_safety.sh check` → `SAFETY_OK`  
   - Confirm queue supervisor **not** running; no foreign `run_multi_month` worker  
   - Confirm `staging/phase6xt_4h_pd10_jan_sep_v1/run/manifest.json` matches inventory (255)

2. **Pilot gates (generates ≤13 pilot snapshots in staging — requires explicit approval)**  
   ```bash
   python3 logs/historical_import/overnight_2026/phase6xt_single_worker_auto_nohup_queue_v1/run_pilot_gates_for_job.py \
     --timeframe 4h --pool-detail 10 \
     --staging backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history/staging/phase6xt_4h_pd10_jan_sep_v1 \
     --out logs/historical_import/overnight_2026/phase6xt_single_worker_auto_nohup_queue_v1/gates/4h_pd10
   ```
   Expect `pilot_verdict.json` → `PASS`.

3. **Gate D file**  
   - Either let a **dedicated** small wrapper write `gate_d_approved_4h_pd10.json` mirroring queue `_write_gate_d` (after pilots), or operator-signed equivalent — **do not** advance `queue_status.json` index.

4. **Overnight (isolated nohup)**  
   ```bash
   logs/historical_import/overnight_2026/phase6xt_single_worker_auto_nohup_queue_v1/overnight_4h_pd_nohup.sh 10 prepare   # if needed
   logs/historical_import/overnight_2026/phase6xt_single_worker_auto_nohup_queue_v1/overnight_4h_pd_nohup.sh 10 start
   ```
   Supervisor: `overnight_supervisor.py` with `--staging-root` → `phase6xt_4h_pd10_jan_sep_v1`, plans = April + September JSON above.

5. **Completion readback**  
   - 255 files under `staging/…/results/**/4h_PD10/`  
   - Spot-check `_validate_file` against plan `price_hash` / `revision_hash`  
   - Production tree unchanged (guard samples stable)

**Explicitly out of scope for this job:** `4h_pd1`, `4h_pd5`, new 1m export, CH writes, production merge, dashboard.

---

## 8. Risk assessment

| Risk | Level | Mitigation |
|------|-------|------------|
| Accidental queue resume | Medium | Keep `queue_status` BLOCKED; use direct `overnight_4h_pd_nohup.sh` only after approval |
| Two concurrent workers | Medium | Global lock + single nohup; verify `pgrep run_multi_month` before start |
| Pilot only exercises `84c22453` | Low | Full plan pre-validated for **fe549309** September segment (180 closes) |
| Stale queue JSON (`pd1_worker: RUNNING`) | Low | Ignore; PID dead — use live `pgrep` / locks |
| Safety script blind to 4h overnight locks | Low | 4h start script checks own `overnight.lock`; document in runbook |
| Prod vs staging identity drift | Low | Validate hashes on sample; config_hash fixed for PD10 |
| Disk | Low | Manifest est. small vs 5m; still enforce ≥15G free |

---

## 9. Relation to blocked 4h PD1/PD5

| Combo | 255-plan immutable coverage | Isolated run without new 1m |
|-------|----------------------------|-----------------------------|
| 4h PD1 | **0 / 255** | **No** |
| 4h PD5 | **0 / 255** | **No** |
| **4h PD10** | **255 / 255** | **Yes** (this report) |

---

## 10. Verdict

**`4H_PD10_READY_FOR_SEPARATE_APPROVAL`**

Data, immutable inputs, original LMV2 staging layout, and an isolated nohup path are **ready** for operator approval to run **pilot gates + overnight 4h PD10 only**, while the auto-queue remains **blocked** on `4h_pd1`.

**STOP** — await explicit execution freigabe. No work was executed beyond read-only validation and this report.
