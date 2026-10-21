# PHASE 6XT — 4H PD10 ISOLATED PILOT V1

**VERDICT:** `4H_PD10_PILOT_PASS_AWAITING_GATE_D`

**Scope executed:** One isolated Original-LMV2 4h PD10 pilot close only. No overnight run, no Gate D file, no queue resume, no CH/MASTER/production writes.

Machine artifacts:

`logs/historical_import/overnight_2026/phase6xt_4h_pd10_isolated_pilot_v1/`

- `gate_a_result.json`, `gate_b_result.json`, `gate_c_result.json`
- `gate_b_plan.json`
- `pilot_run.json`
- `resume_backup_pilot.json` (copy of first successful output)

Isolated output (single file):

`backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history/staging/phase6xt_4h_pd10_isolated_pilot_v1/results/2026-04/4h_PD10/20260411T000000Z.json`

Prior: `reports/PHASE_6XT_4H_PD10_ISOLATED_READINESS_V1.md`

---

## 1. Pilot selection (Task 1)

| Field | Value |
|-------|--------|
| **Decision close** | `2026-04-11T00:00:00+00:00` |
| **Stem** | `20260411T000000Z` |
| **Rationale** | Earliest close in prepared April plan `plan_april_84c22453.json` (first of 75 April-head closes); matches standard queue pilot anchor `GATE_B_ISO` used by `run_pilot_gates_for_job.py` |
| **Immutable snapshot** | `84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93` |
| **price_hash** | `d489aa686f6f648850f248d4809338abbee0d7ab6f0bda3ec5ffe3e698757f48` |
| **revision_hash** | `9a3ba3066376d27aabc81ffe01b9b08741e819ba5095bf895f8dd0125812258e` |
| **Required 1m start** | `2025-10-10T20:00:00+00:00` |
| **history_from_close** | `2025-12-11T00:00:00+00:00` (`rolling_history_from_close(decision, 4)`) |

Preflight: no file at this stem in production or in `phase6xt_4h_pd10_jan_sep_v1` staging before run.

---

## 2. Gates A / B / C (Task 2)

| Gate | Status | Summary |
|------|--------|---------|
| **A — Production guard** | **PASS** | `production_guard_baseline.json` sample `20260815T120000Z.json` present; SHA256 unchanged before/after pilot |
| **B — Single-close generate + validate** | **PASS** | `run_multi_month.py --worker 0` exit 0; `_validate_file` clean; timeframe `4h`, PD10, frozen price hash matches plan |
| **C — Determinism (resume)** | **PASS** | Deleted pilot output, regenerated same plan; SHA256 identical |

### Gate C scope note (operator freigabe)

Full auto-queue pilot script Gate C normally generates **12 additional 4h closes** after Gate B, then a 13-close resume test. Your freigabe allowed **only one** pilot close generation. Gate C here is the **established resume/determinism check on that single close** (same pattern as the resume step at end of `run_pilot_gates_for_job.py`, without the 12-close batch). Documented in `pilot_run.json` → `gate_c_note`.

No production files, queue state, or `phase6xt_4h_pd10_jan_sep_v1` overnight manifest were modified for this pilot.

---

## 3. Isolated generation (Task 3)

- **Pipeline:** `doge_multi_month_2026_history/run_multi_month.py` (Original LMV2 worker)
- **Plan:** `gate_b_plan.json` — one task, one close, `output_root` = isolated staging only
- **Files written:** **1** snapshot JSON under isolated staging (`4h_PD10` count = 1)
- **ClickHouse / MASTER / production tree:** not written
- **Queue:** remains **BLOCKED** on `4h_pd1`; no `gate_d_approved_4h_pd10.json` created

---

## 4. Result validation (Task 4)

| Check | Result |
|-------|--------|
| Expected output path | Present |
| `decision_close` | `2026-04-11T00:00:00+00:00` |
| `config_hash` (engine PD10) | `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2` (via `_validate_file` / identity) |
| `frozen_input.price_hash` | Matches plan / manifest |
| `validate_problems` | `[]` |
| Determinism | `4c0a1652…` before delete = after resume |
| Production guard samples | Unchanged |
| Main jan_sep staging | **No** pilot file written there |
| Duplicate stems (isolated) | Single file only |
| Other PDs / timeframes | Not touched |

**Output hash (SHA256):** `4c0a1652459c0f1c6b393ee1d147e4125d8ba9a5fb4d114cd7dd026d7d9bd1d5`  
**Size:** 64 830 bytes

---

## 5. Runtime and resources (Task 5)

| Phase | Wall time | Worker RSS (peak) |
|-------|-----------|-------------------|
| Gate B (1× generate) | **23.0 s** | ~1 148 MiB |
| Gate C (1× resume) | **22.5 s** | ~1 152 MiB |
| **Total worker time** | **45.5 s** | — |

**Rough extrapolation (not measured overnight):** ~23 s/close × 255 ≈ **98 min** CPU-bound single-worker generation, plus supervisor overhead — order of **~1.5–2 h** for full isolated 255-close run if parity holds.

Preflight: `historical_lmv2_safety.sh` → `SAFETY_OK`; no concurrent `run_multi_month` workers.

---

## 6. Risks

| Risk | Assessment |
|------|------------|
| September segment uses `fe549309` | Not exercised in this pilot; April immutable path proven only |
| Gate C ≠ full 12-close batch | Overnight approval should explicitly accept single-close determinism or require extended pilot before Gate D |
| Safety script ignores 4h locks | Mitigated: manual preflight; no overnight started |
| Queue stale JSON | Irrelevant to this pilot; do not resume queue for PD10 |

---

## 7. Gate D recommendation (Task 5)

**Do not auto-create Gate D.**

For operator Gate D on the **full 255-close isolated overnight** (`overnight_4h_pd_nohup.sh 10 start`), suggested criteria:

1. Explicit acceptance of **single-close pilot + resume** evidence (or optional follow-up: run full `run_pilot_gates_for_job.py` 12-close Gate C into **isolated** staging without touching jan_sep until approved).
2. Confirm queue stays **BLOCKED** on `4h_pd1`; overnight via **direct** `overnight_4h_pd_nohup.sh 10` only.
3. Disk ≥ 15 G, `SAFETY_OK`, no other historical worker.
4. Signed `gate_d_approved_4h_pd10.json` referencing scope `overnight_generate_total: 255` and isolated or jan_sep staging root per your runbook choice.

---

## 8. Production data integrity

- Production guard baseline sample: **unchanged** (`production_guard_unchanged: true`).
- Production results tree: **no** new `20260411T000000Z` file.
- `gate_d_approved_4h_pd10.json`: **not** created.

---

## 9. Verdict

**`4H_PD10_PILOT_PASS_AWAITING_GATE_D`**

**STOP** — no `overnight_4h_pd_nohup.sh 10 start`. Await your explicit freigabe for the full isolated 255-close NOHUP run and separate Gate D approval.
