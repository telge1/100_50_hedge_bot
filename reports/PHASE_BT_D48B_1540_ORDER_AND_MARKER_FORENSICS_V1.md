# PHASE BT-D48B — MISSING TRADE MARKERS AND 15:40 ORDER FORENSICS

**Date:** 2026-10-26  
**Verdict:** `TRADE_MARKER_AND_ORDER_TRACE_PARTIAL` (server forensics + renderer fix verified; browser visibility not re-checked in this session)

## Executive summary

1. **“10 drawn” at GO TO 17:30** is correct server-side but **misleading**: **9** event markers + **1** entry→exit **link** (not 10 trades). Causal as-of 17:30: **13** events total.
2. **Markers were effectively invisible** — BT-D48 used **60s gray arrows** (~**0.2%** of a ±4h viewport width). Drawings existed in TRP/`/api/research/overlays` but were sub-pixel slivers.
3. **15:35 pending** (`80a38dea…`) was **replaced at 15:40** with **no touch** — **no simulated trade** in BT-D46/D47. Child pending `a039133d…` continued until **16:35 CANCEL** — still **no simulated fill**.

Artifacts: `results/phase_bt_d48b_marker_forensics_v1/`

## A. 15:35 → 15:40 → later (read-only SSOT)

| Time (5m close UTC) | Event | Order / pool | Entry / SL / TP | Simulated trade? |
|---------------------|-------|--------------|-------------------|------------------|
| **15:35** | CREATE pending | `80a38dea…` · 0.08681–**0.086838** | 0.08681 / 0.087011676 / 0.08586 | No — activation **15:40** |
| **15:40** | REPLACE (bound drift) | Supersedes `80a38dea…` | Old band → new scanner band 0.08681–**0.08682** | No — D42: `CANCELLED_BEFORE_TOUCH`, high 0.08671 < entry |
| **15:40** | CREATE pending | `a039133d…` (parent `80a38dea…`) | 0.08681 / **0.08699364** / 0.08586 | No — activation **15:45** |
| 15:45–16:30 | KEEP (pending) | Same child order | TP refreshed in-place on some bars | No |
| **16:35** | CANCEL | `a039133d…` | `NOT_NEXT_ELIGIBLE_UPPER` (closer pool 0.08564…) | No |

**Origins after 15:40:**  
`lldv2:DOGEUSDT:5m:upper:1789137900|lldv2:DOGEUSDT:5m:upper:1789140600|lldv2:DOGEUSDT:5m:upper:1789140900`

**Activation contract (unchanged):** BT-D41 `earliest_eligible_execution_utc` = first 5m close key T+5m; fill evaluation on bars with `bar_utc >= order_active_from_utc` (D42 reference).

## B. Chart marker forensics @ 17:30 UTC

| Metric | Value |
|--------|-------|
| Viewport | ±4h around 17:30 (GO TO pad) |
| Causal events ≤ 17:30 | 13 |
| In viewport | 9 markers + 1 link = **10 drawing IDs** |
| BT-D47 overlay @ 17:30 | No entry at 0.08681 / 15:35; only resolved **simulated** positions |

**Pre-fix geometry:** 60s arrow, color token `draw` (#8b9bb4), width 1 — **not** outside price range; **not** hidden by replay; **too small** to see.

**Post-fix (minimal UI):**
- Horizontal **trend tick** at **exact** `time_utc` + `price` anchor, 900s visual span (display only).
- Colors: ENTRY orange, SL red, TP green, OPEN blue; width ~2.5.
- **OverlayLabel** (`ENTRY` / `SL` / `TP` / `OPEN`) at anchor, z_order 64.
- Links: dashed gray entry→exit when bars differ.

## C. Source vs chart parity

`source_to_chart_parity.json`: **all_match: true** for 17:30 viewport markers (anchor time/price equals BT-D47 records).

## D. Tests

- `dashboard/test_bt_d48b_marker_visibility_v1.py` — trend span, labels in composed overlays  
- Updated D48 chart tests — **9 passed**  
- Forensics runner: `tools/phase_bt_d48b_marker_forensics_v1/run_forensics.py`

## E. Browser checklist

Hard-refresh (`research_charts.js?v=phase6xt-bt-d48b-marker-forensics-v1`), BT-D46, GO TO **2026-09-11 17:30 UTC**, confirm colored ticks + ENTRY/SL/TP labels on simulated events only; B density unchanged.

## Prohibited items

No strategy, Entry/SL/TP, pool-selection, PnL, density SSOT, or new backtest runs were changed.
