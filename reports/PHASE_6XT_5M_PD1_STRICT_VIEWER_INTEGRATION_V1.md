# PHASE 6XT – 5M PD1 STRICT 15M VIEWER INTEGRATION V1

**Date:** 2026-10-21  
**Scope:** Spread dashboard research chart (`spread_recovery_hedge_short_dev/dashboard`) — code only, no CH writes, no publish, no dashboard restart, no git.

## VERDICT

**`5M_PD1_VIEWER_INTEGRATION_PASS_READY_FOR_DASHBOARD_APPROVAL`**

---

## 1. Changed files

| File | Change |
|------|--------|
| `research_charts/historical_lmv2_pd_contract_v1.py` | `ALLOWED_VIEWER_SCOPES`, `_PD1_LINE_BY_TIMEFRAME`, `resolve_viewer_historical_scope()`, `resolve_historical_pd_config(..., timeframe=)`, `membership_slot_minutes()` |
| `research_charts/historical_pool_state_v1.py` | `load_historical_pool_state_at_close` uses `timeframe` in PD resolve; scope error text generalized |
| `pd1_landscape_viewer_v1/historical_window_v1.py` | Parametric membership slot (5/15 min), `build_landscape_window(..., timeframe, pool_detail)`, `supported_viewer_scopes` in capabilities |
| `pd1_landscape_viewer_v1/historical_window_batch_v1.py` | Phase-5S prefill on 5m or 15m decision-close grid |
| `pd1_landscape_viewer_v1/api.py` | Optional `timeframe` / `pool_detail` on `POST /live-charts/viewer/api/window` (default 15m PD1) |
| `static/js/research/pd1_exact_density_trp_adapter.js` | `causalDecisionCloseMs(t, slotMinutes)`, scope-aware window/tile cache keys |
| `static/js/research/pd1_exact_origin_density_research_v1.js` | Membership selector wiring, API body params, pane tf match, scope cache invalidation |
| `templates/research_charts.html` | `15m PD1` / `5m PD1` select (`#researchExactOriginPd1Timeframe`) |
| `research_charts/tests/test_historical_lmv2_pd_contract_v1.py` | 5m line + viewer scope rejection |
| `test_pd1_5m_viewer_integration_v1.py` | **new** — 5m window readback + 15m regression |
| `test_pd1_density_window_422_hotfix_v1.py` | VERSION string aligned to current JS (`min-depth-slider-v1`) |

---

## 2. Data flow (unchanged chain, parametric line)

```
Research chart (Exact Origin B)
  → POST /live-charts/viewer/api/window { from_utc, to_utc, timeframe?, pool_detail? }
  → resolve_viewer_historical_scope (only 15m/5m × PD1)
  → load_historical_pool_state_at_close + Phase-5S batch prefill (optional)
  → 5m price candles (CH) + membership on publication grid (5m or 15m slot)
  → pools → PD1ExactDensityTrpAdapter (origin sweep, buckets, canvas/TRP)
```

- **15m PD1 (default):** `historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1`, slot 900s.  
- **5m PD1:** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`, slot 300s.  
- **Rejected:** e.g. `5m` + `pool_detail=5` → `INVALID_SCOPE` (422). No implicit fallback between lines.

---

## 3. 5m PD1 API readback (isolated Python, batch reader on)

Test window: `2026-09-16T00:00:00Z` … `2026-09-17T23:55:00Z`

| Check | Result |
|-------|--------|
| Visible 5m prices | **576** |
| `pipeline_line_id` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| `timeframe` / `membership_slot_minutes` | `5m` / **5** |
| 168h context (`window.context_from_utc`) | **2026-09-09T00:00:00Z** |
| CH writes / publish | **none** (read-only builder) |

Artifacts: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_strict_viewer_integration_v1/capabilities.json`

---

## 4. 15m regression (default API)

| Check | Result |
|-------|--------|
| Default call (no body params) | 15m PD1 |
| September slice prices | **97** (`2026-09-14T21:45:00Z` … `2026-09-15T05:45:00Z`) |
| `config_hash` | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |
| `pipeline_line_id` | `…_15mpd1_v1` |
| Phase-5S batch reader | active |
| `test_pd1_density_window_422_hotfix_v1` artifact parity | PASS |

Published close count (17568) not re-counted in this run; prior final readback + unchanged 15m line/hash + 97-bar window regression treated as sufficient for this integration gate.

---

## 5. JS / Python parity (causal decision close)

| Timeframe | Python `_causal_decision_close` | JS `causalDecisionCloseMs(t, slotMinutes)` |
|-----------|----------------------------------|--------------------------------------------|
| 15m | floor minute to 15 | slotMinutes=15 |
| 5m | floor minute to 5 | slotMinutes=5 |

As-of / ACTIVE / 168h warmup logic unchanged. Price axis remains 5m candles in the window builder.

---

## 6. Tile / window cache parity

- Cache keys now include scope: `pd1-eod-win\x1f{tf}\x1f{pd}\x1f{from}\x1f{to}` and matching fixed 48h tile keys.  
- On membership timeframe change: `invalidateScopeCaches()` + `stripAllPanes()` + forced refresh (no stale 15m tiles under 5m scope).  
- `planExactOriginLoadWindows(..., { scopeKey })` passed from research module.

Automated node harness (`window_422_harness.js`): PASS with current research VERSION.

---

## 7. Canvas / UI

- Existing canvas path (`drawEodDensity`, min-depth slider 1–10, upper/lower colors) reused; `slotMinutes` passed into `poolsToExactOriginZones`.  
- **UI:** Membership select visible when “B – Exact Origin-Density” is on.  
- **Pane rule:** 5m PD1 requires chart pane `tf === "5m"`; 15m PD1 requires `tf === "15m"`.  
- **Browser canvas regression:** not executed in this isolated run (no dashboard restart per task). Validate once after controlled reload.

---

## 8. Tests executed

```text
cd spread_recovery_hedge_short_dev/dashboard
HISTORICAL_LMV2_BATCH_READER=1 pytest \
  research_charts/tests/test_historical_lmv2_pd_contract_v1.py \
  test_pd1_5m_viewer_integration_v1.py \
  test_pd1_density_research_live_integration_v1.py \
  test_pd1_density_window_422_hotfix_v1.py -q

20 passed
```

---

## 9. Open risks

1. **Dashboard process not restarted** — new JS/API code loads only after an approved reload of the spread dashboard service.  
2. **5m PD1 UX** — user must set chart timeframe to 5m; 15m pane + 5m membership selection intentionally does not render (avoids mixed semantics).  
3. **Multi PD checkbox (PD1/PD5/PD10 simultaneous)** — not implemented; scope key shape (`tf\x1fpd`) reserved for future extension.  
4. **Live browser tile/canvas soak** — recommend spot-check Sep 16–17 window after reload.

---

## 10. Dashboard approval instruction

1. Review diff under `spread_recovery_hedge_short_dev/dashboard` (paths in §1).  
2. At an approved maintenance window, **restart only the spread dashboard** (not pools+ob importers).  
3. Open `/live-charts/research`, DOGEUSDT, enable **B – Exact Origin-Density**.  
4. Confirm **15m PD1** on 15m chart: Sep regression viewport still shows density.  
5. Switch **5m PD1**, set chart to **5m**, pan to 2026-09-16…17: density loads, no console 422, no stale bands after toggling 15m ↔ 5m.  
6. Optional: `localStorage phase6xt_eod_fixed_tile_48h_v1=1` and verify 48h tiles still align after scope switch.

**No further CH import, publish, or LMV2 snapshot work required for this viewer gate.**

---

**STOP** — integration report complete; awaiting dashboard reload approval.
