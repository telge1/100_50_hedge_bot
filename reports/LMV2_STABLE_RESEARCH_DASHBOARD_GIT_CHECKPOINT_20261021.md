# LMV2 Stable Research Dashboard — Git Checkpoint (2026-10-21)

## VERDICT

**LMV2_DASHBOARD_CHECKPOINT_COMMITTED_AND_PUSHED**

Controlled checkpoint for the working DOGEUSDT Historical Research Dashboard (canonical runtime: `spread_recovery_hedge_short_dev/dashboard`, port 3000 / :8080 proxy).

---

## Repository topology

| Repository | Git root | Relationship |
|------------|----------|--------------|
| `pools+ob+delta_bot` | `/home/telgenbuescher/projects/pools+ob+delta_bot` | Standalone repo |
| `spread_recovery_hedge_short_dev` | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev` | Standalone repo (not a submodule of pools) |

Both remotes: `git@github.com:telge1/100_50_hedge_bot.git` (private).

No git submodules or worktrees detected for this checkpoint. Background Cursor agents may still hold uncommitted local edits outside this commit set.

---

## Spread (`spread_recovery_hedge_short_dev`) — primary dashboard runtime

| Field | Value |
|-------|--------|
| Branch | `feature/ema-touch-structure-v2` |
| HEAD before | `e960f38d2a821925492ec8c24f795800f4a3a722` |
| HEAD after (commit) | `d936656901563f379034f98e40e220f589324305` |
| Commit message | `checkpoint: stable LMV2 multi-PD research dashboard and min-depth canvas` |
| Annotated tag | `spread-lmv2-dashboard-stable-2026-10-21` → commit `d9366569` |
| Remote branch | `origin/feature/ema-touch-structure-v2` @ `d9366569` (verified via `git ls-remote`) |

### Staged / committed files (38)

- `dashboard/pd1_landscape_viewer_v1/*` (window API + batch + 4h slot membership / causal close)
- `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` (VERSION `phase6xt-exact-origin-canvas-planned-hotfix-v1`, `planned` hotfix)
- `dashboard/static/js/research/pd1_exact_density_trp_adapter.js` (240m 4h adapter)
- `dashboard/research_charts/historical_lmv2_pd_contract_v1.py`, `historical_pool_state_v1.py`, `service.py`
- `dashboard/static/research_trp/chart.js`, `pane.html`, `style.css`
- `dashboard/templates/research_charts.html`, `partials/nav.html`, `research.css`, `research_charts.js`
- `dashboard/app.py`, `dashboard/conftest.py` (pytest PYTHONPATH parity with production)
- Phase 6XT regression tests (5m / 15m / 4h PD10, min-depth, merge-gap, checkbox UI, canvas hotfixes)

### Explicitly excluded (not in commit)

- `.env`, secrets, ClickHouse data / import trees, `doge_*_history` symlinks
- `.rollback_*` directories, `user_data/`, logs, PID/lock files, venvs
- Erroneous duplicate trees (`dashboard/static/js/research/*.py`, `dashboard/templates/*.py` copies)
- Unrelated WIP: EMA v2, ob1000 materializer, collector health, workspace_session, etc.

---

## Pools (`pools+ob+delta_bot`) — shadow viewer + audit reports

| Field | Value |
|-------|--------|
| Branch | `pools-ob-delta-bot` |
| HEAD before | `66e29d46eb0fdd2647b5c80a539330e2dcebde87` (plus 1 unpushed commit existed pre-checkpoint) |
| HEAD after (checkpoint commit) | `78d11f9ae32c832ba6dc4f1b2b01d811dab3cbef` |
| Commit message | same checkpoint message as Spread |
| Annotated tag | `pools-lmv2-dashboard-stable-2026-10-21` → commit `78d11f9a` |
| Remote branch | `origin/pools-ob-delta-bot` @ `78d11f9a` (verified via `git ls-remote`) |

### Staged / committed files (47)

- `backtester/short/dashboard/pd1_landscape_viewer_v1/*` (shadow sync of window slot logic)
- `reports/PHASE_6XT_*.md` (43 activation / integration / audit reports)

### Explicitly excluded

- Historical JSON production trees, import progress dirs, scanner configs, local audits JSON
- Modified but non-checkpoint dashboard files (`app.py`, batch config, `indicator_settings.json`, etc.)
- Root-level markdown notes, ob200 backtester trees, remaining untracked research scripts

---

## Functional verification (code read)

- Exact Origin Density JS: `VERSION = "phase6xt-exact-origin-canvas-planned-hotfix-v1"`; `scopePlans.reduce` for planned counts (no bare `planned`).
- Multi-PD scopes: 5m/15m PD1/5/10; 4h PD10 in viewer allow-list and adapter 240-minute membership.
- Canvas path default and min-depth slider wiring present in committed templates/JS.
- Merge-gap fix covered by `test_exact_origin_density_merge_gap_fix_v1.py`.

---

## Regression tests (Spread `dashboard/`, read-only CH)

Command:

```bash
cd spread_recovery_hedge_short_dev/dashboard
../.venv/bin/python -m pytest \
  test_4h_pd10_strict_viewer_integration_v1.py \
  test_5m_multi_pd_density_integration_v1.py \
  test_15m_multi_pd_minimal_viewer_release_v1.py \
  test_pd1_5m_viewer_integration_v1.py \
  test_pd1_density_min_depth_slider_v1.py \
  test_exact_origin_density_merge_gap_fix_v1.py \
  test_density_pd_checkbox_ui_v1.py \
  test_pd1_density_window_422_hotfix_v1.py -q
```

**Result: 32 passed** (2026-10-21, ~170s). No ClickHouse writes.

Note: `research_charts/tests/test_historical_lmv2_pd_contract_v1.py` requires full repo PYTHONPATH (`dashboard/conftest.py` now sets this for top-level integration tests).

Pre-checkpoint fixes applied only in test/lock metadata: adapter and `historical_window_v1.py` SHA256 locks, invalid-scope case uses `4h`+PD1 (PD10 now valid), HTML cache-buster assert for hotfix version.

---

## Security preflight

- Staged diffs scanned for obvious secret patterns: **none found**.
- No force-push, no reset, no `.env` staged.

---

## Push status

| Item | Status |
|------|--------|
| Spread branch push | OK `e960f38d..d9366569` |
| Spread tag push | OK `spread-lmv2-dashboard-stable-2026-10-21` |
| Pools branch push | OK `99aca489..78d11f9a` |
| Pools tag push | OK `pools-lmv2-dashboard-stable-2026-10-21` |

Tag name `lmv2-dashboard-stable-2026-10-21` was **not** reused on both repos; per-repo tags above avoid collision on shared remote.

---

## Restore hint

```bash
# Spread runtime dashboard
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev
git checkout spread-lmv2-dashboard-stable-2026-10-21

# Pools reports + shadow viewer
cd /home/telgenbuescher/projects/pools+ob+delta_bot
git checkout pools-lmv2-dashboard-stable-2026-10-21
```

---

## Remaining local dirt (expected, not in checkpoint)

- **Spread:** EMA/materializer/run artifacts, large untracked `live_micro_lld_v2_ch_test` import tree, rollback dirs, mistaken file copies under `static/js/research/`.
- **Pools:** Mass untracked CH import JSON, modified `backtester/short/dashboard` files outside `pd1_landscape_viewer_v1`, root markdown todos.

---

*Generated: 2026-10-21. Checkpoint complete — no dashboard restart, no CH writes, no strategy changes.*
