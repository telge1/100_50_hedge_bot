# PHASE BT-D52 — TRUE UPPER PRICE-LEVEL EDGE FOR SHORT STOP LOSS

**Date:** 2026-10-26  
**Verdict:** `TRUE_PRICE_LEVEL_SL_PARTIAL`

**Scope:** Isolated research + `price_level_component_v1` prototype. **No** changes to BT-D46, D47 artifacts, dashboard, density SSOT, Entry/TP, pool selection, or PnL.

---

## Summary

Legacy Short SL uses the **selected atomic** upper-pool top: `short_sl(entry_pool_upper, sl_above_upper_top_pct)` → multiply by **1.002** when `sl_above_upper_top_pct = 0.2`.

BT-D52 reconstructs the **strict price-overlap component** containing the selected entry band at **decision-close as-of** (BFS on `lo1 < hi2 && lo2 < hi1`, no gap bridging, no future data).

For the binding **12:25 / 12:35 fill** cases (pools chosen at **12:05** and **12:30**):

| Fill UTC | Decision | Atomic pool | Overlap component | Component upper | Legacy SL | Candidate composite SL |
|----------|----------|-------------|-------------------|-----------------|-----------|-------------------------|
| 12:25 | 12:05 | 0.08384–0.083844 | **1 band** (same) | 0.083844 | 0.084011688 | **0.084011688** (unchanged) |
| 12:35 | 12:30 | 0.08408–0.08409 | **1 band** (same) | 0.08409 | 0.08425818 | **0.08425818** (unchanged) |

User visual reference **0.08438** → SL **0.08454876** is **not** confirmed from the entry pool’s overlap component:

- **12:05** entry is **below** the 0.08410–0.08438 shelf (unrelated level; correctly **not** merged).
- **12:30** entry band **does not strictly overlap** the next band up (**0.08409–0.08411** touches at 0.08409 but fails strict `<` overlap), so the component does **not** expand to the multi-band shelf BT-D50 described.

Across **all 19** D47 positions on this pilot: **19/19** `CONFIRMED_PRICE_LEVEL_EDGE`, **0** unresolved; **candidate composite SL equals legacy** (single-member components only in this run).

---

## A. Strategy preservation

Unchanged by design: atomic entry pool, entry price, TP rule, scanner eligibility, pending lifecycle, fills, density SSOT, warmup.

---

## B. Price-level reconstruction

**Module:** `tools/phase_bt_d52_true_price_level_sl_v1/price_level_component_v1.py`

**Algorithm:**

1. Locate entry `pool_key` in causal `upper_bands` snapshot.
2. BFS-add bands with **strict interval overlap** only.
3. Envelope: min(lower), max(upper) of members; union of Origin IDs.
4. Diagnostics: overlap-depth histogram, internal coverage gaps, articulation bridges.

**Unresolved (fallback to legacy in experimental compare):**

| Reason | Meaning |
|--------|---------|
| `ENTRY_BAND_NOT_IN_SNAPSHOT` | Missing band at as-of |
| `INTERNAL_COVERAGE_GAP_IN_ENVELOPE` | Hole inside envelope |
| `WEAK_ARTICULATION_BRIDGE_CHAIN` | ≥3 members with articulation cut-vertex |
| `ENVELOPE_WIDTH_DISPROPORTIONATE_TO_ENTRY_BAND` | Width > 100× entry band width |

---

## C. Reference audit (12:25 / 12:35)

See `results/phase_bt_d52_true_price_level_sl_v1/reference_1225_1235.json`.

- **12:25** = fill/TP of **12:05** pending; origins `1789120800|1789128000`; **not** user 0.08410–0.08438 level.
- **12:35** = fill/SL of **12:30** pending; four origins on atomic band; neighbor shelf **not** in same overlap component.

---

## D. Stop loss calculation (prototype only)

When `CONFIRMED_PRICE_LEVEL_EDGE`:

`candidate_SL = short_sl(component_upper, 0.2) = component_upper × 1.002`

When unresolved: `sl_mode = legacy_atomic_fallback`, no silent override.

**Not wired** into `setup_scanner_v1`, integrated backtester, or dashboard.

---

## E. Isolated comparison outputs

| Artifact | Purpose |
|----------|---------|
| `sl_comparison.csv` | `legacy_atomic_sl` vs `candidate_composite_sl` per position |
| `price_level_components.csv` | Component geometry + origins per decision |
| `unresolved_components.csv` | Empty for Sep-11 pilot (all confirmed) |
| `summary.md` | Short narrative |

BT-D46/D47 paths untouched; new results under `results/phase_bt_d52_true_price_level_sl_v1/`.

---

## F. Safety / causal tests

`tools/phase_bt_d52_true_price_level_sl_v1/tests/test_price_level_component_v1.py`:

- Entry contained in component
- Strict overlap does not merge edge-touching neighbors (0.08408 / 0.08409 case)
- Overlap merges two genuinely overlapping bands; SL uses component top × 1.002
- No gap bridging between disconnected bands

Audit script recomputes legacy SL and verifies parity with frozen D47 SL (0 issues).

---

## G. Implications for user reference SL 0.08454876

Canonical confirmation of **~0.08438** as the SL anchor for the **12:30** entry would require either:

1. A **different connectivity rule** (e.g. closed-interval touch — explicitly **not** used here), or  
2. Selecting a **different entry pool** inside the shelf (selection unchanged per BT-D52 scope).

Under **current** SSOT overlap semantics, composite price-level SL **matches legacy** for both reference trades.

---

## Verdict rationale

**`TRUE_PRICE_LEVEL_SL_PARTIAL`:** Prototype and audit **PASS** causally and deterministically; user visual **0.08438** SL is **not** reproduced from the **already-selected** entry pools without changing overlap or selection rules. Production remains on legacy atomic SL until a follow-on phase explicitly adopts a connectivity policy and activates it in the backtester.

**Re-run:**

```bash
python3 tools/phase_bt_d52_true_price_level_sl_v1/run_audit.py
python3 -m pytest tools/phase_bt_d52_true_price_level_sl_v1/tests/test_price_level_component_v1.py -q
```
