# PHASE BT-D38 — Activate Current Batch Code and Verify Live HTTP

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Pool repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Live URL:** `http://dash.immotel.de:8080/live-charts/research`

---

## VERDICT

**`LIVE_FAST_BATCH_PARTIAL`**

| Criterion | Result |
|-----------|--------|
| Live HTTP cold window (batch + ~5 s) | **Verified** |
| Warm `response_cache.hit` | **Verified** |
| Runtime capabilities `batch_prefill_gate_enabled` | **Verified** (after fix) |
| BT-D36 research assets served | **Verified** |
| Authenticated browser density | **NOT_VERIFIED** (`BROWSER_NOT_VERIFIED`) |

---

## A. Pre-restart

| Item | Value |
|------|--------|
| **PID** | `2979403` → listener `:3000` |
| **Issue (BT-D37A)** | Stale gate in loaded `historical_window_v1` (opt-in batch) |
| **CWD / venv** | `.../spread_recovery_hedge_short_dev/dashboard`, `.venv/bin/python app.py` |
| **PYTHONPATH** | spread_recovery + dashboard + `pools+ob+delta_bot` |
| **Active window builds** | None in progress at first stop (last slow log 12:59) |
| **Logs** | `results/phase_bt_d38_live_batch_v1/dashboard_pre_restart.log`, `pre_restart_env.txt` |
| **Trading / CH** | Only dashboard `app.py` restarted; no bot or ClickHouse service changes |
| **On-disk modules** | `historical_window_v1.py` has `_landscape_phase5s_prefill_enabled()` (uncommitted vs HEAD) |

---

## B. Restarts and root cause

### Restart sequence

| Step | PID | Note |
|------|-----|------|
| BT-D38 stop/start #1 | `2985188` | Reload alone **insufficient** — still legacy ~177 s windows |
| Stop/start #2 (pycache purge) | `2987734` | Still legacy path |
| **Fix + restart #3** | **`2989228`** | **Active** |

### Root cause (confirmed)

Importing `research_charts.api` runs `short_pool_oracle_v1` → `pool_pattern.market.ensure_paths()`, which prepends:

`pools+ob+delta_bot/backtester/short/dashboard`

That **shadows** `spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1` with the **pools backtester mirror** (committed API, opt-in `historical_lmv2_batch_reader_enabled()`, no BT-D35/D37 window service).

Evidence after `import app` (pre-fix):

- `pd1_landscape_viewer_v1.historical_window_v1.__file__` → `.../pools+ob+delta_bot/backtester/short/dashboard/...`
- HTTP capabilities: 20 keys, **no** `runtime`
- HTTP window: ~175 s, `phase5s_batch_reader_active=false`

### Minimal correction (dashboard only)

In `app.py`, **re-insert** `_dashboard_dir` on `sys.path` immediately **before** `pd1_landscape_viewer_v1` import (after `research_charts.api` import). One-line intent + `sys.path.insert(0, str(_dashboard_dir))`.

No systemd changes.

---

## C. Runtime verification (PID `2989228`)

`GET /live-charts/viewer/api/capabilities` → **200** (~0.3 s)

```json
"runtime": {
  "batch_prefill_gate_enabled": true,
  "lifetime_builder_version": "pd1_landscape_lifetime_v1_phase5s_batch_default",
  "builder_module_file": ".../spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/historical_window_v1.py",
  "window_response_cache_contract": "bt_d34_pd1_window_response_cache_v1"
}
```

Artifact: `results/phase_bt_d38_live_batch_v1/capabilities_post_restart.json` (post-fix run; re-fetch after final PID matches above).

---

## D. Cold window HTTP

**POST** `/live-charts/viewer/api/window`  
Payload: Sep-11 2026 8 h, DOGEUSDT 5m PD1, `response_mode=exact_origin_v1` (canonical).

| Metric | Value |
|--------|--------|
| HTTP | **200** |
| Client wall | **4.74 s** |
| `phase5s_batch_reader_active` | **true** |
| `batch_prefill_gate_enabled` | **true** |
| `membership_loader` | `phase5s_episode_walk_batch_cache` |
| `timing_ms.phase5s_prefill` | 4310.1 |
| `timing_ms.build_lifetimes` | 276.2 |
| `timing_ms.total` | 4689.7 |
| `pool_count` | **262** |
| `response_cache.hit` | **false** (cold) |
| `key_digest` | `e2fd55f290d21143` (matches BT-D37A CLI reference) |

Artifact: `results/phase_bt_d38_live_batch_v1/window_cold.json`

---

## E. Warm cache

Identical payload, second POST:

| Metric | Value |
|--------|--------|
| HTTP | **200** |
| Client wall | **~5.4 ms** |
| `response_cache.hit` | **true** |
| `key_digest` | `e2fd55f290d21143` |
| Pool count / segment parity vs cold | **262 / match** |

Artifact: `results/phase_bt_d38_live_batch_v1/window_warm.json`

---

## F. Stability (spot checks)

| Probe | Result |
|-------|--------|
| `GET /live-charts/viewer/api/capabilities` | 200, &lt;1 s |
| `GET /` | 302, &lt;1 ms |
| `GET /live-charts/research` | 200, ~10 ms; assets `phase6xt-bt-d36-density-supersede-v1` |
| `POST /api/research/backtester/bt-d26-dynamic-selector/load` | 200, ~30 ms |
| Slow window middleware after fix | **No** new ~170 s window lines on final PID |
| Window 429 storm | **Not** observed during bounded tests |

---

## G. Browser

No authenticated browser session in this phase → **`BROWSER_NOT_VERIFIED`**.

---

## H. Active process (end state)

| Field | Value |
|-------|--------|
| **PID** | `2989228` |
| **Listen** | `0.0.0.0:3000` |
| **PYTHONPATH** | unchanged (includes `pools+ob+delta_bot`) |
| **Imported `historical_window_v1`** | `spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/historical_window_v1.py` |

---

## Scope

No trading / Entry / SL / TP / density SSOT / warmup / strategy changes. No new backtests. Single controlled cold + warm window POST after fix.

---

**End of BT-D38.**
