# PHASE BT-D53 — TOUCH-CONNECTED PRICE LEVEL UPPER EDGE FOR SHORT SL

**Date:** 2026-10-26  
**Verdict:** `TOUCH_CONNECTED_LEVEL_PARTIAL`

**Scope:** Read-only connectivity forensics. **No** BT-D46, dashboard, Entry/TP, pool selection, density SSOT, or PnL changes.

**Tooling:** `tools/phase_bt_d53_touch_connected_sl_v1/` (`connectivity_component_v1.py`, `run_audit.py`)

---

## Executive answer

**Edge-touch connectivity (`lo1 <= hi2 && lo2 <= hi1`)** from the **12:30** selected band **0.08408–0.08409** reaches a canonical component top of **`0.084378000000000009`** (JSON serializes as `0.08437800000000001`), **≈2×10⁻⁶ below** the user’s approximate **0.08438**.

Hypothetical touch SL: **`0.084378 × 1.002 = 0.084546756`** vs user reference **`0.08454876`** (Δ ≈ **2×10⁻⁶** on SL).

That envelope is **not** accepted as a confirmed SL edge: the touch component has **30** members, **29 edge-touch-only** adjacencies, **0 genuine strict overlaps**, and **28 articulation bridge** bands → **`UNRESOLVED_PRICE_LEVEL_EDGE` / `WEAK_ARTICULATION_BRIDGE_CHAIN`**.

**12:05** entry **0.08384–0.083844** stays **isolated** under both strict and touch rules (no neighbor at shared endpoint). User shelf SL is **not** applied there.

---

## A. Connectivity audit (canonical exact floats)

| Rule | Predicate |
|------|-----------|
| **Strict overlap** (BT-D52) | `lo1 < hi2 and lo2 < hi1` |
| **Touch or overlap** (BT-D53) | `lo1 <= hi2 and lo2 <= hi1` |

**12:30 entry → next band:** `0.08409–0.08411` is **`edge_touch_only`** (shared endpoint **0.08409**; strict overlap **false**).

No proximity ε; gaps (e.g. `0.11` vs `0.12`) never connect.

---

## B. Connected components (from selected atomic band)

### 2026-09-11T12:05:00Z (12:25 fill)

| Mode | Lower | Upper | Members | Touch edges | Overlap edges | Status |
|------|-------|-------|---------|-------------|---------------|--------|
| Strict | 0.08384 | 0.083844 | 1 | 0 | 0 | CONFIRMED |
| Touch | 0.08384 | 0.083844 | 1 | 0 | 0 | CONFIRMED |

Disconnected from **0.08410+** shelf.

### 2026-09-11T12:30:00Z (12:35 fill)

| Mode | Lower | Upper | Members | Touch edges | Overlap edges | Status |
|------|-------|-------|---------|-------------|---------------|--------|
| Strict | 0.08408 | 0.08409 | 1 | 0 | 0 | CONFIRMED |
| Touch | 0.08408 | **0.084378000000000009** | **30** | **29** | **0** | **UNRESOLVED** |

**Path (touch):** 30-band chain from entry through `0.084089…|0.084110…` … to uppermost  
`0.084362000000000006|0.084378000000000009` (full list in `reference_1205_1230.json`).

**Extension above user ~0.08438:** Component **stops** at **0.084378**; the next higher canonical bands are **not** touch-connected at this as-of (no silent merge into higher pools).

**Below user lower 0.08410:** Component **includes** entry **0.08408–0.08409** (selected pool); shelf span is **not** trimmed to manual bounds.

---

## C. SL comparison (12:30 reference)

| Method | Upper edge used | SL (= upper × 1.002) |
|--------|-----------------|----------------------|
| **Atomic (legacy)** | 0.08409 | **0.08425818** |
| **Strict overlap component** | 0.08409 | **0.08425818** |
| **Touch component** (computed) | **0.084378000000000009** | **0.084546756** |
| **User visual reference** | ~0.08438 | **0.08454876** |

Touch SL is reported as **`computed_but_UNRESOLVED`** — not a production replacement under BT-D53 safety rules.

---

## D. Weak-chain safety (12:30)

- **Articulation bands:** 28 of 30 members (removing one can split the touch graph).
- **Overlap support:** none between members (touch-only spine).
- **Unresolved reason:** `WEAK_ARTICULATION_BRIDGE_CHAIN` (also qualifies as touch-only multi-band chain).
- **Origins:** union of **many** ACTIVE upper origins across the 30 bands (see JSON); envelope is origin-dense but **graph-ambiguous** for SL.

---

## E. All 19 D47 positions (decision-close as-of)

From `sl_comparison_19.csv`:

| Category | Count | Meaning |
|----------|-------|---------|
| **unchanged** | **8** | Touch component **CONFIRMED** and SL = atomic |
| **unresolved** | **11** | Touch envelope **UNRESOLVED**; SL computed but **not** adopted |
| **confirmed differs** | **0** | — |

Among the **11 unresolved**, **all 11** have `touch_connected_sl ≠ atomic_sl` (hypothetical change if forced).

Examples:

- **12:30** (`aa1a8a9c…`): atomic **0.08425818** → touch **0.084546756** (UNRESOLVED).
- **12:05** (`78cd59a5…`): unchanged (single band).

No fill/exit recalculation; D47 artifacts untouched.

---

## F. Tests

`tools/phase_bt_d53_touch_connected_sl_v1/tests/test_connectivity_component_v1.py` — **4 passed**

- Edge-touch merges in touch mode, not strict  
- Real gap does not connect  
- `short_sl(0.08438, 0.2) = 0.08454876`  
- Multi-band touch-only chain → UNRESOLVED  

---

## G. Deliverables

| Path | Status |
|------|--------|
| `reports/PHASE_BT_D53_TOUCH_CONNECTED_PRICE_LEVEL_SL_V1.md` | This file |
| `results/phase_bt_d53_touch_connected_sl_v1/reference_1205_1230.json` | ✓ |
| `results/phase_bt_d53_touch_connected_sl_v1/connectivity_paths.csv` | ✓ |
| `results/phase_bt_d53_touch_connected_sl_v1/component_edge_comparison.csv` | ✓ |
| `results/phase_bt_d53_touch_connected_sl_v1/weak_chain_risks.md` | ✓ |
| `results/phase_bt_d53_touch_connected_sl_v1/sl_comparison_19.csv` | ✓ |
| `results/phase_bt_d53_touch_connected_sl_v1/summary.md` | ✓ |

**Re-run:**

```bash
python3 tools/phase_bt_d53_touch_connected_sl_v1/run_audit.py
python3 -m pytest tools/phase_bt_d53_touch_connected_sl_v1/tests/test_connectivity_component_v1.py -q
```

---

## Verdict rationale

**`TOUCH_CONNECTED_LEVEL_PARTIAL`:** Touch connectivity **does** reconstruct a canonical top **aligned with** the user’s **~0.08438** shelf at **12:30**, with matching SL to **~0.002%** above that top. The same rule exposes a **long touch-only articulation chain** that fails structural SL confirmation and affects **11/19** pilot decisions. Production remains on **atomic SL** until a follow-on phase defines an adopted policy for unresolved touch envelopes.
