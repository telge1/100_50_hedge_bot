# LONG V3 Phase 3C — Existing SHORT Chart Integration

**Verdict:** `LONG_V3_PHASE3C_CHART_INTEGRATION_PASS`  
**Date:** 2026-10-30  
**Validated run:** `phase3_a_20261030T160156Z_01a05c48` (`COMPLETED_VALIDATED`)

---

## 1. Where SHORT integration lives (reference architecture)

The Research Chart backtester is implemented in **`spread_recovery_hedge_short_dev/dashboard/`** (same pattern documented in BT-D95 / BT-D105 / BT-D111).

| Layer | Path | Role |
|--------|------|------|
| Dropdown | `templates/research_charts.html` `#researchBtStrategy` | Static strategy `<option>` list |
| Backtester button | `static/js/research/research_charts.js` `#researchBacktesterBtn` | Loads overlays via frozen-month or strategy-specific POST |
| Frozen-month registry | `static/js/research/research_charts.js` `BT_FROZEN_MONTH_RESEARCH` | Maps `strategy_id` → load URL |
| Ledger → specs | `research_charts/frozen_month_research_overlay_v1.py` | Read-only JSON, validate counts, `build_drawing_specs` |
| Workspace import | `research_charts/workspace_session.py` `load_frozen_month_research_backtester` | Clears prior frozen overlays; `make_short_position` / **`make_long_position`** + `make_trend` links |
| API | `research_charts/api.py` | `GET/POST …/meta|load` + delegation from `POST /api/research/backtester/load` |
| Chart render | TRP `make_*_position` → `drawings.json` → `GET /api/research/overlays` | `.ov-position .pos-box` styling (`static/research_trp/style.css`) |

Historical **SHORT month** overlays use **short position boxes** (entry/SL/TP/exit window) plus dotted **entry→exit trend** links — two drawings per trade. No separate overlay engine.

**Note:** The `pools+ob+delta_bot/backtester/short/dashboard/` tree is an older/slimmer copy **without** frozen-month wiring; the live UI uses **`spread_recovery_hedge_short_dev/dashboard`**.

---

## 2. Reused for LONG V3 (no new overlay engine)

- `frozen_month_research_overlay_v1.py` — extended with profile `long_v3_phase3a` (`position_side=long`, long exit-reason sets).
- `load_frozen_month_research_backtester` — same loop; factory switches on `drawing_type` (`long_position` vs `short_position`).
- `BT_FROZEN_MONTH_RESEARCH` + `applyBtFrozenMonthResearchBacktester` — same Backtester click flow as June/July SHORT months.
- TRP tools: **`make_long_position`**, **`make_trend`**, existing profit/loss colors (green TP zone, red SL zone).

LONG geometry enforced on specs: **TP above entry, SL below entry** (verified in tests on TP and SL samples).

Fill times use **`fill_utc`** from ledger (mapped to internal `fill_time_utc`); **`place_utc`** stored in metadata only — not used as box start.

---

## 3. Files changed

| File | Change |
|------|--------|
| `spread_recovery_hedge_short_dev/dashboard/research_charts/frozen_month_research_overlay_v1.py` | Profile `long_v3_phase3a`, long ledger adapter, flexible TP/SL validation, `long_position` specs |
| `spread_recovery_hedge_short_dev/dashboard/research_charts/workspace_session.py` | `make_long_position` + entry color for long frozen loads |
| `spread_recovery_hedge_short_dev/dashboard/research_charts/api.py` | `bt-long-v3-phase3a/meta|load`, `/load` delegation |
| `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html` | Dropdown option + JS cache buster |
| `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js` | `BT_FROZEN_MONTH_RESEARCH.long_v3_phase3a_jun_sep_frozen_a_research` |
| `spread_recovery_hedge_short_dev/dashboard/research_charts/data/long_v3_phase3a_trade_ledger.json` | Bundled fallback (copy of validated ledger) |
| `spread_recovery_hedge_short_dev/dashboard/test_long_v3_phase3a_chart_overlay_v1.py` | Integration tests (9) |

**Research repo (data only, unchanged by this task):**  
`pools+ob+delta_bot/logs/research/long_v3_phase3_multimonth_v1/phase3_a_20261030T160156Z_01a05c48/trade_ledger.json`  
+ `run_status.json` must remain `COMPLETED_VALIDATED`.

**Not used:** failed run `phase3_a_20261030T134103Z_41a09536`; Variant B (no full four-month run).

---

## 4. Dropdown registration

| UI label | `strategy_id` | API |
|----------|---------------|-----|
| **LONG V3 — Phase 3A — Juni–September 2026 — Frozen A** | `long_v3_phase3a_jun_sep_frozen_a_research` | `POST /api/research/backtester/bt-long-v3-phase3a/load` |

User flow unchanged: open Research Chart → select strategy → **Backtester** → overlays load (same as BT-D94R SHORT).

---

## 5. Position boxes

Per trade (36 total):

1. **`longv3p3a-box-{position_id}`** — `make_long_position` from **fill** to **exit** UTC, entry/SL/TP from ledger prices, status metadata `EXIT_TP` / `EXIT_SL`.
2. **`longv3p3a-link-{position_id}`** — dotted trend entry price → exit price.

Clearing: `_clear_frozen_month_research_drawings` removes all frozen-month prefixes (SHORT months + LONG V3) before reload — no stacking on repeat Backtester click.

---

## 6. Trade count verification

| Month | Trades | TP | SL |
|--------|--------|----|----|
| Juni 2026 | 8 | 5 | 3 |
| Juli 2026 | 6 | 4 | 2 |
| August 2026 | 7 | 4 | 3 |
| September 2026 | 15 | 10 | 5 |
| **Gesamt** | **36** | **23** | **13** |

Loader: 36 normalized rows · 72 drawings (36 boxes + 36 links) · unique `position_id` per box.

---

## 7. Tests

```bash
cd spread_recovery_hedge_short_dev/dashboard
PYTHONPATH="…/spread_recovery_hedge_short_dev:…/dashboard:…/pools+ob+delta_bot" \
  pytest test_long_v3_phase3a_chart_overlay_v1.py -q
# → 9 passed
```

Includes: HTML/JS/API wiring, ledger + run_status gate, monthly distribution, long TP/SL geometry, workspace load 72 drawings, repeat-load idempotency, June SHORT regression smoke.

---

## 8. Limitations

- **Dashboard restart** required to pick up HTML/JS/API changes if the app was already running (not restarted in this session).
- **Browser visual smoke** not authenticated here; backend + workspace tests confirm drawing pipeline. Manual check: zoom to e.g. `2026-06-08T10:30:00Z` (TP) and a `STRUCTURAL_SL` trade in another month.
- **Variant B** not exposed (by design).
- **Live trading / SSOT / density / SHORT MAIN V2** untouched — visualization only.

---

**STOP** — chart integration complete; no strategy or production activation.
