# PHASE BT-D29 — Live Dynamic Selector + Offset Chart Integration

**Date:** 2026-10-25  
**Scanner repo:** `pools+ob+delta_bot` (data unchanged)  
**Live dashboard:** `spread_recovery_hedge_short_dev/dashboard` (`app.py` :3000 → `:8080`)  
**Verdict:** **`DYNAMIC_OFFSET_SERVER_READY_BROWSER_NOT_VERIFIED`**

---

## A. Data (read-only)

| Run | Path | Setups |
|-----|------|--------|
| BT-D26 baseline | `results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1/setups.json` | 102 |
| BT-D28 offset | `results/setup_scanner_runs/bt_d26_entry_offset_0p1_sep11_24h_v1/setups.json` | 102 |

Contract: `bt_d26_dynamic_upper_pool_selector_v1`. No re-scan, no JSON edits.

---

## B. Live dashboard changes

| File | Change |
|------|--------|
| `research_charts/bt_d26_dynamic_selector_backtester.py` | **New** — discover/load/viewport specs (mirrors BT-D23 pattern) |
| `research_charts/workspace_session.py` | BT-D26 catalog, viewport sync, drawing prefix `btd26-planned-`; clears BT-D23/BT-D7 on load |
| `research_charts/api.py` | Routes `GET/POST …/bt-d26-dynamic-selector/{runs,load,sync-viewport}`; `strategy_id` `pool_dynamic_selector_bt_d26` |
| `templates/research_charts.html` | Strategy option + run dropdown; JS cache `?v=bt-d29-dynamic-selector-live-v1` |
| `static/js/research/research_charts.js` | Load/sync/viewport/summary/backtester button for BT-D26 |

BT-D23, BT-D12 (pool landscape), exit-pool strategies **unchanged** in dropdown.

---

## C. Chart semantics

- Short position boxes: entry / SL / TP from `planned_*` in JSON  
- Tooltip: UTC `decision_utc`, `origin_set_key`, **`selection_reason`**  
- `metadata.plan_only` / `status_label`: **PLANNED SHORT — NOT EXECUTED**  
- TRP `chart.js` `formatPositionLabels(..., planOnly)` — **no USDT PnL** on planned boxes (existing BT-D23 behaviour)

---

## D. Viewport

- Same debounced sync as BT-D23 (5m pane)  
- Removes drawings outside viewport (`_bt_d26_rendered_ids` diff)  
- Mode switch load clears opposite scanner drawings (D26 load clears D23 prefix `btd23-planned-`)

---

## E. Validation (module-level, no auth)

`discover_dynamic_selector_runs()` → both runs, 102 setups each.

**2026-09-11T15:35:00Z** setup row:

| Run | Entry | SL | TP |
|-----|-------|-----|-----|
| Baseline | **0.08681** | **0.087011676** | **0.08586** |
| Offset 0.1% | **0.08672319** | **0.087011676** | **0.08586** |

`0.086810 × 0.999 = 0.08672319` — matches offset run.

Config provenance (offset run): `config_path.txt` → `configs/pilot_bt_d26_entry_offset_0p1_24h.yaml`  
Baseline run: `configs/pilot_bt_d21_pool_first_24h.yaml` (via `config_path.txt` on referenz run).

---

## F. Deployment

| Item | Result |
|------|--------|
| Process | `python app.py` (venv), **PID restarted** ~2832562, listening **0.0.0.0:3000** |
| `PYTHONPATH` | `spread_recovery_hedge_short_dev` + `pools+ob+delta_bot` |
| API probe | `GET /api/research/backtester/bt-d26-dynamic-selector/runs` → **401** without session (auth required) |
| Browser UI | **Not verified** in this phase (same constraint as BT-D24A) |

**User steps:** Research charts → strategy **Dynamic Upper Pool Selector · PLANNED (BT-D26/BT-D28)** → pick run → **Backtester** → pan/zoom Sep 11 15:35.

---

## G. VERDICT

**`DYNAMIC_OFFSET_SERVER_READY_BROWSER_NOT_VERIFIED`**

Integration and numeric parity checks pass at the Python/API layer; live chart requires authenticated browser confirmation.

**STOP** — no scanner/SSOT/trading/LMV2 changes.
