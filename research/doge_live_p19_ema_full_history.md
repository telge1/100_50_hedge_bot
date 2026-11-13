# DOGE-LIVE-P19 – EMA 9/20/59/100 full history

**Date:** 2026-11-13  
**Git baseline:** Spread `4830b2ab` · Pools `789309d7`  
**Browser:** **UNVERIFIED** (fix implemented; not visually signed off)  
**No commit / push** (per task)

## Symptom

EMA9 (rot), EMA20 (orange), EMA59 (blau), EMA100 (weiß/`#e9f2f7` per workspace) should track the **entire loaded candle series** on all chart TFs. After P18, candles could extend via gap-backfill while EMA lines still ended at the **pre-merge** tip (e.g. September tail before November catch-up).

## Phase A – Root cause (confirmed)

| Question | Answer |
|----------|--------|
| Where computed? | **Backend (Python TRP)** — `indicators.ema_overlays.compose.ema_overlays_payload` → `ema_from_candles` (close-only, SMA seed, no lookahead). |
| Frontend math? | **No** — `chart.js` only `setEmaOverlays` / LineSeries + `clipPointsToCandleExtent`. |
| Canonical path | `POST /api/research/pane` → `pane_bundle()` → `_indicators_from_candles()` → `trp["ema_overlays_payload"](candles, ema_cfg)`. |
| Config / colors | `research_charts/user_data/indicator_settings.json` (9/20/59/100 lines); workspace `ema.lines` sent in pane body. |
| Initial load | EMA bundled with **same** candle pack as first `setData` — OK when pane response is complete. |
| Gap-backfill (P18) | `mergePollCandlesIntoPane` updates **candles only** (`setData` preserveView); **`pane.pendingEma` unchanged** → lines stop at old tip; live segment has candles without EMA. |
| Scroll extend | `extendPaneCandleRangeIfNeeded` merges candles via same helper — **no indicator refresh**. |
| Poll | New closed bars → merge — same gap until manual **indicator apply** (`refreshIndicatorsVisible` → `loadPane` indicatorsOnly with **UI history range**, not pane span). |
| TF sync | EMA always computed on pane `timeframe` candles — no cross-TF fallback. |

Warm-up: `pane_bundle` loads `warm_bars = max(period)` bars before `from` when window pinned; trims EMA points to visible `from` (`_trim_indicators_to_visible`).

## Phase B – Fix (minimal)

When candles grow without a full pane reload, recompute EMA on the **pane’s actual candle span**:

- `paneIndicatorReloadRange(pane)` — `from = first_bar − warmup×step`, `to = last_bar`, `limit = min(50000, n_candles + warmup)`.
- `paneEmaStaleVsCandles(pane)` — last EMA point lags last candle by >2 bars.
- `scheduleRefreshPaneEmaOverlays` / `refreshPaneEmaOverlays` — debounced `loadPane(..., { indicatorsOnly: true, from, to, limit, skipEmaRangeRestore: true })`.
- Triggers: after `mergePollCandlesIntoPane` (gap/poll/goto extend); after `ensurePaneCaughtUp` + P18 seed if still stale.
- `loadPane` honors `opts.limit`; bundle passes `skipEmaRangeRestore` for EMA-only sync.

Unchanged: TRP EMA engine, colors, forming-candle paint path, pool/PD1/P16–P18 viewport logic.

## Historical coverage (backend spot check)

| TF | pane_bundle DOGE, limit=8000, workspace EMA | Note |
|----|-----------------------------------------------|------|
| 5m–1d | EMA series tips align with last **closed** candle in pack | Last row may be forming bar (+1 bucket vs EMA tip) — expected |
| Max load | `MAX_LIMIT = 50000` bars per request | Beyond that, scroll-left extend + repeated EMA sync applies |

## Changed files (uncommitted)

| Repo | Path |
|------|------|
| Spread | `dashboard/static/js/research/research_charts.js` |
| Spread | `dashboard/templates/research_charts.html` (`?v=pd1-p19-ema-full-history-v1`) |
| Spread | `dashboard/test_research_charts_interaction.py` |
| Pools mirror | `backtester/short/dashboard/static/js/research/research_charts.js` |
| Pools mirror | `backtester/short/dashboard/templates/research_charts.html` |

## Tests

| Test | Result |
|------|--------|
| `node --check research_charts.js` | PASS |
| `test_host_coalesce_and_hidden_pane_and_poll_after_initial` | PASS (includes `scheduleRefreshPaneEmaOverlays`) |
| `test_ema_stoch_lld_payloads_from_bundle` | PASS |
| Full `test_research_charts_interaction.py` | **3 pre-existing failures** (429 / pool_v1) — unchanged |
| Browser EMA full history all TFs | **UNVERIFIED** |

## Open issues / limits

1. **>50k bars** on one pane: EMA reload capped at 50k; oldest history may need chunked reload (not implemented).
2. **Very large pane** after merge: extra `POST /api/research/pane` (indicators only) — debounced 400ms.
3. **Forming bar:** EMA remains on closed series; tip candle may be one bucket ahead — canonical behavior preserved.
4. **Stoch/OI** not resynced on candle-only merge (out of scope; same pattern if needed later).

## Verification hints (browser)

Hard-refresh with `pd1-p19-ema-full-history-v1`. After load, confirm EMA lines reach live tip without manual indicator apply. Network: `POST /api/research/pane` with `sourceAction` `ema-after-candle-merge` or `ema-post-catch-up` after gap-backfill.

**STOP**
