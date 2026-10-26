# PHASE BT-D11 — Existing Backtester Overlay UI V1

**VERDICT:** `BACKTESTER_OVERLAY_UI_PARTIAL`

---

## A. Deployment path (resolved — not blocked)

| Item | Finding |
|------|---------|
| **Live process** | `spread_recovery_hedge_short_dev/.venv/bin/python app.py` (PID observed on host) |
| **Port** | `0.0.0.0:3000` (LISTEN) |
| **CWD / app root** | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` |
| **PYTHONPATH (restart script)** | `spread/dashboard` → `spread` → **`pools+ob+delta_bot`** → `trading_research_platform` |
| **Template served** | `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html` (46 429 B, Oct 24) |
| **JS served (spread)** | `spread.../static/js/research/research_charts.js` (~9106 lines) |
| **JS changed (this phase)** | `pools+ob+delta_bot/backtester/short/dashboard/static/js/research/research_charts.js` (~8309→ extended) |
| **research_charts API package** | **First** on path: `spread/dashboard/research_charts/` (not auto-synced with pools+ob copy) |

### Why “B – Exact Origin-Density” is visible in the browser but was missing in BT-D10 pools+ob template

- Checkbox `id="researchIndExactOriginDensity"` exists only in **spread** `research_charts.html` (line ~211).
- **pools+ob** `backtester/short/dashboard/templates/research_charts.html` had **no** Exact-Origin checkbox (older fork).
- BT-D10 inspected the pools+ob tree; the user’s browser hits **spread @ :3000**, which includes Phase 6XT integration (`pd1_exact_origin_density_research_v1.js`).

**Implication:** BT-D11 code was integrated in **pools+ob** `backtester/short/dashboard/`. **Port 3000 will not show BT-D11** until equivalent files are deployed to spread (or the server is restarted with pools+ob dashboard as primary). **No restart was performed** (per instruction).

---

## B. UI / API integration (pools+ob tree)

### Strategy selector

- New option: **Pool Landscape SHORT · Depth 2 (Research)**  
  `value="pool_landscape_v1_candidate_bt_d7"`
- Run dropdown: `#researchBtD7Run` (whitelist catalog only)
- Summary strip: `#researchBtD7Summary`

### APIs (additive)

| Method | Path | Role |
|--------|------|------|
| `GET` | `/api/research/backtester/bt-d7/runs?symbol=DOGEUSDT` | List validated runs from allowed result roots |
| `POST` | `/api/research/backtester/bt-d7/load` | Body: `{ symbol, run_id, visible }` — load or hide overlays |

**Security:** Only `run_id` matching `^[a-z0-9][a-z0-9_\-]{0,120}$` under:

- `results/phase_bt_d6_config_experiments_v1/*/integration_result.json`
- `results/phase_bt_d7_config_driven_short_pnl_integration_v1/*/integration_result.json`

Manifest must have `strategy_id=pool_landscape_v1_candidate`, 5m PD1, depth ≥2. **No arbitrary paths from client.**

### Backtester button workflow

1. Select strategy **Pool Landscape SHORT · Depth 2 (Research)**  
2. Choose **BT-D7 Run** from dropdown (preloaded via GET runs)  
3. Click **Backtester** → POST `bt-d7/load` → TRP drawings + optional pending entry hlines + fill/exit vlines  

**No** on-click backtest execution; **read-only** artifacts.

### Overlay Test

- Unchanged: `set_overlay_test` → `build_test_overlays` / `source=test_overlay`  
- Test: `test_overlay_test_unchanged` **PASS**  
- BT-D7 trade graphics use **drawings** (`bt_d7_short_research_overlay_v1`), not test overlays.

---

## C–E. Rendering & lifecycle

| Lifecycle | Chart behavior |
|-----------|----------------|
| `PENDING` / `SIGNAL` | Entry **hline** only (preview); **no** `short_position` box |
| `FILLED_OPEN` | `short_position` to `display_end_utc`; no realized PnL required |
| `CLOSED_TP` / `CLOSED_SL` / `CLOSED` | Full `short_position` from ledger fill/exit; PnL from `pnl.trade_ledger` |
| `CANCELLED` | No position; no PnL |

Data path: `integration_result.json` → BT-D10 `overlay_export_v1` → `trp_position_adapter_v1` → `workspace_session.import_bt_d7_short_research`.

---

## F. Backtester panel summary

`#researchBtD7Summary` shows (when present in manifest/PnL): strategy version, config hash, upper gap %, depth 2, time span, signal count, filled trades, net PnL USDT, return %, win rate, max drawdown.  
**0 trades:** message includes “0 Trades / keine realisierten Handelsgewinne”.

---

## G. Regression tests

| Test | Result |
|------|--------|
| `tests/test_bt_d7_short_research_backtester.py` (5) | **PASS** |
| `tools/phase_bt_d10_.../test_overlay_export_v1.py` (4) | **PASS** |
| `test_research_charts_interaction.py::test_stoch_backtester_positions_from_signal_list` | **PASS** |
| Browser @ :3000 | **NOT VERIFIED** (spread tree not updated) |
| Exact Origin-Density | **No code changes** in density modules |

---

## H. Changed files (pools+ob)

| File |
|------|
| `backtester/short/dashboard/research_charts/bt_d7_short_research_backtester.py` (new) |
| `backtester/short/dashboard/research_charts/api.py` |
| `backtester/short/dashboard/research_charts/workspace_session.py` |
| `backtester/short/dashboard/templates/research_charts.html` |
| `backtester/short/dashboard/static/js/research/research_charts.js` |
| `backtester/short/dashboard/tests/test_bt_d7_short_research_backtester.py` (new) |
| `backtester/short/dashboard/test_research_charts_parity.py` |
| `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/overlay_export_v1.py` |
| `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/trp_position_adapter_v1.py` |
| `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/test_overlay_export_v1.py` |

**Not changed:** BT-D4 density, canonical JS adapter, frozen candidate, SSOT, Original Short V2, existing result JSON files.

---

## I. Known limitations

1. **Deployment drift:** Live UI ≠ pools+ob dashboard until spread copies are updated and process restarted (user-approved).  
2. **Historical pilot runs:** Validated runs may have **0 fills** — only pending hlines + summary, no TP/SL boxes.  
3. **Symbol:** Server allows **DOGEUSDT** only for BT-D7 load.  
4. **Long strategy:** Adapter supports `long_position` shape only; no long signals generated.  
5. **PnL labels on chart:** Position tool shows entry/SL/TP; extra PnL text in drawing metadata — full TV-style labels depend on existing `chart.js` position label path.

---

## Next minimal deploy step (manual)

1. Copy/sync BT-D11 files from `pools+ob+delta_bot/backtester/short/dashboard/` to `spread_recovery_hedge_short_dev/dashboard/` (preserve spread-only Exact Origin blocks).  
2. User-approved restart of `app.py` on :3000.  
3. Browser smoke: select BT-D7 run → Backtester → verify drawings; toggle Overlay Test → confirm TEST overlays still appear.  

**STOP** — local integration and tests complete; no production restart performed.
