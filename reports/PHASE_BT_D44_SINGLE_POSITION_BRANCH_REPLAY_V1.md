# PHASE BT-D44 — Causal Single-Position Branch Replay V1

**Project:** `pools+ob+delta_bot`  
**Baselines:** BT-D39 … BT-D43 **PASS**  
**Pilot:** DOGEUSDT Sep-11–12 2026 (24h)  
**Date:** 2026-10-26  

---

## VERDICT

**`POSITION_BRANCH_REPLAY_PASS`**

Isolated dual-path replay (unfilled vs single-slot hypothetical position). No real execution, no PnL, no strategy/density/dashboard changes. Prior D41/D42/D43 artifacts **unchanged**.

---

## 1. Strategy preserved

Nearest eligible upper pool, Depth2, 168h context, Gap/Entry/SL/TP, T+5m close-key activation (documented with **5m period boundaries** — close-key labels the bucket **ending** at that timestamp, not the period start).

---

## 2. Replay paths

| Path | Role |
|------|------|
| **A_UNFILLED** | Pending lifecycles only; **no** position block → 102 scanner opportunities |
| **B_HYPOTHETICAL** | At most **one** `HYPOTHETICAL_POSITION_OPEN`; blocks new pending until exit or pilot end |

States used: `FLAT`, `PENDING_SHORT`, `UNFILLED_CANCELLED`, `UNFILLED_REPLACED`, `HYPOTHETICAL_POSITION_OPEN`, `HYPOTHETICAL_POSITION_CLOSED`, `OUTCOME_UNRESOLVED`.

Path B entries only when BT-D42 status is **`EXECUTION_POSSIBLE`** and `causal_sell_limit_v2` fills **before** cancel effective close. **`EXECUTION_UNRESOLVED`** does **not** assume fill on B.

---

## 3. Why 102 events ≠ 102 trades

| Metric | Count |
|--------|------:|
| Scanner candidate events | 102 |
| Distinct pending order IDs | 102 |
| Cancelled / replaced pendings | 89 / 12 |
| Path B hypothetical entries (defensible) | **19** |
| Path B scanner emits **blocked** (position open) | **4** |
| Path B hypothetical positions **closed** (SL/TP sim) | **17** |
| Still open at pilot end | **1** |
| Exit outcome unresolved (intrabar) | **1** |
| Orders with **no** defensible B entry | **83** |

**Exact independent trade count:** `null` — not claimed.

**Defensible closed hypotheticals on path B:** **17** (not PnL; exit labels only).

**Range if all `EXECUTION_POSSIBLE` entries counted:** min closed **17**, max entry events **19** — not a promise of live trade count.

---

## 4. Hypothetical exits (5)

Uses existing `planned_sl` / `planned_tp` and `simulate_short_exit` with `no_trade_ambiguous` — **no** fees, slippage, or PnL. Ambiguous same-bar SL+TP → `OUTCOME_UNRESOLVED` on that branch.

---

## 5. Branch management (6)

Single global path B tracker (deterministic merge — no exponential fork). Ambiguous fills stay on path A unfilled; path B does not invent fills for 47 `EXECUTION_UNRESOLVED` segments.

---

## 6. Reference 15:35 UTC (8)

| Item | Value |
|------|--------|
| Create | 15:35 UTC |
| Active from | **15:40** close-key |
| Replace | **15:40** (bound drift) |
| Chronology | `CANCELLED_BEFORE_TOUCH` |
| Path A | `UNFILLED_REPLACED` — no touch before replace |
| Path B | **No** hypothetical entry |

Detail: `results/phase_bt_d44_single_position_branch_v1/reference_1535.json`.

---

## 7. Tests

```bash
python3 tools/phase_bt_d44_single_position_branch_v1/run_branch_replay.py
python3 -m pytest tools/phase_bt_d44_single_position_branch_v1/tests/test_branch_replay_v1.py
```

**5 passed** — outputs, blocked emits, reference 1535, one exit row per B entry, deterministic counts.

---

## 8. Artifacts

| Path | Role |
|------|------|
| `tools/phase_bt_d44_single_position_branch_v1/branch_replay_v1.py` | Core replay |
| `tools/phase_bt_d44_single_position_branch_v1/run_branch_replay.py` | Driver |
| `results/phase_bt_d44_single_position_branch_v1/event_timeline.csv` | Path A+B events |
| `results/phase_bt_d44_single_position_branch_v1/branch_states.csv` | Final path states |
| `results/phase_bt_d44_single_position_branch_v1/potential_position_exits.csv` | Hypothetical SL/TP outcomes |
| `results/phase_bt_d44_single_position_branch_v1/independent_trade_counts.json` | Count semantics |
| `results/phase_bt_d44_single_position_branch_v1/reference_1535.json` | Reference case |
| `results/phase_bt_d44_single_position_branch_v1/summary.md` | Machine summary |

---

**End of BT-D44.**
