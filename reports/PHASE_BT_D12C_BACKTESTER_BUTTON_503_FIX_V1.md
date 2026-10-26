# PHASE BT-D12C — Fix Live Backtester Button 503 / Connection Refused V1

**Date:** 2026-10-25  
**Verdict:** `BACKTESTER_BUTTON_503_FIX_API_PASS_UI_NOT_VERIFIED`

---

## A. Root cause (evidence)

| # | Finding | Evidence |
|---|---------|----------|
| 1 | `#researchBacktesterBtn` in served JS **does** call `POST /api/research/backtester/bt-d12/load-latest` when `btStrategy() === pool_landscape_v1_candidate_bt_d7` | `research_charts.js` (spread worktree) lines ~8360+ |
| 2 | User-visible error format `503 … (/api/research/backtester/load)` matches `httpError()` on **generic** load, not the BT-D12 catch text | `httpError` in `research_charts.js` |
| 3 | **Before fix:** `POST /api/research/backtester/load` with `strategy_id: pool_landscape_v1_candidate_bt_d7` → **503** `signal_feed_unavailable` / `[Errno 111] Connection refused` | Repro on `:8080` and `:3000` (authenticated) |
| 4 | **Cause of 503:** `api_research_backtester_load` had **no** branch for pool landscape → fell through to `fetch_stoch_signal_rows` (dead collector upstream) | `research_charts/api.py` pre-D12C |
| 5 | **BT-D12 path OK:** `POST …/bt-d12/load-latest` → **200**, 9 aux lines, 0 positions | BT-D12B + D12C probes |
| 6 | **Stale browser JS risk:** HTML still served `research_charts.js?v=phase6xt-density-draw-tools-v1` while BT-D12 button logic was added later → clients could keep **old** bundle without the BT-D12 branch and hit stoch `/load` on click | Same cache-bust string before D12C |
| 7 | Proxy `:8080` → backend `:3000` behaves consistently for both endpoints (no split routing) | Matched status codes on 8080 vs 3000 pre-fix |
| 8 | Strategy `change` handler always fired four generic `/load` calls (unrelated strategies); not the 503 for pool landscape, but noisy | `research_charts.js` ~8190 |

**Not claimed:** live chart screenshot / visual label check (no IDE browser to `dash.immotel.de:8080`).

---

## B. Correct behaviour (implemented)

For `pool_landscape_v1_candidate_bt_d7`:

- **Primary:** `POST /api/research/backtester/bt-d12/load-latest` from `#researchBacktesterBtn` via `applyBtD12PoolLandscapeBacktester()`.
- **Defence:** same BT-D12 import if legacy clients POST `/api/research/backtester/load` with that `strategy_id` (no stoch collector).
- **Strategy change:** no generic `/load` fan-out when pool landscape is selected.
- Other strategies unchanged (exit pool, cluster sweep, stoch, etc.).

---

## C. Result load

| Field | Value |
|-------|--------|
| Source | `results/backtester_runs/latest_result.json` |
| Run | `bt_d9_24h_import_v2_snapshots` |
| Signals | 3 |
| Filled positions | 0 |
| Entry/SL/TP hlines | 9 (`loaded_aux`) |
| Net PnL | 0 USDT |

Verified after restart via **8080** authenticated `POST …/load` with pool landscape `strategy_id`.

---

## D. Density chart

Server workspace import unchanged (BT-D12A): TRP hlines via `import_bt_d7_short_research` → `loaded_aux: 9`, `loaded_positions: 0`.  
**UI visibility** on B – Exact Origin-Density not visually confirmed in this phase.

---

## E. Minimal fix + backups

**Backups (spread dashboard):**

- `research_charts/api.py.bak_bt_d12c_20261025`
- `static/js/research/research_charts.js.bak_bt_d12c_20261025`
- `templates/research_charts.html.bak_bt_d12c_20261025`

**Changes:**

1. `research_charts/api.py` — `_bt_d12_latest_workspace_response()`; pool landscape routed on `/load` and `/bt-d12/load-latest`.
2. `static/js/research/research_charts.js` — `applyBtD12PoolLandscapeBacktester()`; strategy-change guard; cache-related flow cleanup.
3. `templates/research_charts.html` — script bust `?v=bt-d12c-pool-landscape-backtester-v1`.
4. Mirror API helper in `pools+ob+delta_bot/backtester/short/dashboard/research_charts/api.py` (parity/tests only).

**Rollback:** restore `.bak_bt_d12c_*`; restart dashboard (same PYTHONPATH / `HISTORICAL_LMV2_BATCH_READER=1` as BT-D12B).

---

## F. Tests

| Test | Result |
|------|--------|
| `test_bt_d12c_pool_landscape_load_route.py` (helper + source routing) | PASS |
| `test_bt_d12_latest_backtester.py` | PASS |
| `py_compile` `api.py` | OK |
| **8080** `POST …/load` pool landscape | **200**, `bt_d12_latest_result_v1`, `loaded_aux=9` |
| **8080** `POST …/bt-d12/load-latest` | **200** (unchanged) |
| **8080** served HTML script bust | contains `bt-d12c-pool-landscape-backtester-v1` |
| **8080** served JS | contains `applyBtD12PoolLandscapeBacktester` |
| Stoch `/load` without job | still **503** (expected; collector down) |

---

## G. Restart

| Item | Value |
|------|--------|
| Reason | Load `api.py` changes |
| Old PID | 2757702 |
| New PID | **2767403** |
| Port | 3000 (single listener) |

---

## H. Verdict

| Criterion | Status |
|-----------|--------|
| Root cause identified with HTTP repro | **Yes** |
| Minimal fix (API + JS + cache bust) | **Done** |
| Real browser path **8080** API | **Pass** |
| Visual chart verification | **Not done** |

**VERDICT:** `BACKTESTER_BUTTON_503_FIX_API_PASS_UI_NOT_VERIFIED`

**Operator:** Hard-refresh `/live-charts/research` (confirm new `research_charts.js?v=bt-d12c-pool-landscape-backtester-v1`), select **Pool Landscape SHORT · Depth 2 (Research)**, click **Backtester** — expect status from BT-D12 message and 9 plan lines on 2026-09-11.

---

## STOP

No further backtester/strategy work in BT-D12C scope.
