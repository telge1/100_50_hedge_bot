# PHASE BT-D12B — Controlled Dashboard Restart and Signal Overlay Verification V1

**Date:** 2026-10-25  
**Verdict:** `LIVE_SIGNAL_OVERLAYS_API_PASS_UI_NOT_VERIFIED`

---

## Scope

- **Only** the Spread research dashboard on port **3000** was restarted (user-approved).
- No new backtest, no strategy/SSOT/density changes, no other services touched.

| Item | Value |
|------|--------|
| Dashboard | `spread_recovery_hedge_short_dev/dashboard/app.py` |
| Backtester project | `/home/telgenbuescher/projects/pools+ob+delta_bot` |
| Published result | `results/backtester_runs/latest_result.json` |
| Run ID | `bt_d9_24h_import_v2_snapshots` |

---

## A. Preflight

| Check | Result |
|-------|--------|
| Port 3000 listener (before restart) | `python` PID **2705103**, cwd `.../spread_recovery_hedge_short_dev/dashboard` |
| Runtime command | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| `PYTHONPATH` (documented from prior live restart) | `dashboard` → spread repo root → `pools+ob+delta_bot` → `trading_research_platform` |
| `HISTORICAL_LMV2_BATCH_READER` | `1` |
| `DASHBOARD_PORT` | default **3000** (not overridden) |
| BT-D12 backups in Spread worktree | `research_charts/api.py.bak_bt_d12_20261025`, `static/js/research/research_charts.js.bak_bt_d12_20261025` |
| `py_compile` (Spread `api.py`, `bt_d12_latest_backtester.py`, `bt_d7_short_research_backtester.py`) | OK |
| Regression tests (`bt_d7` / `bt_d10` / `bt_d12` under `backtester/short/dashboard/tests`) | **6 passed** |
| `test_bt_d12_latest_backtester` | **1 passed** |
| BT-D12A in Spread worktree (`POST .../bt-d12/load-latest`, planned SL/TP in `build_drawing_specs`) | Present |
| `latest_result.json` valid | Yes — **3** signals, **0** filled trades |
| Planned prices (6-decimal compare) | Match spec table below |
| Total PnL | **0 USDT** (`net_pnl_usdt`: 0.0) |

**Preflight:** PASS — restart allowed.

---

## B. Controlled restart

| Step | Detail |
|------|--------|
| Stop | `SIGTERM` to PID **2705103** (stopped ~2s) |
| Start | Same cwd, venv, `PYTHONPATH`, `HISTORICAL_LMV2_BATCH_READER=1`, log append `dashboard/logs/dashboard.log` |
| New PID | **2757702** |
| Single listener on 3000 | Confirmed (`ss -tlnp`) |
| Health | `GET /` → **302** (expected unauthenticated redirect) |
| Log tail | Uvicorn up; authenticated `POST .../bt-d12/load-latest` → **200** |

**Rollback (if needed):** restore `.bak_bt_d12_*` files; repeat the same stop/start block above.

---

## C. Backtester button workflow (API)

**Strategy:** Pool Landscape SHORT · Depth 2 (Research)  
**Endpoint:** `POST /api/research/backtester/bt-d12/load-latest`  
**Body:** `{"symbol":"DOGEUSDT","visible":true}`  
**Source:** reads `latest_result.json` only — **does not** start a new backtest.

Authenticated probe (existing persisted `session_id` cookie, same mechanism as live UI):

| Field | Value |
|-------|--------|
| HTTP status | **200** |
| `backtester.source` | `bt_d12_latest_result_v1` |
| `backtester.summary.run_id` | `bt_d9_24h_import_v2_snapshots` |
| `backtester.summary.signal_count` | **3** |
| `backtester.summary.filled_trade_count` | **0** |
| `backtester.summary.net_pnl_usdt` | **0.0** |
| `backtester.loaded` (position rectangles) | **0** |
| `backtester.loaded_aux` (hlines/markers) | **9** |
| `bt_d7_short_research.loaded_positions` | **0** |

---

## D. Density chart overlays (server-side drawing pipeline)

Offline parity on Spread modules + published JSON:

- `build_drawing_specs` → **0** position specs, **9** hline specs, **0** markers.
- All three signals **PENDING/UNFILLED** → planned Entry / SL / TP only (no trade rectangles, no invented PnL).

| Close UTC | Entry | SL | TP |
|-----------|-------|-----|-----|
| 2026-09-11T12:00:00Z | 0.083678 | 0.083857 | 0.082930 |
| 2026-09-11T15:35:00Z | 0.086810 | 0.087012 | 0.085860 |
| 2026-09-11T17:00:00Z | 0.085930 | 0.086154 | 0.084590 |

(Rounded 6 dp; full floats in JSON match BT-D12A report.)

---

## E. Chart regression (unchanged scope)

No code changes in this phase. Existing research chart stack (B – Exact Origin-Density, pool bands, depth slider, navigation, pan/zoom, crosshair, overlay test, other backtesters) was **not** modified during BT-D12B.

---

## F. Browser verification

Cursor IDE browser could not reach `http://127.0.0.1:3000` (`chrome-error://chromewebdata/`). **No** screenshot or visual price-label check on the live chart.

**Not claimed:** `VISUALLY_VERIFIED`.

**Manual UI check (operator):** Research Chart → DOGEUSDT → 2026-09-11 → **B – Exact Origin-Density** → strategy **Pool Landscape SHORT · Depth 2 (Research)** → **Backtester** → confirm 9 labeled hlines and **0** position boxes.

---

## G. Verdict

| Criterion | Status |
|-----------|--------|
| Controlled restart with preflight + rollback path | **Done** |
| `latest_result.json` + 3 BT-D9 signals + 0 PnL | **Verified** |
| Overlay pipeline (9 plan hlines, 0 positions) via authenticated API | **Pass** |
| Live chart visual confirmation | **Not performed** (browser unavailable) |

**VERDICT:** `LIVE_SIGNAL_OVERLAYS_API_PASS_UI_NOT_VERIFIED`

---

## STOP

Restart and API verification complete. No further BT-D12B actions unless UI verification is requested separately.
