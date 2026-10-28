# BT-D86A — BT-D85 Existing Dashboard Backtester Overlay V1

**VERDICT:** `BT_D85_DASHBOARD_OVERLAY_PASS`

**Research baseline:** BT-D85 `JULY_MONTH_RESEARCH_PASS` @ pools `434fbbd8`  
**Ledger SSOT:** `results/phase_bt_d85_july_2026_no_pause_research_v1/trade_ledger_no_pause.json`

## Integration summary

The existing **Backtester** button and **researchBtStrategy** dropdown on the Research Chart (`/live-charts/research`) now include:

| UI | Value |
|----|--------|
| Dropdown option | `BT-D85 · Juli 2026 · EMA V3 · 25 Trades` |
| Strategy id | `bt_d85_july_ema_v3_no_pause_research` |

**Behavior:** selecting BT-D85 and clicking **Backtester** loads **saved research trades only** (no simulation, no cache rebuild). The chart zooms to July 2026 when possible. Switching to another strategy clears BT-D85 drawings.

## Ledger validation (exact counts)

| Check | Expected | Actual |
|-------|----------|--------|
| Modeled fills | 25 | **25** |
| TP (`SIMULATED_TP`) | 10 | **10** |
| SL (`SIMULATED_SL`) | 15 | **15** |
| Ambiguous / open | 0 | **0** |

Overlay per trade: **1 short-position drawing** (entry @ `fill_time_utc`, SL/TP from ledger, exit time @ `exit_time_utc`) + **1 thin entry→exit trend line** → **50** persisted drawings total.

## Data paths

1. Primary: `POOLS_OB_DELTA_BOT_ROOT` / sibling `pools+ob+delta_bot` → BT-D85 ledger JSON  
2. Bundled fallback: `dashboard/research_charts/data/bt_d85_july_trade_ledger_no_pause.json` (copy of validated ledger)

No cache bundles or CH imports.

## Dashboard repo changes (`spread_recovery_hedge_short_dev`)

| File | Change |
|------|--------|
| `dashboard/research_charts/bt_d85_july_research_backtester.py` | Ledger load, validation, drawing specs |
| `dashboard/research_charts/data/bt_d85_july_trade_ledger_no_pause.json` | Bundled ledger |
| `dashboard/research_charts/workspace_session.py` | Load/clear BT-D85 drawings |
| `dashboard/research_charts/api.py` | `GET …/bt-d85-july/meta`, `POST …/bt-d85-july/load` |
| `dashboard/templates/research_charts.html` | Dropdown option |
| `dashboard/static/js/research/research_charts.js` | Strategy handlers, Backtester wiring |
| `dashboard/test_bt_d86a_bt_d85_dashboard_overlay_v1.py` | Unit/API/workspace tests |

## API verification (post-restart)

Process: `app.py` on **0.0.0.0:3000** (proxied by **:8080** for `dash.immotel.de`).

| Request | Result |
|---------|--------|
| `GET /api/research/backtester/bt-d85-july/meta?symbol=DOGEUSDT` | `success: true`, `trade_count: 25` |
| `POST /api/research/backtester/bt-d85-july/load` | `loaded: 50`, TP **10** / SL **15** |
| Served HTML | option `bt_d85_july_ema_v3_no_pause_research` present |

Tests: `pytest dashboard/test_bt_d86a_bt_d85_dashboard_overlay_v1.py` → **5 passed**

## Deployment / rollback

- **Restart:** SIGTERM prior PID **3016305**, started new `app.py` PID **3507185** with `PYTHONPATH` including `pools+ob+delta_bot`. Log: `/tmp/dashboard_app_3000.log`
- **Rollback:** revert scoped dashboard commits and restart `app.py` (same env/PYTHONPATH as above)

## Safety

- No strategy, EMA, density, SL/TP, or execution changes in pools research code  
- No new backtest simulations  
- No git push (either repo)

## Pools repo (this report only)

No pools code changes required for overlay; ledger already produced by BT-D85.

**STOP.**
