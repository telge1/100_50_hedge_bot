# PHASE BT-D43 — Single Active Short Order Causal Replay V1

**Project:** `pools+ob+delta_bot`  
**Baseline:** BT-D39 … BT-D42 **PASS**  
**Pilot:** DOGEUSDT Sep-11–12 2026 (24h)  
**Date:** 2026-10-26  

---

## VERDICT

**`SINGLE_ORDER_REPLAY_PASS`**

Causal, event-driven replay with **at most one** pending short, unchanged nearest-eligible upper selection, BT-D41/BT-D42 activation and touch semantics, explicit dual branches where fills are not proven. No trading, PnL, SL/TP exits, or production changes.

---

## 1. Binding strategy principle (preserved)

- From current price **upward**, only the **lowest** structurally eligible upper pool (`pool_lower` minimum).
- Higher eligible uppers remain in the landscape but **do not** spawn parallel orders.
- **One** pending short at any instant (`max_simultaneous_pending = 1` in replay).

Selection and gates are those already used in BT-D26/D41 (`detect_short_signal_pool_first`, `_structural_eligible_upper`).

---

## 2. State machine (B)

| State | Meaning |
|-------|---------|
| `FLAT` | No pending; only state that starts a **new** independent pending |
| `PENDING_SHORT` | Active resting limit (research) |
| `EXECUTION_POSSIBLE_UNRESOLVED` | Touch / v2 evidence without confirmed fill |
| `POSITION_OPEN_HYPOTHETICAL` | Reserved branch label in outcomes (not auto-entered in primary replay) |
| `CANCELLED` / `REPLACED` | Terminal for current order id → return to `FLAT` before next `CREATE` |

Primary replay timeline follows **Branch 1 (unfilled)** for cancel/replace — aligned with BT-D41. **Branch 2** (hypothetical fill) is recorded in `branch_outcomes.csv` and blocks counting new independents as **NOT_DETERMINABLE** without exit modeling.

---

## 3. Activation contract (C — unchanged)

Documented in BT-D41/BT-D42 and embedded on each `CREATE_PENDING`:

- Order at decision close **T** → `pending_active_from_utc` = **T+5m** bar key.
- SSOT labels the **5m candle evaluated at that close** (period **ending** at T+5m).
- `evaluate_short_sell_limit_fill` uses `bar_utc >= order_active_from_utc` — **no** fill in the originating decision candle.
- No retroactive fills or cancels.

---

## 4. Invalidation (D)

Replay consumes BT-D41 review events: inactive pool, Depth2/TP loss, structural ineligibility, **no longer nearest** eligible upper, scanner-driven replace on bound drift. **No** cancel solely for 2–5% price drop.

---

## 5. Touch vs cancel (E)

BT-D42 evidence is attached per order in `branch_outcomes.csv` and `touch_by_order.csv`:

| BT-D42 segment status | Count |
|------------------------|------:|
| `EXECUTION_POSSIBLE` | 19 |
| `EXECUTION_UNRESOLVED` | 47 |
| `CANCELLED_BEFORE_TOUCH` | 35 |
| `STILL_PENDING_NO_TOUCH` | 1 |

Touch ≠ fill. Where touch may precede cancel, `bt41_cancel_conditional_on_unfilled = false` (66 segments) — cancel/replace must not be read as “definitely unfilled.”

---

## 6. Deduplication (F)

| Concept | Pilot count | Semantics |
|---------|--------------:|-----------|
| BT-D26 scanner **candidate episodes** | 102 | Emit events (PLANNED) |
| BT-D41 **CREATE** | 102 | New pending order ids |
| BT-D41 **CANCEL** / **REPLACE** | 89 / 12 | Lifecycle events, not extra independents |
| BT-D42 **touch evidence rows** | 82 | 5m bar × order touches |
| **Distinct order IDs** | 102 | `order_timelines.csv` |
| **Replace lineage links** | 12 | `order_lineage.csv` (bound drift amendments) |
| **Independent trade count** | **NOT_DETERMINABLE** | No position-exit simulation |

**Do not** add 102 + 89 + 82 as one trade total.

---

## 7. Reference 2026-09-11 15:35 UTC (H)

| Step | Event |
|------|--------|
| 15:35 close 0.08668 | Scanner selects 0.08681–0.086838; `CREATE_PENDING` |
| Active from | **15:40** |
| 15:40 | Bound-drift **REPLACE** (same lower, upper 0.086838 → 0.08682) |
| Touch before replace? | **No** — 5m high **0.08671 < entry 0.08681** (BT-D42) |
| Chronology | `CANCELLED_BEFORE_TOUCH` on unfilled branch |

Detail: `results/phase_bt_d43_single_order_replay_v1/reference_1535.json`.

---

## 8. Tests (I)

```bash
python3 -m pytest tools/phase_bt_d43_single_order_replay_v1/tests/test_single_order_replay_v1.py
```

- One pending at a time (`max_simultaneous_pending ≤ 1`)
- 12 replace lineage rows = BT-D41 REPLACE count
- Deterministic replay from D41/D42 artifacts

---

## 9. Artifacts

| Path | Role |
|------|------|
| `tools/phase_bt_d43_single_order_replay_v1/single_order_replay_v1.py` | State machine |
| `tools/phase_bt_d43_single_order_replay_v1/run_replay.py` | Driver |
| `results/phase_bt_d43_single_order_replay_v1/state_transitions.csv` | 585 replay steps |
| `results/phase_bt_d43_single_order_replay_v1/order_lineage.csv` | Parent→child on REPLACE |
| `results/phase_bt_d43_single_order_replay_v1/touch_by_order.csv` | Touch intervals per order |
| `results/phase_bt_d43_single_order_replay_v1/branch_outcomes.csv` | Dual-branch semantics |
| `results/phase_bt_d43_single_order_replay_v1/reference_1535.json` | Reference case |
| `results/phase_bt_d43_single_order_replay_v1/summary.md` | Machine summary |

**Re-run:** `python3 tools/phase_bt_d43_single_order_replay_v1/run_replay.py`  
(Requires existing `phase_bt_d41_*` and `phase_bt_d42_*` results.)

---

## 10. Scope limits

No strategy/ranking/threshold changes, no PnL, no dashboard/density/LMV2 edits, no fabricated fills or SL/TP outcomes.

---

**End of BT-D43.**
