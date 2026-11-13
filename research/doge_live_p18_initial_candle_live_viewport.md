# DOGE-LIVE-P18 – Initial candle LIVE viewport

**Date:** 2026-11-13  
**Status:** Fix applied (frontend); **Browser: UNVERIFIED** · **Automated static tests: PASS**

## Symptom

First open of DOGEUSDT Research Charts (5m) showed candles through **2026-09-30 ~10:30 UTC** (~66k bars). Manual **Chartansicht zurücksetzen** (pane `resetView`) jumped to the current November live tip.

P17B backend pool bounds remain unchanged.

## Confirmed root cause (Phase A)

### Load sequence

1. **`boot` → `boot_switch_symbol_start` → `switchSymbol` → `loadPane`**  
   - `resolvePaneLoadRange()` supplies `from`/`to` from History preset (default **30d** ending at wall-clock `now`, or **rolling** with `from/to: null`).  
   - First **`POST /api/research/pane`** returns the canonical candle pack (large history when window is open-ended; indicators bundled).

2. **`loadPane_pane_api_ok` → `applyPaneBundle` → `loadPane_setData_ok`**  
   - `setData` with `preserveView: false`, `skipDefaultView: false` (live mode, no GO-TO).  
   - In `chart.js`, **`applyDefaultView()`** sets the visible range to the **last bar in that payload** (`setVisibleRange` / logical fallback).

3. **`ensurePaneCaughtUp`** (gap-backfill, fire-and-forget **before fix**)  
   - If closed tip lags `now` by more than 2 bars, **`GET /api/research/candles?...&from=<last>&limit=<pollLimit>`** merges missing closed bars.  
   - **`mergePollCandlesIntoPane`** calls `chart.setData(..., { preserveView: true, skipDefaultView: … })`, which **restores the pre-merge visible range** — still centered on the **old series tip** (e.g. September frozen tail).

4. **Poll / forming**  
   - `forming_candle_applied` via `updateFormingBar` or throttled `setData` with **`preserveView: true`** — viewport unchanged.

5. **Manual reset**  
   - Pane **⟲** → `chart.resetView()` → **`applyDefaultView()`** on the **merged** series (live tip) — matches expected LIVE view.

### Answers to audit questions

| # | Question | Answer |
|---|----------|--------|
| 1 | Why ~66k candles to 30.09? | Open history load (weeks/limit + frozen DOGE line) can return a long series whose **closed tip** still sits on the frozen segment until gap-backfill; default view anchors to that tip. |
| 2 | First API historically limited? | Request uses History `from`/`to` and `historyLoadLimitForTimeframe()`; rolling uses null bounds + bar limit — not “November-only”. |
| 3 | How are Nov candles loaded? | **`ensurePaneCaughtUp`** → `/api/research/candles` gap-backfill → `mergePollCandlesIntoPane`. |
| 4 | Second `setData` keeps visible range? | **Yes** — `preserveView: true` in merge path (`mergePollCandlesIntoPane`). |
| 5 | Old saved viewport restored? | No localStorage viewport; issue is **logical range preserved** across gap merge, not persisted pan state. |
| 6 | Why reset works? | `resetView` → `applyDefaultView` on full merged data. |
| 7 | Auto LIVE too early? | **`applyDefaultView` runs once on stale tip** before async catch-up completes. |
| 8 | Race? | **Yes:** `loadPane` apply vs async `ensurePaneCaughtUp` + preserveView on merge. |

### API bounds (spot check, dashboard Python, 2026-11-13)

| Call | N candles | Last close (UTC) |
|------|-----------|------------------|
| `pane_bundle(DOGE, 5m, limit=2016)` | 2016 | 2026-11-13 09:10 |
| `pane_bundle(..., limit=24192)` (12w UI) | 24192 | 2026-11-13 09:10 |
| `load_candles(..., limit=70000)` | 50000 (MAX) | 2026-11-13 09:10 |

When the visible tip in the **first** pane response lags live, viewport stays on that tip until reset unless catch-up recenters.

## Fix (Phase B – minimal)

After **`await ensurePaneCaughtUp`** (same loader, no duplicate candle system), call **`seedLiveViewportForPane`** → `chart.resetView()` only when:

- Live mode (`!isHistoricalReplay()`, `!historyBlocksLive()`),
- Not `preserveView` / GO-TO / indicators-only,
- `sourceAction` ∈ `{ symbol-switch, tf-change, layout-change }` or `focusLiveViewport`.

This mirrors explicit live restore (`focusChartsOnLatestLoadedCandle` / reset) without affecting history reload, replay, or manual pan (`viewManual` after first center).

**Ordering benefit for pools:** Exact-Origin refresh still runs **after** catch-up + seed, so P16 stale-viewport preemption and P17 bounds refresh see the live tip viewport.

## Changed files

| Path | Change |
|------|--------|
| `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js` | `shouldSeedLiveViewportAfterLoad`, `seedLiveViewportForPane`; `await ensurePaneCaughtUp` + seed |
| `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html` | Cache bust `?v=pd1-p18-initial-live-viewport-v1` |
| `spread_recovery_hedge_short_dev/dashboard/test_research_charts_interaction.py` | Assert new symbols |
| `backtester/short/dashboard/...` | Mirror of JS + template (repo copy) |

No publisher/collector changes. **No commit / push.**

## Tests (Phase D)

| # | Case | Result |
|---|------|--------|
| 1–11 | Browser matrix (live open, pools, scroll, TF swap, forming, replay, drawings) | **UNVERIFIED** (no authenticated browser session) |
| static | `pytest test_research_charts_interaction.py::test_host_coalesce_and_hidden_pane_and_poll_after_initial` | **PASS** (`seedLiveViewportForPane`, `await ensurePaneCaughtUp`) |

## Boot trace markers (for browser verification)

- `boot_switch_symbol_start` / `boot_switch_symbol_ok`
- `loadPane_start` → `loadPane_pane_api_ok` → `loadPane_setData_ok` → `loadPane_apply_ok`
- `seed_live_viewport_ok` (new, after gap-backfill)
- `forming_candle_applied` (poll)

**STOP** — P18 complete pending browser sign-off.
