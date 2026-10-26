# PHASE BT-D12A — Complete Entry / SL / TP for All Backtest Signals V1

**Date:** 2026-10-25  
**Verdict:** `COMPLETE_SIGNAL_OVERLAYS_READY_FOR_RESTART`

---

## A. Finding (where levels were computed)

| Stage | Entry | SL | TP |
|-------|-------|----|----|
| BT-D7 signal engine | `entry_level` (upper pool lower edge) | — | — |
| BT-D7 fill path | fill price | `short_sl(pool_upper)` at fill | `select_short_tp_depth2(fill, …)` at fill |
| BT-D9 / BT-D12 JSON (before D12A) | yes | **missing on signal** | **missing on signal** |
| BT-D10 overlay | entry hline only for pending | only if trade | only if trade |

**Gap:** Planned SL/TP existed only after fill; unfilled pending signals had no stored or drawn plan levels.

---

## B. Signal snapshot (new, causal)

`tools/phase_bt_d7_config_driven_short_pnl_integration_v1/signal_snapshot_v1.py`

At each new signal, `short_research_engine_v1` attaches `signal_snapshot` using **existing** `strategy_rules.short_sl` and `select_short_tp_depth2` on **as-of density** and **planned entry** (limit level), not post-fill state.

Fields include: `entry_price_planned`, `stop_loss_price_planned`, `take_profit_price_planned`, band edges, `origin_ids`, gap %, pending window, `config_hash`, density provenance, `snapshot_provenance` (`LIVE_AT_SIGNAL`).

---

## C. BT-D9 legacy

Original BT-D9 artifacts **unchanged**.

Reconstruction: `enrich_bt_d9_snapshots_v1.py` — reads validated **cache at decision close** + same rules → `snapshot_provenance: RECONSTRUCTED`.

Published run: **`bt_d9_24h_import_v2_snapshots`** → updates `latest_result.json`.

| Signal (close UTC) | Entry | SL | TP |
|--------------------|-------|-----|-----|
| 2026-09-11T12:00:00Z | 0.083678 | 0.08385738 | 0.08293 |
| 2026-09-11T15:35:00Z | 0.08681 | 0.087011676 | 0.08586 |
| 2026-09-11T17:00:00Z | 0.08593 | 0.086153964 | 0.08459 |

PnL remains **0 USDT** (0 trades).

---

## D. JSON / overlay contract

- Signals in `result.json` / `integration` carry `signal_snapshot`.
- BT-D10 overlay contract **`bt_d10_overlay_contract_v1_1`**: records expose `entry_price_planned`, `stop_loss_price_planned`, `take_profit_price_planned`, `snapshot_provenance`.
- Atomic `latest_result.json` publishing unchanged.

---

## E. Density chart (TRP)

`build_drawing_specs` (pools+ob + spread copy): for **PENDING / SIGNAL / CANCELLED** — three **hlines** with price labels (Entry / SL / TP). Filled trades still use `short_position` zones.

Backtester button flow unchanged: `bt-d12/load-latest` → no new UI.

**Dashboard restart required** to load API/JS (not performed).

---

## F. Tests

| Test | Result |
|------|--------|
| `test_signal_snapshot_v1` | PASS |
| `test_overlay_planned_levels_v1` | PASS |
| BT-D7 integration (5) | PASS |
| BT-D10 suite | PASS |
| `test_bt_d12_latest_backtester` (3 signals, all SL/TP) | PASS |

---

## G. Deployment

- Spread: `bt_d7_short_research_backtester.py` updated (3 plan lines).
- pools+ob: same + engine/snapshot modules.

Rollback: restore `.bak_bt_d12_*` if needed; revert to `bt_d9_24h_import_v1` run folder for latest (manual).

---

## Verdict token

**`COMPLETE_SIGNAL_OVERLAYS_READY_FOR_RESTART`**
