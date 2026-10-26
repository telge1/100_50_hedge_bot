# PHASE BT-D24A — Live BT-D23 Visibility Fix

**Verdict:** **`BT_D23_SERVER_READY_BROWSER_NOT_VERIFIED`**

---

## A. Active service (vs repo)

| Item | Finding |
|------|---------|
| **Port 3000** | `python app.py` — PID **2805125** (restarted 2026-10-25 ~16:10 UTC+3) |
| **Working directory** | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` |
| **Python** | `spread_recovery_hedge_short_dev/.venv/bin/python` |
| **Port 8080** | Reverse proxy in front (external `dash.immotel.de:8080`); local check returned **301** |
| **BT-D24 code location (before fix)** | Only under `pools+ob+delta_bot/backtester/short/dashboard/` |
| **Live deployment** | **`spread_recovery_hedge_short_dev/dashboard`** — **did not** contain BT-D23 UI/API |

**Root cause:** BT-D24 integrated the scanner into the **pools+ob** dashboard tree; production Research Charts run from the **spread_recovery** dashboard. Dropdown and routes were never deployed there.

**Data path:** `setups.json` resolved via `resolve_pools_ob_repo()` → sibling  
`/home/telgenbuescher/projects/pools+ob+delta_bot/results/setup_scanner_runs/…`  
(261 setups; file unchanged.)

---

## B. Fix applied (spread dashboard only)

Deployed to **`spread_recovery_hedge_short_dev/dashboard`**:

- `research_charts/bt_d23_setup_scanner_backtester.py` (uses `POOLS_OB_DELTA_BOT_ROOT` / sibling pools repo)
- `research_charts/api.py` — routes + `SETUP_SCANNER_BT_D23_STRATEGY_ID`
- `research_charts/workspace_session.py` — catalog + viewport sync
- `templates/research_charts.html` — strategy option + run selector + **JS cache bust** `?v=bt-d24a-setup-scanner-live-v1`
- `static/js/research/research_charts.js` — Backtester handler, viewport debounce, summary (no PnL)

**Strategy ID:** `pool_first_setup_scanner_bt_d23`  
**Label:** `Pool-First Setup Scanner · PLANNED only (BT-D23)`  
**Default run:** `bt_d23_pilot_sep11_24h_v1`

Existing Exit Pool / Pool Landscape options **unchanged**.

---

## C. Server verification (authenticated API not curl-tested)

| Check | Result |
|-------|--------|
| `discover_setup_scanner_runs()` | 1 run, **261** setups |
| `load_setups_document(bt_d23_pilot_sep11_24h_v1)` | OK |
| Workspace `load_bt_d23_setup_scanner_catalog` | `setup_count_total=261`, `source=bt_d23_setup_scanner_chart_v1` |
| Viewport sync (15:30–17:30 UTC window) | Draws subset; metadata **PLANNED ONLY** |
| Reference **15:35** levels | entry **0.08681**, SL **0.087011676**, TP **0.08586** |
| Static template | contains `pool_first_setup_scanner_bt_d23` |
| Static JS | contains `isBtD23SetupScannerStrategy` |
| `latest_result` / BT-D12 path | Module import unchanged |

**API routes registered** (require dashboard auth in browser):

- `GET /api/research/backtester/bt-d23-setup-scanner/runs`
- `POST /api/research/backtester/bt-d23-setup-scanner/load` — body: `{"symbol":"DOGEUSDT","run_id":"bt_d23_pilot_sep11_24h_v1","visible":true}`
- `POST /api/research/backtester/bt-d23-setup-scanner/sync-viewport` — body: `{"symbol":"DOGEUSDT","view_start_unix":…,"view_end_unix":…}`

Unauthenticated `curl` → **401** (expected).

---

## D. Restart

- Controlled restart of **only** the dashboard `app.py` process.
- **Required env for startup:**  
  `PYTHONPATH=/home/telgenbuescher/projects/pools+ob+delta_bot:…/dashboard`  
  (provides `pool_pattern`); optional `POOLS_OB_DELTA_BOT_ROOT` for explicit pools root.
- First restart attempt without `PYTHONPATH` failed (`ModuleNotFoundError: pool_pattern`); second attempt **successful**.

**User action:** Hard refresh Research Charts (Ctrl+F5) so `research_charts.js?v=bt-d24a-setup-scanner-live-v1` loads.

---

## E. Browser E2E

**Not verified** in this session (login required on `/live-charts/research`). After refresh you should see:

1. Fourth strategy option **BT-D23**
2. **Setup Scanner Run** selector with `bt_d23_pilot_sep11_24h_v1`
3. **Backtester** → catalog 261, overlay source **bt_d23_setup_scanner_chart_v1** (not BT-D12)
4. Planned boxes in viewport; labels **PLANNED SHORT — NOT EXECUTED**

---

## F. Parity

- `setups.json` — read-only, not modified  
- Scanner logic — not re-run  
- `latest_result.json` — not touched  
- BT-D21 trading backtester — unchanged  

---

## VERDICT

**`BT_D23_SERVER_READY_BROWSER_NOT_VERIFIED`**

STOP after deploy + server verification.
