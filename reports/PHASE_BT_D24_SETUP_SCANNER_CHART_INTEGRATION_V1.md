# PHASE BT-D24 — Setup Scanner Chart Integration + Visual Audit

**Project:** `pools+ob+delta_bot`  
**Upstream:** BT-D23 `SETUP_SCANNER_READY`  
**Verdict:** **`SETUP_CHART_INTEGRATION_PASS`**

---

## 1. Architecture (unchanged overlays)

Integration uses the **existing Research Chart** host:

- **Backtester** button (`#researchBacktesterBtn`)
- **B — Exact Origin-Density** (PD1 pane, unchanged)
- **Short-position boxes** via TRP `make_short_position` (red risk / green target — existing style)
- **BT-D7 import path** pattern, separate drawing namespace for BT-D23

**Spread / live dashboard path:** `backtester/short/dashboard/` (same tree as BT-D12/BT-D7).

No changes to scanner logic, `setups.json`, density SSOT, BT-D21 trading engine, or `latest_result.json`.

---

## 2. Payload identification (stable)

| Field | BT-D12 trading overlay | BT-D23 setup scanner |
|--------|------------------------|----------------------|
| `backtester.strategy_id` | `pool_landscape_v1_candidate_bt_d7` | `pool_first_setup_scanner_bt_d23` |
| `backtester.source` | `bt_d12_latest_result_v1` / `bt_d7_short_research_overlay_v1` | `bt_d23_setup_scanner_chart_v1` |
| `backtester.payload_kind` | — | `bt_d23_setup_scanner_chart_integration_v1` |
| Drawing prefix | `btd10-short-` | `btd23-planned-` |
| Workspace key | `bt_d7_short_research` | `bt_d23_setup_scanner` |

**UI:** Strategy dropdown → **「Pool-First Setup Scanner · PLANNED only (BT-D23)」** → **Backtester** loads catalog.

---

## 3. Read-only API

| Method | Route |
|--------|--------|
| GET | `/api/research/backtester/bt-d23-setup-scanner/runs` |
| POST | `/api/research/backtester/bt-d23-setup-scanner/load` (`run_id`, `symbol`, `visible`) |
| POST | `/api/research/backtester/bt-d23-setup-scanner/sync-viewport` (`view_start_unix`, `view_end_unix`) |

**Load:** reads `results/setup_scanner_runs/<run_id>/setups.json` only; stores **full 261** setups in workspace memory (catalog).  
**Viewport:** renders overlapping setups on **5m pane** only; debounced on `on_visible_range` (pan/zoom).  
**Catalog file on disk:** never modified.

Module: `research_charts/bt_d23_setup_scanner_backtester.py`

---

## 4. Short boxes (PLANNED)

Per setup:

- Entry / SL / TP from `planned_*` fields
- Time window: `decision_utc` … +4h preview (same as BT-D10 planned horizon)
- Label: **`PLANNED SHORT — NOT EXECUTED`**
- Metadata: `setup_id`, `origin_set_key`, optional planned Δ% and R:R in `tooltip` (not USDT PnL)
- `plan_only: true`, no `FILLED` / `CLOSED` lifecycle

---

## 5. Visual reference cases (numeric parity)

From `results/phase_bt_d24_setup_chart_integration_v1/visual_reference_cases.json` (sourced from unchanged `setups.json`):

### 2026-09-11 15:35 UTC (pool lower 0.08681)

Scanner row at decision close — chart specs use **identical** `planned_entry`, `planned_sl`, `planned_tp`.  
**No earlier** setup on the same pool band at this origin composition (first structural episode at 15:35).

### 2026-09-11 17:00 UTC (pool lower 0.08593)

Setup at **17:00** with planned entry **0.08593**; aligns with 5m **decision close** timestamp (no +5m offset).  
Earlier rows near 0.08594 use **different origin sets** (not the 17:00 baseline union).

---

## 6. Quality inventory (261 setups)

See `setup_distribution.json`:

- **261** total setups in pilot window
- **~unique origin-set episodes** drive count (not a single repeated level)
- Multiple setups can share **similar entry prices** with **different origin_set_key** (dynamic origin combinations)
- Deduplication in scanner: one emission per origin episode (BT-D23); chart shows all catalog entries in viewport

---

## 7. Parity checklist

| Item | Status |
|------|--------|
| `setups.json` unchanged | ✓ |
| Scanner code unchanged | ✓ |
| BT-D21 / `short_research_engine` unchanged | ✓ |
| `latest_result.json` / BT-D12 load | ✓ (tested) |
| Other overlay namespaces | ✓ (BT-D23 prefix + clear on cross-load) |

---

## 8. Tests & artifacts

**Tests:** `backtester/short/dashboard/tests/test_bt_d24_setup_scanner_chart_integration.py` (3 passed)

**Artifacts:** `results/phase_bt_d24_setup_chart_integration_v1/`

- `api_parity.json`
- `visual_reference_cases.json`
- `setup_distribution.json`
- `integration_tests.json`

**Regenerate distribution:**  
`python3 tools/phase_bt_d24_setup_chart_integration_v1/generate_audit_artifacts.py`

---

## 9. Operator steps

1. Open Research Charts, symbol **DOGEUSDT**, enable **B — Exact Origin-Density** on 5m.
2. Strategy: **Pool-First Setup Scanner · PLANNED only (BT-D23)**.
3. Run: **bt_d23_pilot_sep11_24h_v1** (dropdown).
4. Click **Backtester** → zoom to Sep 11 window → pan/zoom to audit 15:35 / 17:00.

---

## VERDICT

**`SETUP_CHART_INTEGRATION_PASS`**

STOP — integration and tests complete. No strategy parameters, execution, or PnL simulation added.
