# DOGE-LIVE-P25 — Stable Multi-Timeframe Git Release

**Date:** 2026-11-13 (UTC)  
**Result:** **P25_RELEASE_VERIFIED**

---

## Executive summary

Browser-confirmed DOGE Exact-Origin pools across **5m, 30m, 1h, and 4h** (user acceptance, this session) are captured in a **minimal, frontend-only** Spread commit on `feature/ema-touch-structure-v2`, with regression tests and research mirrors in `pools+ob+delta_bot`. Backend landscape/on-demand stack remains at prior commits on the same branch (HEAD~1 baseline `5cb52d87`); no MC4 multicoin partial stack was published.

---

## Phase A — Inventory and backup

### Spread (`spread_recovery_hedge_short_dev`)

| Item | Value |
|------|--------|
| Branch | `feature/ema-touch-structure-v2` |
| Pre-P25 HEAD | `5cb52d8780411828412f72748a08c9e976178564` |
| Post-P25 HEAD | `332f348bf261ca3892446f0ef821ea7761020ff3` |
| Upstream | `origin/feature/ema-touch-structure-v2` (pushed, hashes match) |
| Live dashboard | PID **2379483**, cwd `…/spread_recovery_hedge_short_dev/dashboard`, `:3000` |

**Modified (pre-commit) tracked files:** 3 (`pd1_exact_origin_density_research_v1.js`, `research_charts.js`, `research_charts.html`).

**Staged for release (Group A):** 9 files (3 JS/HTML + 5 pytest + Node coalesce harness).

**Excluded (Group B/C):** MC4 accident tree under `dashboard/static/js/research/*.py`, `dashboard/research_trp/`, `dashboard/templates/*.py`, rollback dirs, `user_data`, `run/ob1000_materializer/`, patches, local backups — **not** committed.

**Backup (outside active tree consumption):**

`research/backups/p25_20261113T160022Z/` in **pools** repo — full patch + file copies + `SHA256SUMS.txt` (not in git).

### Pools (`pools+ob+delta_bot`)

| Item | Value |
|------|--------|
| Branch | `pools-ob-delta-bot` |
| Pre-P25 HEAD | `e4ada867b3f6f93d9f901cae290a9f8fe50f4fde` |
| Working tree | Large unrelated dashboard/MC4 deltas under `backtester/short/` — **not** part of P25 release |

### SHA256 — live served assets (pre/post commit identical on disk)

| File | SHA256 |
|------|--------|
| `pd1_exact_origin_density_research_v1.js` | `7ad9349328cc468b4dae7bb4f25506c59291c14a42fcc1aa04434c493407752a` |
| `research_charts.js` | `94f9275565108db73790e845cf553d3ed032b01e9ef086c82fc736dcbed8fe86` |
| `research_charts.html` | `ca1f9c6fc09273d93bc1a95c6a1a2aea31d1c9c512b23605a30abc5f0df202e7` |

Cache bust: `pd1-p24f-1h-4h-bounds-v1`

---

## Phase B — DOGE stack (unchanged backend in this commit)

Call chain (unchanged at `5cb52d87` for Python):

`research_charts.html` → `research_charts.js` / `pd1_exact_origin_density_research_v1.js` → capabilities + on-demand bounds API → landscape window POST → pool state → D2 canvas.

Backend modules (already on branch, not in P25 diff):

- `pd1_landscape_viewer_v1/window_service_v1.py`, `historical_window_v1.py`, `historical_window_batch_v1.py`
- `pd1_landscape_viewer_v1/on_demand_ch1m_pd1_v1.py` (30m / 1h / 4h PD1)
- `research_charts/historical_pool_state_v1.py` (on-demand viewer bounds)

**`pipeline_line_id` regression:** `get_published_decision_closes(..., pipeline_line_id=hcfg.pipeline_line_id)` present in `historical_window_v1.py` at baseline; on-demand integration tests at HEAD pass (see Phase E).

---

## Phase C — Change groups

| Group | Action |
|-------|--------|
| **A** | Committed — P23–P24F frontend + tests |
| **B** | MC4 multicoin untracked mirrors — **excluded** |
| **C** | Secrets, sessions, backups, generated CH history, vendor copies — **excluded** |

---

## Phase D — Reproducibility

1. Git worktree at `5cb52d87`: `spread_recovery_hedge_short_dev-p25-verify` (branch `p25-verify-wt`).
2. Copied the 9 release files into the worktree (simulates fresh checkout + commit contents).
3. **21 passed** — P23–P24F pytest suite in worktree.
4. Committed tree on `332f348b` matches live dashboard file hashes for the three served assets.

**Note:** Standalone `python3 -c import pd1_landscape_viewer_v1` requires dashboard `app.py` path setup (`project_root` / `vendor`); live process uses that. Window on-demand tests run correctly from `dashboard/` with project venv.

---

## Phase E — Regression tests

| Suite | Result |
|-------|--------|
| P23 `test_p23_exact_origin_auth_session_v1.py` | PASS (in 21) |
| P24C `test_p24c_tf_switch_viewport_suppress_v1.py` | PASS |
| P24D `test_p24d_on_demand_viewport_resolve_v1.py` | PASS |
| P24E Python + Node harness | PASS |
| P24F `test_p24f_1h_4h_on_demand_bounds_v1.py` | PASS |
| Backend on-demand (HEAD baseline, untracked on disk) `test_pd1_30m_on_demand_v1.py`, `test_pd1_4h_on_demand_v1.py` | **7 passed** (CH, not mutating orders) |

**Known / out of scope:** Full dashboard pytest matrix; pools repo `backtester/short` parity tests; MC4 WIP.

---

## Phase F — Git publish

### Spread commit

```
332f348b fix(doge-live): stabilize exact-origin pools across timeframes
```

**Files pushed (9):**

- `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js`
- `dashboard/static/js/research/research_charts.js`
- `dashboard/static/js/research/test_p24e_coalesce_logic_v1.js`
- `dashboard/templates/research_charts.html`
- `dashboard/test_p23_exact_origin_auth_session_v1.py`
- `dashboard/test_p24c_tf_switch_viewport_suppress_v1.py`
- `dashboard/test_p24d_on_demand_viewport_resolve_v1.py`
- `dashboard/test_p24e_identical_viewport_coalesce_v1.py`
- `dashboard/test_p24f_1h_4h_on_demand_bounds_v1.py`

Remote: `git@github.com:telge1/100_50_hedge_bot.git` → `332f348b` verified.

### Pools mirror commit

See post-commit hash below — research reports P23–P25 only.

---

## Phase G — Post-push verification

| Check | Status |
|-------|--------|
| Local HEAD == `origin/feature/ema-touch-structure-v2` | YES (`332f348b`) |
| Working tree JS hashes == `git show HEAD:…` | YES (pd1 EO file checked) |
| Dashboard restart | **Not performed** — live process still on pre-push in-memory state; **hard refresh** loads new static assets from disk (already matched committed bytes before push) |
| Uncommitted dependencies for DOGE TF fix | **None** — all P23–P24F code in commit |

---

## Browser-confirmed timeframes (user)

User confirmed **all desired DOGE pool timeframes work** in live chart after P23–P24F.

**Technical UI scopes (unchanged in commit):**

| Chart pool TF | Released scope | Data path |
|---------------|----------------|-----------|
| 5m | 5m PD1 publish | Live publisher + frozen capabilities |
| 30m | 30m PD1 | On-demand `on_demand_ch1m_30mpd1_v1` |
| 1h | 1h PD1 | On-demand `on_demand_ch1m_1hpd1_v1` (P24F bounds fetch) |
| 4h | **4h PD10** only in `RELEASED_VIEWER_SCOPES` | Frozen publish Apr–Sep; on-demand **PD1** bounds prefetched but **not** default UI scope |

**4h clarification:** Success in browser does **not** imply live November coverage for **4h PD10**. P24F stores 4h PD1 bounds for future release; default UI remains PD10 frozen. Document user success as stable chart/pool behavior for the **released** 4h scope (and TF switching without P24E loops), not as new PD10 live data.

---

## Open MC4 / local-only work (Spread)

Still **uncommitted** and must not be merged blindly:

- `dashboard/research_charts/multicoin_pd1_scope_v1.py`, `viewer_scope_v1.py`
- Entire mistaken copy trees: `dashboard/static/js/research/**/*.py`, `dashboard/static/research_trp/`, `dashboard/templates/*.py`
- `research/doge_live_mc4_window_backup_20261113*.patch`
- Untracked on-demand perf/tests (`test_pd1_30m_*.py`, etc.) — valuable but optional follow-up commit

## Open work (Pools repo)

- `backtester/short/dashboard/**` modified + untracked landscape stack — **mirror drift**, not updated in P25.

---

## STOP

**P25_RELEASE_VERIFIED** — Spread multitimeframe Exact-Origin frontend secured; research mirror updated; no force push; no service restart.
