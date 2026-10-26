# PHASE BT-D48 — INTEGRATED SHORT BACKTESTER CHART OVERLAY V1

**Date:** 2026-10-26  
**Pilot:** DOGEUSDT 2026-09-11 24h  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**SSOT overlay:** `results/phase_bt_d47_trade_forensics_v1/chart_overlay_events.json`

## Verdict

**CHRONOLOGICAL_CHART_SERVER_READY_BROWSER_NOT_VERIFIED**

Server-side adapter, API routes, workspace viewport sync, UI mode wiring, and regression tests pass. Browser verification on the live Research Charts page was not performed in this session (per phase rules: do not claim browser success from server tests alone).

## Scope delivered

### A. New dashboard mode

- Strategy id: `chronological_short_backtester_bt_d46`
- Label: **Chronological Short Backtester · BT-D46**
- Separate from BT-D23, BT-D26/BT-D28, LMV2, and B Exact Origin-Density (no modes removed).

### B. Source of truth (read-only)

- Module: `research_charts/bt_d46_chronological_backtester.py`
- Loads and validates BT-D47 `chart_overlay_events.json` + joins `positions_audit.csv` for the data panel.
- No strategy recompute, no new signals.

Validation (pilot):

| Check | Result |
|-------|--------|
| Total markers | 38 |
| SIMULATED_ENTRY | 19 |
| SIMULATED_EXIT_SL | 14 |
| SIMULATED_EXIT_TP | 4 |
| SIMULATED_POSITION_OPEN | 1 |
| `scanner_boxes_included` | `false` |
| Unique position IDs | 19 |
| Provenance `source` | `bt_d46_integrated_short_backtester_v1` |

### C. Display

- Viewport-synced **arrow markers** at modeled prices (no large position boxes).
- Optional **trend links** entry→exit only when entry and exit bars differ (same-bar pairs flagged `same_bar_order_unresolved`; no misleading sub-bar ordering).
- Disclaimer: **SIMULATED TOUCH FILL — NOT EXCHANGE CONFIRMED** (no PnL / no USDT fee labels).

### D. Replay / as-of

- `as_of_unix` on viewport sync filters events with `time_utc <= as_of` (shared with GO TO replay via `resolveBtD26AsOfUnix` pattern).

### E. Data panel

- `#researchBtD46PositionDetail` shows position/order, pool edges, origins, decision/pending/entry, frozen SL/TP, exit evidence, execution assumption (BT-D45 M1), resolution limits when `position_id` is passed to sync (hook for selection).

### F. Density independence

- Load/sync paths call `repaintExactOriginDensityFromCache` / `repaintExactOriginDensityAllVisiblePanes` (same pattern as BT-D26). No changes to BT-D38 density math or modules.

### G. Pilot 15:35 UTC

- No `SIMULATED_ENTRY` at 15:35 for entry 0.086810 (unfilled pending before 15:40 replace). Overlay contains zero `0.08681` / `15:35` entry markers.

### H. Regression tests

`spread_recovery_hedge_short_dev/dashboard/test_bt_d48_chronological_backtester_chart_v1.py` — **7 passed** (2026-10-26).

Covers: UI/API wiring, 38-event validation, 15:35 guard, causal as-of, no duplicate drawings on repeat sync, owned-drawing clear on mode unload, no PnL fields in overlay records.

### I. API routes

| Method | Path |
|--------|------|
| GET | `/api/research/backtester/bt-d46-chronological/meta` |
| POST | `/api/research/backtester/bt-d46-chronological/load` |
| POST | `/api/research/backtester/bt-d46-chronological/sync-viewport` |
| POST | `/api/research/backtester/load` (`strategy_id=chronological_short_backtester_bt_d46`) |

### J. Assets

- `research_charts.js?v=phase6xt-bt-d48-chronological-backtester-v1`

## Browser checklist (manual)

1. Open Research Charts → select **Chronological Short Backtester · BT-D46** → **Backtester** → confirm 38-event summary and markers on 5m DOGEUSDT pilot window.
2. Toggle **B** Exact Origin-Density on/off — density must remain independent while overlay loads/unloads.
3. GO TO `2026-09-11T15:35:00Z` — no filled entry marker for replaced pending; only causal events ≤ as-of.
4. Switch to BT-D26 — D46 arrows cleared; D26 behavior unchanged.
5. Pan/zoom — no duplicate markers after repeated viewport sync.

## Prohibited items

No strategy, ranking, Entry/SL/TP, density SSOT, new backtests, PnL, or live orders were modified.
