# PHASE BT-D56 — Pre-Strategy Baseline Audit and Git Checkpoint V1

**Date:** 2026-10-26  
**Scope:** Audit, tests, documentation, local git checkpoint only. No strategy implementation, no push, no live mutations.

---

## 1. Ausgangslage

| | Repo A (pool/backtester) | Repo B (live dashboard) |
|---|---|---|
| **Path** | `/home/telgenbuescher/projects/pools+ob+delta_bot` | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev` |
| **Git root** | same as path | same as path |
| **Branch** | `pools-ob-delta-bot` | `feature/ema-touch-structure-v2` |
| **Remote** | `origin` → `git@github.com:telge1/100_50_hedge_bot.git` (SSH URL; credentials not logged) | same remote |
| **Pre-checkpoint HEAD** | `af9dedcb` (2025-10-21 docs checkpoint) | prior working tree with 30+ modified tracked files |
| **Live :3000** | n/a (embedded tree in A) | HTTP **302** (listener left running; not restarted) |

---

## 2. Geänderte Dateien (Inventar vor Checkpoint)

### Repo A — committed (381 files)

Grouped by theme:

1. **Density / PD1 / Phase6XT** — `backtester/short/dashboard/research_charts/*` (api, workspace_session, research_charts.js, phase6xt viewer, batch tests), `reports/PHASE_6XT_*`, `reports/PHASE_DENSITY_*`
2. **Phase5S / batch / cache** — partial via phase6xt historical import config and tests (full CH import trees **not** committed)
3. **Dashboard APIs** — embedded research dashboard integration in Repo A mirror tree
4. **BT-D26** — tools + reports under `tools/phase_bt_d28*` / D26 report
5. **BT-D46** — `tools/phase_bt_d46_integrated_short_backtester_v1/`, report `PHASE_BT_D46_*`
6. **BT-D47–D49 / boxes** — D47 forensics tools, D48/D49 reports; live box code lives primarily in Repo B
7. **Research BT-D39–D55** — `tools/phase_bt_d39` … `phase_bt_d54`, matching `reports/PHASE_BT_D*`
8. **Tests** — D6/D7/D8/D9 tool test suites (source committed)
9. **Documentation** — strategy research markdown, `strategy_freeze/`, `configs/`, `CURRENT_STATUS.md`, `conftest.py`
10. **Excluded from commit** — see §4

### Repo B — committed (60 files)

1. **PD1 window / cache** — `window_service_v1.py`, `window_response_cache_v1.py`, `window_request_trace_v1.py`, batch/window modules
2. **Density research UI** — `pd1_exact_origin_density_research_v1.js`, `research.css`, PD1 API changes
3. **BT-D26/D46 adapters** — `bt_d26_dynamic_selector_backtester.py`, `bt_d46_chronological_backtester.py`, related chart modules
4. **API / workspace** — `research_charts/api.py`, `workspace_session.py`, `app.py`
5. **Tests** — BT-D30–D37, D48/D48a/D48b, D49, PD1 viewer tests, pane race node tests
6. **Materializer source** — `ob1000_materializer.py`, runner, foreground script (runtime state **not** committed)

---

## 3. Was committed wurde

### Repo A

- **Commit:** `d5661662416d2a0c02810570ca35ae83a58f9b0d`
- **Message:** `checkpoint: preserve validated pool research and chronological backtester before strategy v1`
- **Stats:** 381 files, +63073 / −183 lines

### Repo B

- **Commit:** `94c596fe39283a158503c6eb00367da8712c4b7b`
- **Message:** `checkpoint: preserve live density and BT-D46 box visualization before strategy v1`
- **Stats:** 60 files, +8926 / −439 lines

### Paired baseline (use both SHAs together)

| Tag | Repo A commit | Repo B commit |
|-----|---------------|---------------|
| `pre_downtrend_strategy_v1_20261026` | `d5661662416d2a0c02810570ca35ae83a58f9b0d` | `94c596fe39283a158503c6eb00367da8712c4b7b` |

Annotated tags created locally on both repositories (not pushed).

---

## 4. Was bewusst nicht committed wurde

### Repo A (~350 remaining status entries)

- **Runtime / user state:** `user_data/drawings.json`, `indicator_settings.json`
- **Mass data / CH import progress:** `live_micro_lld_v2_ch_test/.../_controlled_import_progress_*`, large `doge_*_history` trees
- **Results artifacts:** `results/` (CSVs ignored by `.gitignore`; JSON/exports remain local)
- **Untracked research dashboard modules:** many `backtester/short/dashboard/research_charts/*.py` pilots not staged (ob200, big_picture, duplicate backtester copies)
- **Misc markdown todos** at repo root (Coins_scanner, To_Do, etc.)

### Repo B (~320 remaining status entries)

- **Materializer runtime:** `run/ob1000_materializer/*` (heartbeat, progress, locks, pid, shards)
- **Rollback / backup:** `dashboard/.rollback_*`, `*.bak_*`
- **Accidental copies:** `dashboard/templates/*.py` and `dashboard/static/js/research/*.py` mirroring `research_charts/` (likely mistaken copy; excluded)
- **Symlinks / bulk history:** `doge_apr_sep_2026_v1_history`, `doge_multi_month_2026_history`
- **Large results JSON:** `results/ob_pool_5m_*`
- **Untracked CH package bulk:** most of `live_micro_lld_v2_ch_test/` tree beyond what was needed for today's live path (left local)

No `.env`, secrets, or session cookies were staged.

---

## 5. Testresultate (ehrlich)

### Repo B — PASS (targeted)

```
pytest dashboard/test_bt_d49_box_only_visualization_v1.py \
       dashboard/test_bt_d48a_live_route_registration_v1.py \
       dashboard/test_bt_d48_chronological_backtester_chart_v1.py
→ 14 passed
```

### Repo A — PARTIAL

| Suite | Result |
|-------|--------|
| D52 + D53 + D46 integrated | **12 passed** |
| D6 foundation | **28 passed** |
| D7 integration | **26 passed** |
| D8 `test_bt_d7_fixture_cache_parity_v1` | **1 FAILED** — fixture provider vs validated-cache backtest mismatch on `signals` / orders / trades / pnl parity (assert at line 63) |

**Interpretation:** Chronological backtester and forensics tooling tests pass; **canonical density cache parity vs fixture provider is not validated** at this checkpoint. Do not claim full density-cache SSOT parity.

### Not run in this phase

- Full Repo B BT-D30–D37 slow parity suite (unit tests committed; not re-run end-to-end here)
- Live browser verification
- ClickHouse live queries

---

## 6. Commit-SHAs und Tags

```
# Repo A
git -C /home/telgenbuescher/projects/pools+ob+delta_bot checkout pools-ob-delta-bot
git -C /home/telgenbuescher/projects/pools+ob+delta_bot checkout pre_downtrend_strategy_v1_20261026
# → d5661662416d2a0c02810570ca35ae83a58f9b0d

# Repo B
git -C /home/telgenbuescher/projects/spread_recovery_hedge_short_dev checkout feature/ema-touch-structure-v2
git -C /home/telgenbuescher/projects/spread_recovery_hedge_short_dev checkout pre_downtrend_strategy_v1_20261026
# → 94c596fe39283a158503c6eb00367da8712c4b7b
```

**No `git push`** (per phase instructions).

---

## 7. Konfiguration / Module (checkpoint-relevant)

- Repo A: `configs/backtester.yaml`, pilot YAMLs under `configs/pilot_bt_d*`, `scanner_config.json`
- Repo B: live dashboard env unchanged; materializer **process state** intentionally dirty in working tree
- D47 SSOT reference (local, not fully in git): `results/phase_bt_d47_trade_forensics_v1/` on machine where forensics were generated

---

## 8. Verbleibende Risiken

1. **D8 cache parity failure** — strategy/backtests using `validated_cache` may diverge from fixture provider until fixed.
2. **Divergent trees** — Repo A embedded dashboard vs Repo B live dashboard are not byte-identical; paired tag SHAs must be used together.
3. **Large uncommitted surfaces** — ~350 (A) / ~320 (B) status lines remain; CH history and runtime JSON not in git.
4. **Accidental template/static Python clones in Repo B** — if merged blindly, could break imports; excluded but still on disk.
5. **No remote backup** — checkpoint is local only until push is approved.

---

## 9. Wiederherstellung

```bash
# Restore both baselines
git -C /home/telgenbuescher/projects/pools+ob+delta_bot fetch origin  # when push approved
git -C /home/telgenbuescher/projects/pools+ob+delta_bot checkout pre_downtrend_strategy_v1_20261026

git -C /home/telgenbuescher/projects/spread_recovery_hedge_short_dev checkout pre_downtrend_strategy_v1_20261026
```

Preserve local `run/`, `results/`, and `user_data/` when checking out; do not `git clean`.

---

## 10. Freigabe für neue Strategieentwicklung

| Criterion | Status |
|-----------|--------|
| Research tools + reports preserved in Repo A | Yes |
| Live BT-D46/D49 path preserved in Repo B | Yes (14 targeted tests pass) |
| No strategy/entry/SL/TP code changed in this phase | Yes |
| Full density cache parity | **No** (D8 failure) |
| Working tree clean | **No** (intentional exclusions) |

---

## VERDICT

**`PRE_STRATEGY_CHECKPOINT_PARTIAL`**

Proceed to **Downtrend Isolated Upper Short Strategy V1** implementation only with awareness that (1) density cache parity is open, and (2) local-only git tags/commits need push for off-machine recovery.

---

*End of BT-D56 report.*
