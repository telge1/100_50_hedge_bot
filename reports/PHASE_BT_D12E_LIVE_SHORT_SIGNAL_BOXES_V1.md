# PHASE BT-D12E — Deploy Short Signal Boxes and Verify Live Dashboard V1

**Date:** 2026-10-25  
**Verdict:** `LIVE_SHORT_SIGNAL_BOXES_API_PASS_UI_NOT_VERIFIED`

---

## A. Preflight

| Check | Result |
|-------|--------|
| Port 3000 (before restart) | PID **2767403**, single listener |
| Cwd | `spread_recovery_hedge_short_dev/dashboard` |
| Command | `.../spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| `PYTHONPATH` | dashboard → spread repo → `pools+ob+delta_bot` → `trading_research_platform` |
| `HISTORICAL_LMV2_BATCH_READER` | `1` |
| BT-D12D in worktree | `trp_position_adapter_v1.py` (`plan_only`), `bt_d7_short_research_backtester.py` (no pending hlines), `chart.js` (`planOnly` labels), `pane.html` bust `bt-d12d-planned-short-position-boxes-v1` |
| Backups | `bt_d7_short_research_backtester.py.bak_bt_d12d_20261025`, `chart.js.bak_bt_d12d_20261025`, `pane.html.bak_bt_d12d_20261025` |
| `py_compile` (api, bt_d7) | OK |
| Regression tests | **7 passed** (`test_bt_d12d`, `test_bt_d12c`, `test_overlay_export_v1`) |
| `latest_result.json` | Valid — run `bt_d9_24h_import_v2_snapshots`, **3** signals, **0** trades |

**Preflight:** PASS — restart approved and executed.

---

## B. Restart

| Step | Detail |
|------|--------|
| Stop | SIGTERM PID **2767403** (~2s) |
| Start | Same venv, `PYTHONPATH`, `HISTORICAL_LMV2_BATCH_READER=1`, log append `dashboard/logs/dashboard.log` |
| New PID | **2774873** |
| Listeners on 3000 | **1** |
| Health | `GET /` → **302** |

**Rollback:** restore `*.bak_bt_d12d_20261025`; repeat stop/start block above.

---

## C. Backtester (API)

**Endpoint:** `POST /api/research/backtester/bt-d12/load-latest`  
**Body:** `{"symbol":"DOGEUSDT","visible":true}`  
**Strategy UI:** Pool Landscape SHORT · Depth 2 (Research) — unchanged, no new backtest on click.

| Route | HTTP | `loaded_positions` | `loaded_aux` | `signal_count` | `filled_trade_count` | `net_pnl_usdt` |
|-------|------|------------------|--------------|----------------|----------------------|----------------|
| `:8080` (proxy) | 200 | **3** | **0** | 3 | 0 | **0.0** |
| `:3000` (direct) | 200 | **3** | **0** | 3 | 0 | **0.0** |

**Duplicate guard:** second `load-latest` on `:8080` → `loaded_positions` still **3** (no duplicate import count).

---

## D. Expected overlays (server-side specs, post-restart)

| Signal start (UTC) | Entry | SL | TP | Status |
|--------------------|-------|-----|-----|--------|
| 2026-09-11T12:05:00 | 0.083678 | 0.083857 | 0.082930 | PLANNED SHORT / UNFILLED |
| 2026-09-11T15:40:00 | 0.086810 | 0.087012 | 0.085860 | PLANNED SHORT / UNFILLED |
| 2026-09-11T17:05:00 | 0.085930 | 0.086154 | 0.084590 | PLANNED SHORT / UNFILLED |

(Decision closes 12:00 / 15:35 / 17:00; box left edge uses `pending_start_utc` from snapshot.)

- **3** `short_position` specs, **0** hlines, `plan_only` metadata on all.
- **0** realized trade PnL on workspace summary.

**Static assets (8080, authenticated):**

- `pane.html` serves `chart.js?v=bt-d12d-planned-short-position-boxes-v1` — **yes**
- Main `research_charts.html` still references `research_charts.js?v=bt-d12c-pool-landscape-backtester-v1` (unchanged; pane carries BT-D12D chart renderer).

---

## E. Chart control (manual / UI)

Not executed in this phase (see F).

**Operator checklist:** B – Exact Origin-Density ON → DOGEUSDT → 2026-09-11 → Pool Landscape strategy → **Backtester** → confirm 3 TV-style short boxes over density bands; pan/zoom; second Backtester click → still 3 boxes.

---

## F. Verification

| Method | Result |
|--------|--------|
| Authenticated API `:8080` / `:3000` | **Pass** (counts + prices via offline spec parity) |
| IDE browser (`dash.immotel.de:8080/live-charts/research`) | Page load only; **no** usable snapshot/screenshot |
| Visual box colors / labels on chart | **Not verified** |

**Not claimed:** `LIVE_SHORT_SIGNAL_BOXES_VERIFIED`.

---

## G. Verdict

**VERDICT:** `LIVE_SHORT_SIGNAL_BOXES_API_PASS_UI_NOT_VERIFIED`

---

## STOP

Restart and API verification complete. No further BT-D12E actions unless operator UI sign-off is requested.
