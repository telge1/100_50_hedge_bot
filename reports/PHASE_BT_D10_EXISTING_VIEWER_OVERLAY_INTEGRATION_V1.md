# PHASE BT-D10 — Existing Viewer Overlay Integration V1

**VERDICT:** `EXISTING_VIEWER_INTEGRATION_NEEDS_APPROVAL`

**Scope:** Read-only UI inventory, overlay data contract, isolated export/adapter. **No** changes to production `research_charts.js` / `chart.js`, **no** new chart surface, **no** SSOT/density/strategy changes.

---

## 1. Executive summary

The Trading Research Platform (TRP) in this repo already renders **Entry / SL / TP short & long zones** via TRP `short_position` / `long_position` drawings (`static/research_trp/chart.js`). The **Backtester** button loads those drawings through `POST /api/research/backtester/load` (Stoch, Exit Pool, Cluster Sweep, etc.). **Overlay Test** is a **separate**, reserved path for deterministic **TEST** primitives (`trading_research_platform/overlays/samples.py`), not trade overlays.

BT-D7 short research output (`integration_result.json`) is **not** wired to any API or button yet. This phase delivers:

| Deliverable | Path |
|-------------|------|
| Overlay contract (JSON Schema) | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/overlay_contract_v1.schema.json` |
| Ledger → contract export | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/overlay_export_v1.py` |
| Contract → TRP position specs (isolated) | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/trp_position_adapter_v1.py` |
| CLI | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/export_overlay_cli_v1.py` |
| Tests | `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/test_overlay_export_v1.py` (4 passed) |

**Next step (requires explicit approval):** minimal server hook mirroring `import_stoch_backtester` + toolbar run picker — **not implemented here**.

---

## 2. Existing UI inventory (Step 1)

### 2.1 Shell & chart engine

| Item | Location |
|------|----------|
| Research page template | `backtester/short/dashboard/templates/research_charts.html` |
| Client orchestration | `backtester/short/dashboard/static/js/research/research_charts.js` |
| TRP chart canvas (positions, pan/zoom) | `backtester/short/dashboard/static/research_trp/chart.js` |
| Workspace / drawings / overlays | `backtester/short/dashboard/research_charts/workspace_session.py` |
| HTTP API | `backtester/short/dashboard/research_charts/api.py` |
| TRP Python packages | `/home/telgenbuescher/projects/trading_research_platform` via `research_charts/trp_import.py` |

### 2.2 Backtester button

| Property | Detail |
|----------|--------|
| **DOM** | `<button id="researchBacktesterBtn">` — `research_charts.html` ~L44 |
| **Handler** | `research_charts.js` ~L7577 — `addEventListener("click", …)` |
| **Behaviour** | Dispatches on `researchBtStrategy` select: Exit Pool longs, Cluster Sweep, EMA Dual Cross, EZM markers, A+ Pool Signals, default **Stoch Fade** `POST /api/research/backtester/load` |
| **Rendering** | Server adds TRP **drawings** (`long_position` / `short_position`); client `applyWorkspace` → `compose_drawings` → chart payloads (`type: "position"`, entry/stop/target, red/green fills in `chart.js` ~L4789+) |
| **No BT-D7** | No `strategy_id` for `pool_landscape_v1_candidate` / BT-D7 in select or API branch |

### 2.3 Overlay Test checkbox

| Property | Detail |
|----------|--------|
| **DOM** | `<input id="researchOverlayTest">` — label **Overlay Test** — `research_charts.html` ~L43 |
| **Client state** | `state.overlayTest`; change handler ~L6666 → `POST /api/research/overlay-test` |
| **Server** | `api.py` `api_research_overlay_test` → `workspace_session.set_overlay_test` |
| **Active function** | **Yes — reserved:** `_reload_test_overlays` removes overlays where `is_test_overlay`, then adds `build_test_overlays(symbol)` from TRP `overlays/samples.py` (TEST hline/vline/zone/marker; metadata `source: test_overlay`) |
| **Must not overwrite** | BT-D10 must **not** repurpose this checkbox alone; extend with e.g. `overlay_mode: test \| bt_d7_research` or separate **Research Short BT** toggle after approval |

### 2.4 Chart B — Exact Origin-Density

| Property | Detail |
|----------|--------|
| **User expectation** | Checkbox **B – Exact Origin-Density** on main research chart |
| **In this repo copy** | `research_charts.html` / `research_charts.js` here **do not** contain `researchIndExactOriginDensity` (grep empty). Integration tests target `spread_recovery_hedge_short_dev/dashboard` (`test_pd1_density_research_live_integration_v1.py`) |
| **Related local assets** | `templates/pd1_landscape_viewer.html` (standalone viewer option B), POC under `research_charts/live_micro_lld_v2_ch_test/...` |
| **BT-D10 impact** | Overlay export is **orthogonal** to density canvas; no change to Exact Origin module when wired via **drawings** only |

### 2.5 Existing overlay / marker primitives

- **OverlayManager** (`OverlayLine`, `OverlayMarker`, `OverlayZone`) — used for LLD, scanners, **Overlay Test**
- **DrawingManager** position tools — `make_short_position` / `make_long_position` — used by backtesters
- **Client** `syncOverlays` / `chart.addOverlay` / `chart.layoutOverlays` — `research_charts.js` ~L1190+
- **Backtest signal APIs** | `POST /api/research/backtester/load`, `POST /api/research/backtester/run`, `GET /api/research/overlays`

---

## 3. BT-D4 / D6 / D7 / D8 stack (Step 2 — data source)

| Phase | Role | Key path |
|-------|------|----------|
| **BT-D4** | `ResearchDensityRunV1.collect_at_close` — 5m PD1 exact unique-origin depth ≥2 | `tools/phase_bt_d4_enforced_depth2_backtester_v1/research_backtester_v1.py` |
| **BT-D6** | Config schema, PnL aggregator, manifest hash | `tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1/` |
| **BT-D7** | Short research engine, signals/orders/trades JSON | `tools/phase_bt_d7_config_driven_short_pnl_integration_v1/short_research_engine_v1.py` |
| **BT-D8** | Validated density cache (optional `density_source: validated_cache`) | `tools/phase_bt_d8_canonical_density_cache_v1/` |

**Strategy (unchanged):** `pool_landscape_v1_candidate`, `NOT_FINAL_APPROVED`, TP `next_eligible_band` **EXPERIMENTAL**.

**Ledger shape (authoritative for overlay):** `integration_result.json` — fields `signals`, `orders`, `trades`, `pnl.trade_ledger`, `manifest`, `config_hash`.

Example on disk:  
`results/phase_bt_d6_config_experiments_v1/historical_pilot_24h_ac6147168271_20261025T102359Z/integration_result.json` (signals + pending/cancelled orders, **zero trades**).

---

## 4. Overlay contract (Step 2)

**Version:** `bt_d10_overlay_contract_v1`  
**Schema:** `tools/phase_bt_d10_existing_viewer_overlay_integration_v1/overlay_contract_v1.schema.json`

Per-record fields (required subset): `signal_id`, `run_id`, `strategy_config_hash`, `symbol`, `signal_time_utc`, `side`, `lifecycle_state`, `pending_order_status`, `entry_price`, optional `sl_price` / `tp_price`, fill/exit times, `realized_*` from **ledger only**, `density_provenance`.

**Lifecycle mapping (from ledgers only):**

| State | Condition |
|-------|-----------|
| `CLOSED` | Trade row with `exit_utc` + `exit_price` |
| `FILLED` | Trade row, open (no closed exit in trade row) |
| `PENDING` | `PENDING_PLACED` order, no trade |
| `CANCELLED` | Order status e.g. `CANCELLED_POOL_INACTIVE` |
| `SIGNAL` | Signal without qualifying order |

**Rules enforced in export:**

- Pending **without** fill → **no** TRP position spec (no fake closed box).
- `sl_price` / `tp_price` on overlay record only when present on **trade** ledger (no TP recompute in export).
- `realized_net_pnl_usdt` / `realized_pnl_pct` only from `pnl.trade_ledger` join on `trade_id`.
- `display_end_utc` for pending: `order_active_from + 4h` (display-only horizon; documented in code).

---

## 5. Chart rendering plan (Step 3 — after approval)

Use **existing** `short_position` drawing pipeline (same as Stoch / Exit Pool):

1. Approved API loads overlay bundle or reads `overlay_bundle_bt_d10_v1.json` from a selected run directory.
2. `trp_position_adapter_v1.overlay_bundle_to_position_specs` → specs compatible with `signal_to_position_spec` shape.
3. New workspace method (proposed): `import_bt_d7_short_research(symbol, specs, meta)` — clone of `import_stoch_backtester` with `origin: bt_d7_short_research_overlay_v1` and prefix `btd10-short-`.
4. **Backtester** button: add `<option value="pool_landscape_v1_candidate_bt_d7">` + branch calling new load endpoint with `run_id` / path.
5. **Overlay Test** extension (proposed): when `researchOverlayTest` **and** `state.btD7OverlayEnabled`, show/hide BT-D7 drawings; leave `build_test_overlays` path intact when only classic Overlay Test is on.

**Short geometry (already in chart.js):** red loss zone entry→SL, green reward entry→TP; labels via `computePositionStats` / `formatPositionLabels`.

**Pan/zoom:** Unchanged — drawings are time-scoped; reuse `syncChartAfterBacktest` / `zoomChartToIsoRange` patterns from Exit Pool loader.

---

## 6. Proposed API additions (not implemented — needs approval)

```http
POST /api/research/backtester/bt-d7/load
{
  "symbol": "DOGEUSDT",
  "integration_result_path": "results/.../integration_result.json",
  "visible": true
}
```

Response: workspace `snapshot()` + `backtester: { source: "bt_d7_short_research_overlay_v1", loaded, run_id, message }`.

Optional:

```http
GET /api/research/backtester/bt-d7/runs?symbol=DOGEUSDT
```

Lists directories containing `integration_result.json` under `results/phase_bt_d6_config_experiments_v1/` and `results/phase_bt_d7_*`.

---

## 7. Tests (Step F)

| Test | Status |
|------|--------|
| Overlay prices = ledger (`minimal_closed_trade_v1.json`) | **PASS** |
| Pending not exported as position spec | **PASS** |
| Short SL above entry, TP below entry | **PASS** |
| Historical pilot → pending/cancelled only, zero position specs | **PASS** |
| Frontend recompute | **N/A** (no frontend change) |
| Pan/zoom / Exact Origin / 15m PD1 | **N/A** until wiring; contract does not touch density |

Run:  
`python3 -m pytest tools/phase_bt_d10_existing_viewer_overlay_integration_v1/test_overlay_export_v1.py -q`

Export example:  
`python3 tools/phase_bt_d10_existing_viewer_overlay_integration_v1/export_overlay_cli_v1.py results/.../integration_result.json`

---

## 8. Why `NEEDS_APPROVAL` (not `READY` / `BLOCKED`)

| Criterion | Status |
|-----------|--------|
| Isolated contract + adapter | **Done** |
| UI inventory + integration plan | **Done** |
| Production viewer JS/canvas | **Not modified** (per instruction) |
| Backtester button / Overlay Test wiring | **Not implemented** |
| Exact Origin checkbox in **this** tree | **Drift** vs spread dashboard tests — verify deployment target before UI work |
| BT-D7 synthetic config validation | **Blocked** in current tree (`synthetic_fixture_v1.json` missing backtest keys) — does not block overlay contract |

**Not blocked:** Ledger export path is clear; TRP position renderer exists; no second chart engine required.

---

## 9. Isolated artifacts summary

```
tools/phase_bt_d10_existing_viewer_overlay_integration_v1/
  overlay_contract_v1.schema.json
  overlay_export_v1.py
  trp_position_adapter_v1.py
  export_overlay_cli_v1.py
  test_overlay_export_v1.py
  fixtures/minimal_closed_trade_v1.json
```

**STOP** — no production viewer changes, no strategy/SSOT/density edits.
