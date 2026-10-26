# PHASE BT-D23 — Pool-First Short Setup Scanner

**Project:** `pools+ob+delta_bot`  
**Scope:** Planned Entry / SL / TP only — no orders, fills, positions, exits, or PnL  
**Signal logic:** `pool_first_structural_v1` (same `_structural_eligible_upper` gates as BT-D21)  
**Verdict:** **`SETUP_SCANNER_READY`**

---

## Deliverables

| Item | Location |
|------|----------|
| Scanner core | `tools/phase_bt_d23_pool_first_setup_scanner_v1/setup_scanner_v1.py` |
| Dedup | `tools/phase_bt_d23_pool_first_setup_scanner_v1/setup_dedup_v1.py` |
| CLI | `tools/phase_bt_d23_pool_first_setup_scanner_v1/run_setup_scanner.py` |
| Chart adapter (read-only) | `tools/phase_bt_d23_pool_first_setup_scanner_v1/chart_adapter_v1.py` |
| Tests | `tools/phase_bt_d23_pool_first_setup_scanner_v1/tests/` |
| Pilot output | `results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/setups.json` |
| Chart bundle | `results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/chart_overlay_bundle.json` |

**Not modified:** trading backtester (`short_research_engine_v1`), `latest_result.json`, LMV2 Short V2, baseline `configs/backtester.yaml`.

---

## Run

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot
PYTHONPATH=tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1:tools/phase_bt_d7_config_driven_short_pnl_integration_v1:tools/phase_bt_d8_canonical_density_cache_v1:tools/phase_bt_d23_pool_first_setup_scanner_v1:tools/phase4d_pool_landscape_backtester_v2 \
  python3 tools/phase_bt_d23_pool_first_setup_scanner_v1/run_setup_scanner.py \
  --config configs/pilot_bt_d21_pool_first_24h.yaml \
  --run-id bt_d23_pilot_sep11_24h_v1
```

Config: same 24h window and validated cache as BT-D21 pilot (`2026-09-11T12:00:00Z` … `2026-09-12T12:00:00Z`).

---

## Behaviour

### Strategy

- **5m PD1**, `exact_unique_origin`, **depth ≥ 2**
- Density: validated cache / provider with **168h `warmup_hours`** and older ACTIVE origins (unchanged SSOT contract; noted in `density_warmup_note` in JSON)
- **All** upper bands passing `_structural_eligible_upper` per bar (not only the single lowest pool chosen by the trading engine)
- No bearish, proximity, touch, pending slot, `position_open_until`, cancel, or fill simulation

### Entry / SL / TP

- Entry: `compute_short_entry_level` (`pool_lower_edge` or `pool_lower_edge_offset`, e.g. ×0.999 for 0.1%)
- SL: `short_sl(pool_upper, sl_above_upper_top_pct)`
- TP: `select_short_tp_depth2` — **`next_eligible_band`** (flagged in JSON as *not* dense lower-cluster selection)

### Setup status

Every row: `"status": "PLANNED"` — no FILLED, trades, or PnL fields.

### Dedup (explicit, not trading `pool_signal_hold`)

| Event | Meaning |
|--------|---------|
| `FIRST_STRUCTURAL_ELIGIBILITY` | Origin set first passes gates in an active density episode |
| `UNCHANGED_ORIGIN_SET` | Same origin set still active — suppressed (no per-bar spam) |
| Episode end | Origin set absent from snapshot under `origin_set_v1` → may emit again on return (`FIRST_STRUCTURAL_ELIGIBILITY`) |

Identity key: sorted unique origin IDs (`origin_set_identity_key`). Band price drift does not re-list the same origin episode.

---

## Pilot results (24h cache, Sep 11–12 2026)

| Metric | Value |
|--------|--------|
| **Setup count** | **261** |
| **Earliest decision** | `2026-09-11T12:00:00Z` |
| **Config hash** | `4822db251577…` (same normalized pilot as BT-D21) |

### Reference landscapes (BT-D19 baseline times)

**2026-09-11 15:35 UTC — upper pool ~0.08681 / 0.086838**

- Scanner **first** lists this origin composition at **`2026-09-11T15:35:00Z`** (`FIRST_STRUCTURAL_ELIGIBILITY`).
- **Not** recognized earlier: before 15:35 the exact origin union for that band is **not** present in the causal upper landscape (pool band appears at that close — same as BT-D19 timeline). No retroactive setup.
- Not blocked by pending slots (scanner ignores trading lifecycle).

**2026-09-11 17:00 UTC — baseline pool ~0.08593 / 0.085982**

- First setup at **17:00** with origins `…1789145100|…1789145700` at planned entry **0.08593**.
- Earlier **12:00** / **12:40** setups near **0.08594** use **different origin sets** (not the 17:00 baseline pair `1789140600|1789140900`). Origins `1789140600` / `1789140900` first appear together in scanner output at **15:35** / **15:40** on **other** band geometry — not the 17:00 touch band before price/structure allowed it.

---

## Chart

`chart_overlay_bundle.json` is compatible with `trp_position_adapter_v1.overlay_bundle_to_position_specs` (verified in integration test). Labels: **`PLANNED SHORT — NOT EXECUTED`**, `plan_only: true`, no USDT PnL.

**Not wired** into production dashboard API (`bt_d7_short_research_backtester.discover_runs` unchanged). Use adapter locally or extend allowlist in a future integration phase.

Research chart: load PD1 exact-origin density (B panel) and import position specs from the bundle the same way as BT-D10/BT-D12 planned boxes.

---

## Tests

```
tools/phase_bt_d23_pool_first_setup_scanner_v1/tests/
  test_setup_scanner_v1.py          — dedup, entry offset, fixture scan
  test_setup_scanner_integration_v1.py — 24h cache, no PnL, chart specs
```

**6 passed** (including cache integration).

---

## VERDICT

**`SETUP_SCANNER_READY`**

STOP — scanner, tests, and 24h pilot complete. No PnL simulation, no trading backtest changes.
