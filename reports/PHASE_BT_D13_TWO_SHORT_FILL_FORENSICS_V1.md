# PHASE BT-D13 — Why Did the Two Short Signals Not Fill? V1

**Date:** 2026-10-25  
**Scope:** BT-D9 run `bt_d9_24h_run_304d6a244138_20261025T105617Z` / `latest_result.json` (`bt_d9_24h_import_v2_snapshots`)  
**Method:** Read-only ledger + SSOT 5m OHLC + validated density cache replay (no strategy changes, no hindsight).

---

## Executive summary

| Signal (decision close UTC) | Diagnosis | Primary reason |
|-----------------------------|-----------|----------------|
| **2026-09-11T15:35:00Z** | `ORDER_CANCELLED_BEFORE_TOUCH` | Pending became active at **15:40**; on that bar the **exact entry upper pool** `(0.08681 \| 0.086838)` was **no longer** in density `upper_bands` → `CANCELLED_POOL_INACTIVE`. Bar high **0.08671** never reached sell-limit **0.08681**. |
| **2026-09-11T17:00:00Z** | `NO_ENTRY_TOUCH_DURING_PENDING` | Pending from **17:05** until backtest end (**227** active 5m bars). Pool stayed active. **No** bar had `low ≤ 0.08593 ≤ high`; best high **0.08584** (**0.00009** below entry). No time-based expiry in config. |

**Not** caused by: max-one-position blocking (no open position), later-signal replacement (17:00 pending blocked new signals until test end), or post-hoc TP reachability.

**Artifacts:**  
`results/phase_bt_d13_two_short_fill_forensics_v1/pending_bars.csv`  
`results/phase_bt_d13_two_short_fill_forensics_v1/order_lifecycle.json`

---

## A. Original ledgers

**Sources (canonical BT-D9):**

| Artifact | Path |
|----------|------|
| Signals | `results/phase_bt_d6_config_experiments_v1/bt_d9_24h_run_…/signals.json` |
| Orders / events | `…/orders.json` |
| Trades | `…/trades.json` (empty) |
| Config | `…/config.immutable.json` |
| Published mirror | `results/backtester_runs/latest_result.json` |

**Order IDs:** BT-D9 `orders.json` has **no** separate `order_id` — only status events keyed by `signal_close`.

### Signal 1 — 2026-09-11T15:35:00Z

| Field | Value |
|-------|--------|
| **signal_id** | `304d6a244138:2026-09-11T15:35:00Z:0.086809999999999998\|0.086837999999999999` |
| **order_created** (signal accepted) | 2026-09-11T15:35:00Z (decision close) |
| **order_active_from** | 2026-09-11T15:40:00Z |
| **pending_expiry** | **null** (`pending_limit_expiry_bars`: null) |
| **entry_limit** | 0.086810 |
| **planned SL / TP** | 0.087012 / 0.085860 |
| **Status flow** | `PENDING_PLACED` → `CANCELLED_POOL_INACTIVE` @ bar **15:40:00Z** |
| **Cancel reason (engine)** | `pool_still_active()` false for signal pool edges |
| **Suppression** | None (no open position; pending cleared after cancel) |

### Signal 2 — 2026-09-11T17:00:00Z

| Field | Value |
|-------|--------|
| **signal_id** | `304d6a244138:2026-09-11T17:00:00Z:0.085930000000000006\|0.085982000000000003` |
| **order_active_from** | 2026-09-11T17:05:00Z |
| **pending_expiry** | **null** |
| **entry_limit** | 0.085930 |
| **planned SL / TP** | 0.086154 / 0.084590 |
| **Status flow** | `PENDING_PLACED` only (no cancel, no fill before `end_utc` **2026-09-12T12:00Z** exclusive) |
| **Suppression** | Engine does not emit further signals while this pending is open |

---

## B. Historical 5m bars (fill model)

**OHLC:** `price_feed_v1.load_prices_from_ssot_window` (same path as BT-D9).  
**Density:** `ValidatedDensityCacheSessionV1` on BT-D9 cache bundle.

**BT-D7 fill rule** (`short_research_engine_v1.py`):

1. On each 5m **close** `bar`, if pending and snapshot exists:
2. If **not** `pool_still_active(pool_lower, pool_upper, snapshot)` → **cancel** (no fill attempt on that bar).
3. Else if `bar >= order_active_from` and no open position: fill if **`low ≤ entry_level ≤ high`** at limit price.

**Short sell-limit:** Fill requires the bar range to **include** the limit (price trades up to 0.08681 / 0.08593). Touch is **not** “close below entry”.

Full per-bar log: **`pending_bars.csv`** (228 rows: 1 for 15:35 pending window, 227 for 17:00).

### Signal 15:35 — sole pending-active bar

| bar_close_utc | open | high | low | close | order_active | entry_limit | touched | pool_active | fill_decision |
|---------------|------|------|-----|-------|--------------|-------------|---------|-------------|---------------|
| 2026-09-11T15:40:00Z | 0.08668 | **0.08671** | 0.08639 | 0.08639 | yes | 0.08681 | **no** | **no** | POOL_INACTIVE_CANCEL |

At **15:35** close the pool **was** active; at **15:40** the exact band `0.08681|0.086838` is absent from `upper_bands` (nearby bands exist at 0.08682|0.086838 — identity is **exact** float match in BT-D7).

### Signal 17:00 — excerpt (first bars + best approach)

| bar_close_utc | high | low | pool_active | touched |
|---------------|------|-----|-------------|---------|
| 2026-09-11T17:05:00Z | 0.08584 | 0.08556 | yes | no |
| 2026-09-11T17:10:00Z | 0.08556 | 0.08533 | yes | no |
| … | … | … | yes | no |
| **Best high in window** | **0.08584** @ 17:05 | | yes | no |

Gap to entry: **0.00009** (0.10 bp on price). Entry was **not** reached during pending; later move toward TP is **out of scope** (no hindsight).

---

## C. Fill forensics (per signal)

### 15:35 UTC

| # | Answer |
|---|--------|
| 1. Pending order created? | **Yes** — `PENDING_PLACED` |
| 2. When active? | **2026-09-11T15:40:00Z** |
| 3. How long valid? | **One** 5m bar (cancel on same bar’s close processing) |
| 4. Entry touched in window? | **No** (high 0.08671 &lt; 0.08681) |
| 5. Touch but no fill? | N/A |
| 6. Miss distance | **0.00010** below entry on cancel bar |
| 7. Entry after expiry? | N/A — cancelled for **pool inactive**, not time expiry |
| 8. Replaced by later signal? | **No** — cancelled before 17:00 signal |
| 9. Defect vs correct? | **Correct pending behaviour** per BT-D7 + cancel-before-fill ordering |

**Diagnosis:** `ORDER_CANCELLED_BEFORE_TOUCH`

### 17:00 UTC

| # | Answer |
|---|--------|
| 1. Pending created? | **Yes** |
| 2. Active from | **2026-09-11T17:05:00Z** |
| 3. Valid until | Backtest end **2026-09-12T12:00Z** (exclusive) — **227** bars |
| 4. Entry touched? | **No** on any active bar |
| 5–7. | Price remained **below** sell-limit; no post-expiry entry (no TTL) |
| 8. Later signal? | **No** third signal after 17:00 in BT-D9 window |
| 9. | **Correct** — limit never traded through |

**Diagnosis:** `NO_ENTRY_TOUCH_DURING_PENDING`

---

## D. Strategy contract comparison

| Document | Relevant rule |
|----------|----------------|
| `strategy_freeze/.../STRATEGY_SPEC_CANDIDATE_4E.md` | Sell-limit at upper pool lower edge; **no fixed TTL** while zone valid; cancel if entry pool loses **depth-2 ACTIVE** zone |
| `USER_CONFIRMED_RULES.md` | Same pending-limit semantics |
| **BT-D7 implementation** | `pool_still_active`: **exact** `(pool_lower, pool_upper)` must appear in snapshot `upper_bands`; cancel **before** intrabar fill check on same bar |
| **Phase 4D reference** | `pending_limit_simulator.py` uses lifecycle CSV + zone tracker (richer than BT-D7 exact-band check) |

**Parity note (not a fill bug for these two cases):** BT-D7 is a **simplified** deactivation test vs Phase 4E lifecycle. For **15:35**, deactivation on **15:40** is consistent with “pool at signal no longer listed at exact edges.” For **17:00**, pool **remained** listed — behaviour matches “limit not touched.”

**No silent rule changes** were made in this phase.

---

## E. Defect assessment & minimal fix (not implemented)

| Verdict | Finding |
|---------|---------|
| **15:35** | Not `FILL_LOGIC_DEFECT`. Engine cancelled on pool deactivation **before** price could reach limit on the only active bar. |
| **17:00** | Not `FILL_LOGIC_DEFECT`. Sell-limit never in range for 227 bars. |

**Optional future audit (if product wants 4E parity):** Compare BT-D7 `pool_still_active` exact-band match vs `pending_limit_simulator` / `EXEC-POOL-ORIGIN-01` band continuity on runs where nearby bands persist after origin shift. **Not required** to explain these two non-fills.

**If** stakeholders required fills when price **later** hits TP: that would contradict pending-limit + no-hindsight rules — **not** recommended.

---

## F. No hindsight

No changes to entry, SL, TP, pending duration, or backtest results. TP paths after pending ended were **not** used as fill explanations.

---

## STOP

Diagnosis complete. No code or config changes applied.
