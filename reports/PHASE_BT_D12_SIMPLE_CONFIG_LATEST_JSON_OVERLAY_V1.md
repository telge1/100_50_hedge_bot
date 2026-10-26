# PHASE BT-D12 — Simple Config-Driven Backtester + Latest JSON Chart Overlay V1

**Date:** 2026-10-25  
**Verdict:** `SIMPLE_BACKTESTER_READY_FOR_RESTART`

---

## Summary

Config-only CLI backtester with versioned `result.json` + atomic `latest_result.json`, and a **targeted merge** into the live Spread Research Dashboard (`spread_recovery_hedge_short_dev/dashboard/`) so the existing **Backtester** button loads overlays on **B – Exact Origin-Density** charts — **no** UI date fields, **no** run selector, **no** backtest-on-click.

**Dashboard server was not restarted** (per instruction). After restart, select **Pool Landscape SHORT · Depth 2 (Research)** → **Backtester**.

---

## A. Config (BT-D6 extension)

| Field | Rule |
|-------|------|
| `backtest.symbol` | **Required** (e.g. `DOGEUSDT`) |
| `backtest.start_utc` / `end_utc` | Strict `YYYY-MM-DDTHH:MM:SSZ`, 5m-aligned, `start < end` |
| `backtest.end_utc_exclusive` | **Required** boolean |
| `backtest.symbols` | Optional; must match `symbol` if set |

All existing strategy/PnL/density fields unchanged. Depth 2 / 5m PD1 / exact unique-origin remain locked.

Example: `tools/phase_bt_d12_simple_config_latest_json_overlay_v1/configs/example_bt_d12_24h_v1.json`

---

## B. CLI

```bash
export HISTORICAL_LMV2_BATCH_READER=1
python tools/phase_bt_d12_simple_config_latest_json_overlay_v1/run_simple_backtest.py \
  --config tools/phase_bt_d12_simple_config_latest_json_overlay_v1/configs/example_bt_d12_24h_v1.json
```

- Uses BT-D7 engine + BT-D6 validator + `canonical_provider` or `validated_cache`
- Cache: fail-closed if bundle missing or incomplete (no silent recompute in CLI)
- Does **not** auto-build large caches

---

## C. JSON results

| Path | Role |
|------|------|
| `results/backtester_runs/<run_id>/result.json` | Immutable per-run artifact |
| `results/backtester_runs/latest_result.json` | Dashboard pointer (atomic replace on success only) |

**BT-D9 import (no recompute):** `bt_d9_24h_import_v1` — 3 signals, 5 orders, 0 trades, 0 USDT PnL.

```bash
python tools/phase_bt_d12_simple_config_latest_json_overlay_v1/import_bt_d9_result.py
```

---

## D. Spread dashboard merge (backups)

| Backup | Path |
|--------|------|
| API | `spread.../research_charts/api.py.bak_bt_d12_20261025` |
| JS | `spread.../static/js/research/research_charts.js.bak_bt_d12_20261025` |

**Added / updated (spread):**

- `research_charts/bt_d12_latest_backtester.py` — reads `pools+ob/results/backtester_runs/latest_result.json` via `POOLS_OB_DELTA_BOT_ROOT` or sibling path
- `research_charts/bt_d7_short_research_backtester.py` — TRP drawing builder (copied from pools+ob)
- `workspace_session.py` — `import_bt_d7_short_research` / clear helpers
- `POST /api/research/backtester/bt-d12/load-latest` — read-only overlay load
- Strategy option: **Pool Landscape SHORT · Depth 2 (Research)**
- `#researchBacktesterBtn` → `bt-d12/load-latest` (no run dropdown)

**pools+ob** mirror: same API/JS/bt_d12 module under `backtester/short/dashboard/`.

---

## E. Overlays

BT-D10 `overlay_export_v1` → TRP drawings (pending entry hlines, fill/exit markers, short positions when filled). Density layers unchanged; Overlay Test unchanged.

---

## F. BT-D9 verification

| Check | Result |
|-------|--------|
| 3 signals in `latest_result.json` | PASS |
| 0 trades, 0 USDT net | PASS |
| Overlay records | 3 (`PENDING` / cancelled lifecycle) |

---

## G. Tests

| Suite | Result |
|-------|--------|
| `tools/phase_bt_d12_.../tests` | PASS |
| BT-D6 / BT-D7 integration | PASS |
| `backtester/short/dashboard/tests/test_bt_d12_latest_backtester.py` | PASS |

---

## H. Rollback

```bash
cd spread_recovery_hedge_short_dev/dashboard
cp research_charts/api.py.bak_bt_d12_20261025 research_charts/api.py
cp static/js/research/research_charts.js.bak_bt_d12_20261025 static/js/research/research_charts.js
# remove bt_d12_latest_backtester.py, bt_d7_short_research_backtester.py if needed
```

---

## Limitations

- TP plan lines for unfilled signals may show entry hline only (no density bands in JSON for display TP).
- Browser verification requires **dashboard restart** (not done in this phase).
- Not a proof of profitable strategy.

---

## Verdict token

**`SIMPLE_BACKTESTER_READY_FOR_RESTART`**
