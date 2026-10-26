# PHASE BT-D49 — BT-D46 BOX-ONLY TRADE VISUALIZATION

**Date:** 2026-10-26  
**Verdict:** `BT_D46_BOX_ONLY_SERVER_READY_BROWSER_NOT_VERIFIED`

## Summary

BT-D46 **Chronological Short Backtester** now renders **only** classic TRP **short position boxes** (red stop zone above entry, green target below, entry mid-bar) — one box per simulated trade. BT-D48/BT-D48B marker ticks, link lines, and overlay labels are **removed** from this mode.

**No** changes to D46/D47 data, strategy, Entry/SL/TP math, PnL, or density.

## Root cause (prior display)

BT-D48/BT-D48B used sub-pixel arrows/ticks and separate ENTRY/SL/TP markers. User requirement: **TradingView-style position boxes** per trade, not event markers.

## What changed

| File | Change |
|------|--------|
| `research_charts/bt_d46_chronological_backtester.py` | `build_viewport_draw_specs` → `position_specs` from `positions_audit.csv` + causal overlay; `BOX_PREFIX=btd46-box-`; min width 300s for same-bar; OPEN extends to `as_of` |
| `research_charts/workspace_session.py` | `sync_bt_d46_*` uses `make_short_position` only; clears legacy marker/link/label overlays |
| `static/js/research/research_charts.js` | Summary text: trade boxes / `box_only_short_position_v1` |
| `templates/research_charts.html` | Asset `phase6xt-bt-d49-box-only-v1` |
| `test_bt_d49_box_only_visualization_v1.py` | New regression suite |
| `test_bt_d48b_marker_visibility_v1.py` | Skipped (superseded) |

API routes unchanged (`meta` / `load` / `sync-viewport`).

## Display rules (implemented)

- **One** `short_position` drawing per position with causal `SIMULATED_ENTRY` ≤ `as_of`
- **Time:** `entry_bar_utc` → exit bar (SL/TP) or `as_of` when still open
- **Prices:** `entry_price`, `frozen_sl`, `frozen_tp` from audit (unchanged SSOT)
- **Same-bar:** visual min span 300s without changing semantic timestamps
- **No** `btd46-marker-*`, `btd46-link-*`, `btd46-label-*` in new sync (legacy IDs still cleared on load)

## Reference cases

| Case | Expected |
|------|----------|
| GO TO **2026-09-11 15:35 UTC** | No box for unfilled 0.08681 pending |
| GO TO **2026-09-11 17:30 UTC** | **7** causal trades; **5** boxes in ±4h viewport (no marker lines) |
| Strategy switch BT-D46 ↔ BT-D26 | Only `btd46-box-*` / owned origins cleared; density untouched |

## Tests

`test_bt_d49_box_only_visualization_v1.py` + updated D48 tests — **12 passed**, 1 skipped (D48B markers).

## Live verification

- Dashboard restarted: PID **3016305** (2026-10-26)
- `POST …/load` + `POST …/sync-viewport` (as-of `2026-09-11T17:30:00Z`): **200**, **5** viewport boxes / **7** causal trades, `box_only_short_position_v1`
- External `8080`: route auth **401** (not 404) — proxy OK
- **Browser:** hard-refresh required; not automated in this session

## Operational note

After deploy, restart `:3000` `app.py` so box renderer is loaded; hard-refresh research JS cache.
