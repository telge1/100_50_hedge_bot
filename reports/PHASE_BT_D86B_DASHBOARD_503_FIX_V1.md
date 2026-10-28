# PHASE BT-D86B — Dashboard 503 Load Fix (BT-D85)

**Baseline:** BT-D86A `BT_D85_DASHBOARD_OVERLAY_PASS` — dashboard `6f0f27b0`, research report `c3a62350`  
**Phase:** BT-D86B  
**Date:** 2026-10-28  

## Verdict

**`BT_D85_DASHBOARD_LOAD_FIXED`**

## Root cause

With **BT-D85 · Juli 2026 · EMA V3 · 25 Trades** selected, the Backtester click executed the **legacy default branch** in `research_charts.js` (`POST /api/research/backtester/load` with `strategy_id: "stoch_fade"`, ~48h live signal feed), **not** the BT-D85 archived-ledger path (`POST /api/research/backtester/bt-d85-july/load`).

Evidence:

| Observation | Implication |
|-------------|-------------|
| UI error text **`Backtester fehlgeschlagen`** + URL **`/api/research/backtester/load`** | Matches the **stoch fallback** catch block (line ~9509), not the BT-D85 handler (`BT-D85 Laden fehlgeschlagen`). |
| API body for failing path | `503` / `signal_feed_unavailable` / **`[Errno 111] Connection refused`** from `fetch_stoch_signal_rows` → upstream collector unreachable (expected when collector is down). |
| HTML already contained BT-D85 `<option>` while script tag stayed at **`?v=phase6xt-bt-d49-box-only-v1`** | Browsers could keep **cached JS from before BT-D86A**, so `isBtD85JulyResearchStrategy()` branch never ran despite the new dropdown label. |

The July 1 short drawing seen before load was **not** proof of a successful BT-D85 load; it could be unrelated chart state. Successful BT-D85 load yields **25 trades → 50 drawings** (25 position boxes + 25 entry→exit links).

Running dashboard at **`:3000`** (PID after fix **3510837**); **`:8080`** nginx proxies to **`:3000`**.

## Fix (minimal)

1. **Cache-bust** research JS: `research_charts.js?v=phase6xt-bt-d86b-bt-d85-503-fix-v1` in `templates/research_charts.html`.
2. **JS safety net** immediately before `lastStochStrategy()` / legacy `/load`: if `btStrategy()` is BT-D85, call `applyBtD85JulyResearchBacktester(true)` and return (duplicate guard for stale bundles).
3. **API safety net** in `POST /api/research/backtester/load`: if `strategy_id` is `bt_d85_july_ema_v3_no_pause_research` (aliases included), **delegate** to `api_research_backtester_bt_d85_july_load` — no stoch/collector, no new backtest.

Legacy stoch and other dropdown strategies unchanged.

## Changed files

**Dashboard repo** (`spread_recovery_hedge_short_dev`):

- `dashboard/research_charts/api.py`
- `dashboard/templates/research_charts.html`
- `dashboard/static/js/research/research_charts.js`
- `dashboard/test_bt_d86b_dashboard_503_fix_v1.py` (new)

**Research repo** (`pools+ob+delta_bot`):

- `reports/PHASE_BT_D86B_DASHBOARD_503_FIX_V1.md` (this file)

## Data verification (SSOT ledger)

Source: `results/phase_bt_d85_july_2026_no_pause_research_v1/trade_ledger_no_pause.json`

| Metric | Expected | Actual |
|--------|----------|--------|
| Fills | 25 | 25 |
| TP (`SIMULATED_TP`) | 10 | 10 |
| SL (`SIMULATED_SL`) | 15 | 15 |
| Ambiguous | 0 | 0 |
| Open (no exit) | 0 | 0 |

Calendar spot-check (fills by `fill_time_utc` day): Jul 1 → 1, Jul 7 → 2, Jul 31 → 2 (not all 25 visible in one viewport).

Workspace load: **`loaded: 50`** (= 25 position specs + 25 link specs), **`trade_count: 25`** in summary.

## Tests

```text
pytest dashboard/test_bt_d86a_bt_d85_dashboard_overlay_v1.py dashboard/test_bt_d86b_dashboard_503_fix_v1.py -q
9 passed
```

## Deployment

- Restarted dashboard **`app.py`** on **0.0.0.0:3000** (rollback: restart previous PID / redeploy commit `6f0f27b0`).
- Post-restart HTTP (authenticated session cookie):
  - `GET /live-charts/research` → script **`phase6xt-bt-d86b-bt-d85-503-fix-v1`**
  - `POST /api/research/backtester/bt-d85-july/load` → `loaded: 50`, `strategy_id: bt_d85_july_ema_v3_no_pause_research`
  - `POST /api/research/backtester/load` with BT-D85 `strategy_id` → same delegation, **no 503**

## Browser verification

Automated Cursor browser against `http://dash.immotel.de:8080/live-charts/research` returned an **empty document** (no login session / page not rendered in automation). **Full visual overlay check (25 trades, Jul 1/7/31, repeat-click idempotency) was not completed in-browser here.** API + unit tests + served HTML cache-bust on `:8080` proxy were verified from the host.

**Operator check:** hard-refresh research page, select BT-D85, click Backtester → expect status from BT-D85 loader, **no** `/api/research/backtester/load` 503 in network tab.

## Commits

| Repo | Hash | Note |
|------|------|------|
| spread_recovery_hedge_short_dev | `da21f346` | BT-D86B fix |
| pools+ob+delta_bot | `894077f1` | Report (this file) |

## Safety checklist

- No new historical backtest  
- No ClickHouse / cache rebuild  
- No trading rule / EMA / pool / BT-D46 changes  
- No git push  
