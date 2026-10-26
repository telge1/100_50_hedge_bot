# PHASE BT-D48A — FIX BT-D46 CHRONOLOGICAL LIVE API 404

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Live URL:** `http://dash.immotel.de:8080/live-charts/research`

## Verdict

**BT_D48A_SERVER_READY_BROWSER_NOT_VERIFIED**

## A. Root cause

| Observation | Result |
|-------------|--------|
| UI/HTML/JS on disk | BT-D46 option + `POST /api/research/backtester/bt-d46-chronological/load` (correct path, no `/live-charts` prefix on API) |
| Fresh `import app` | 3 BT-D46 routes registered |
| **Running** `:3000` process (PID **2989228**, started **13:22**) before fix | `POST …/bt-d46-chronological/load` → **404**; `POST …/bt-d26-dynamic-selector/load` → **401** |
| After controlled restart (PID **3010691**, **14:57**) | BT-D46 `load` / `meta` → **401** without session (**route present**); **200** with session |

**Conclusion:** Not a wrong URL prefix, HTTP method, or missing code. The live **dashboard `app.py` process was stale** — started before BT-D48 route handlers were loaded. Nginx `:8080` proxies `/api/` to `:3000` correctly (external BT-D46 `load` returns **401**, not **404**, after restart).

## B. Minimal fix

1. **Restart** dashboard on port 3000 with existing `PYTHONPATH` (includes `pools+ob+delta_bot` for overlay SSOT).
2. **JS (owned drawings):** `syncResearchBacktesterLayersOnStrategyChange` — on switch to BT-D46, `visible:false` clear for BT-D26/BT-D23; on load BT-D46, clear BT-D26 first. Asset bump: `phase6xt-bt-d48a-live-404-fix-v1`.
3. **No** changes to BT-D46/BT-D47 results, strategy, or density math.

API contract unchanged:

- `GET /api/research/backtester/bt-d46-chronological/meta`
- `POST /api/research/backtester/bt-d46-chronological/load`
- `POST /api/research/backtester/bt-d46-chronological/sync-viewport`

## C. Registered vs requested route

| Requested (JS) | Registered (FastAPI) |
|----------------|----------------------|
| `POST /api/research/backtester/bt-d46-chronological/load` | Same |
| `GET /api/research/backtester/bt-d46-chronological/meta` | Same |
| `POST /api/research/backtester/bt-d46-chronological/sync-viewport` | Same |

Research page: `GET /live-charts/research` (HTML only).

## D. Live HTTP verification (localhost :3000, authenticated session)

| Call | HTTP | Notes |
|------|------|-------|
| GET meta `DOGEUSDT` | 200 | `success: true`, `marker_count_total: 38` |
| POST load | 200 | 19/14/4/1 event counts |
| POST sync-viewport (pilot window) | 200 | viewport markers rendered |

## E. Replay checks (server module)

- GO TO **2026-09-11 15:35 UTC** (`as_of_unix`): **no** `SIMULATED_ENTRY` at 15:35 for unfilled 0.08681 pending; **0** future markers after as-of in filtered set.

## F. Tests

- `dashboard/test_bt_d48a_live_route_registration_v1.py` — route registration + live listener not 404
- `dashboard/test_bt_d48_chronological_backtester_chart_v1.py` — updated for D48A JS bump  
- **9 passed** (2026-10-26)

## G. Browser checklist (manual)

1. Hard-refresh research page (confirm `research_charts.js?v=phase6xt-bt-d48a-live-404-fix-v1`).
2. Select **Chronological Short Backtester · BT-D46** → prior BT-D26 boxes should clear on strategy change.
3. **Backtester** → load succeeds (no 404); markers appear after viewport sync.
4. **B** density ON/OFF while switching BT-D26 ↔ BT-D46.

## Operational note

After any BT-D48+ Python API change, restart the `:3000` dashboard process (e.g. `systemctl restart hedgebot-dashboard` or controlled `app.py` restart). A **404** on BT-D46 while BT-D26 returns **401** is a strong signal of a **stale process**, not a client URL bug.
