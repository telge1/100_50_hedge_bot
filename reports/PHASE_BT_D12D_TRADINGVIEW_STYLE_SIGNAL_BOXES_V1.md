# PHASE BT-D12D — TradingView-Style Short Position Boxes V1

**Date:** 2026-10-25  
**Verdict:** `SHORT_SIGNAL_BOXES_READY_FOR_RESTART`

---

## Goal

Replace pending-only Entry/SL/TP **hlines** with existing TRP **`short_position`** boxes (red loss / green profit zones, entry line, price labels) for all backtester signals loaded from `latest_result.json`.

No strategy, SSOT, density, or backtest-button workflow changes.

---

## A. Renderer used

| Layer | Component |
|-------|-----------|
| Drawing primitive | TRP `short_position` via `make_short_position` (unchanged) |
| Spec mapping | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/trp_position_adapter_v1.py` |
| Workspace import | `import_bt_d7_short_research` → `build_drawing_specs` (spread + pools+ob mirror) |
| Pane render | `static/research_trp/chart.js` `renderPosition` (existing zones + labels) |

---

## B–E. Behaviour

### Unfilled / PENDING (BT-D9: all 3 signals)

- **Prices:** `entry_price_planned`, `stop_loss_price_planned`, `take_profit_price_planned` from published `signal_snapshot` only (no chart recomputation).
- **Time left:** `pending_start_utc` (order active) when present, else `order_active_from_utc` / `signal_time_utc`.
- **Time right:** `pending_expiry_utc` if present, else `display_end_utc` from overlay export (same **4h visual preview** as `overlay_export_v1._display_end_utc`). If both missing, adapter uses **4h preview** and sets `metadata.preview_time_end=true` (documented, visual-only).
- **Status:** `metadata.status_label` = `PLANNED SHORT / UNFILLED` (or CANCELLED / EXPIRED variants).
- **PnL labels:** `metadata.plan_only=true` → chart labels show **% distances and R:R only** (no USDT profit/loss text on the box).

### Filled / closed (future runs)

- Unchanged ledger-driven window (fill → exit).
- `metadata.realized_net_pnl_usdt` / `exit_reason` feed closed-trade entry label when present.

### `build_drawing_specs`

- Pending rows: **position specs only** (no plan hlines).
- Filled closed: position specs + fill/exit markers as before.

---

## G. BT-D9 acceptance (offline)

| Signal (UTC) | Entry | SL | TP |
|--------------|-------|-----|-----|
| 2026-09-11T12:00 | 0.083678 | 0.083857 | 0.082930 |
| 2026-09-11T15:35 | 0.086810 | 0.087012 | 0.085860 |
| 2026-09-11T17:00 | 0.085930 | 0.086154 | 0.084590 |

`test_bt_d12d_planned_position_boxes.py`:

- **3** `short_position` specs, **0** hlines, **0** markers  
- `stop > entry > target`, `plan_only`, UNFILLED status  
- `filled_trade_count == 0`, no invented realized PnL on specs  

After workspace import (post-restart): `loaded_positions=3`, `loaded_aux=0`.

---

## H. Backtester button

Unchanged: `bt-d12/load-latest` → `latest_result.json` only.

---

## I. Tests

| Suite | Result |
|-------|--------|
| `test_overlay_export_v1.py` | PASS |
| `test_planned_short_position_boxes_v1.py` | PASS |
| `test_bt_d12d_planned_position_boxes.py` | PASS |
| `test_bt_d12c_pool_landscape_load_route.py` (updated counts) | PASS |

---

## J. Deployment / backups (spread dashboard)

| File | Change |
|------|--------|
| `pools+ob+delta_bot/tools/.../trp_position_adapter_v1.py` | Planned short position specs |
| `research_charts/bt_d7_short_research_backtester.py` (spread + pools+ob) | Drop pending hlines |
| `static/research_trp/chart.js` | `plan_only` label formatting |
| `static/research_trp/pane.html` | `chart.js?v=bt-d12d-planned-short-position-boxes-v1` |

**Backups:** `*.bak_bt_d12d_20261025` on spread `bt_d7_short_research_backtester.py`, `chart.js`, `pane.html`.

**Rollback:** restore backups; revert `trp_position_adapter_v1.py` from git.

**Restart:** **Required** for running dashboard Python process to load adapter + `build_drawing_specs` changes. **Not performed** (no separate restart approval in BT-D12D). After restart: hard-refresh research panes so new `chart.js` bust loads.

---

## Verdict

| Item | Status |
|------|--------|
| Implementation + tests | **Done** |
| Live 8080 visual check | **Not done** |

**VERDICT:** `SHORT_SIGNAL_BOXES_READY_FOR_RESTART`

---

## STOP

No further BT-D12D scope without restart approval and optional UI verification.
