# PHASE BT-D28 — Short Entry Offset 0.1% in Dynamic Setup Scanner

**Project:** `pools+ob+delta_bot`  
**Date:** 2026-10-25  
**Verdict:** **`ENTRY_OFFSET_SCANNER_PASS`**

No changes to BT-D26 selection/state machine, TP function, SL math, density SSOT, dashboard, or LMV2 Short V2.

---

## 1. Config

| Item | Value |
|------|--------|
| **New pilot YAML** | `configs/pilot_bt_d26_entry_offset_0p1_24h.yaml` |
| **Baseline (A)** | `configs/pilot_bt_d21_pool_first_24h.yaml` (`entry_price_mode: pool_lower_edge`, offset null) |
| **Only deltas** | `entry_price_mode: pool_lower_edge_offset`, `entry_offset_pct: 0.1` |
| **strategy_version** | `bt_d28_entry_offset_0p1_pilot_v1` (metadata) |

Normalized via `config_yaml_v1.load_yaml_and_validate` → `entry_price_reference: upper_pool_lower_edge`, `entry_offset_pct: 0.1`.

---

## 2. Entry / SL / TP (executed code)

| Leg | Implementation |
|-----|----------------|
| **Entry** | `entry_price_v1.compute_short_entry_level` → `pool_lo * (1 - 0.1/100)` |
| **SL** | `strategy_rules.short_sl` → `pool_upper * 1.002` (unchanged `risk.sl_above_upper_top_pct: 0.2`) |
| **TP** | `select_short_tp_depth2(planned_entry, …, min_distance_pct=0.5)` — unchanged function |

Structural eligibility (`_structural_eligible_upper`) uses the same `compute_short_entry_level`, so offset affects TP **gates** and **planned TP** where distance to lowers changes.

---

## 3. Runs (24h validated cache)

| Label | Run ID | Setups |
|-------|--------|--------|
| **A** — pool lower edge | `bt_d26_dynamic_referenz_sep11_24h_v1` | 102 |
| **B** — 0.1% offset | `bt_d26_entry_offset_0p1_sep11_24h_v1` | 102 |

CLI:

```bash
python3 tools/phase_bt_d26_dynamic_upper_pool_selector_v1/run_dynamic_selector.py \
  --config configs/pilot_bt_d26_entry_offset_0p1_24h.yaml \
  --run-id bt_d26_entry_offset_0p1_sep11_24h_v1
```

All setups: **`PLANNED`** only. Comparison summary: `results/setup_scanner_runs/bt_d26_entry_offset_0p1_sep11_24h_v1/comparison_vs_no_offset.json`.

---

## 4. Control comparison (A vs B)

| Metric | Result |
|--------|--------|
| Selected upper pools (same decision + origin + bounds) | **102/102 match** |
| `planned_sl` | **0 differences** |
| `planned_entry` | **102/102 differ** (offset applied) |
| `planned_tp` | **17 differences** (lower entry → different depth-2 lower top under 0.5% rule) |
| Candidate transitions / pool switches | **Same count** (288 transitions); no extra setups from offset alone |

**TP follow-on:** Not a new TP policy — same `select_short_tp_depth2`; planned entry is ~0.1% lower, so min-distance filtering and sort-by-highest-top can pick a **deeper** lower band in marginal cases.

Example (TP change): `2026-09-12T01:55:00Z` — entry `0.08453` → `0.08444547`, TP `0.0841` → `0.0836`.

---

## 5. Reference timestamps (2026-09-11)

### 12:00 UTC — pool 0.08365 / 0.08366

| | Entry | SL | TP |
|--|-------|-----|-----|
| A | 0.08365 | 0.08382732 | 0.08293 |
| B | **0.08356635** | 0.08382732 | 0.08293 |

`0.08365 × 0.999 = 0.08356635`

### 15:35 UTC — pool 0.08681 / 0.086838

| | Entry | SL | TP |
|--|-------|-----|-----|
| A | 0.08681 | 0.087011676 | 0.08586 |
| B | **0.08672319** | 0.087011676 | 0.08586 |

`0.086810 × 0.999 = 0.08672319` (user check)

### 17:00 UTC — pool 0.08593 / 0.085982

| | Entry | SL | TP |
|--|-------|-----|-----|
| A | 0.08593 | 0.086153964 | 0.08459 |
| B | **0.08584407** | 0.086153964 | 0.08459 |

---

## 6. Tests

```bash
python3 -m pytest tools/phase_bt_d26_dynamic_upper_pool_selector_v1/tests/test_dynamic_selector_v1.py -q
```

Added: `test_entry_offset_via_normalized_config` (pilot YAML → `0.08681 * 0.999`). **7 passed.**

---

## VERDICT

**`ENTRY_OFFSET_SCANNER_PASS`** — Offset wired through existing config + `compute_short_entry_level`; dynamic pool selection unchanged; SL unchanged; TP logic unchanged with **17** documented planned-TP deltas from entry reference shift.

**STOP** — no dashboard/density/LMV2 changes.
