# LONG V3 Trend Continuation — Entry Engine V1 (Phase 2A)

Isolated package: `tools/long_v3_trend_continuation_v1/`

**Status:** Research entry only — not live, no CH writes, no full-month backtest.

## Causal touch / publish semantics

- SSOT snapshot at decision close **t** reflects ACTIVE bands known at end of bar **t** (frozen cache / published reader parity).
- **Touch** on bar **t** is evaluated with support **P** from snapshot **t−1** and reference price `close[t−1]`. Intrabar `low[t]` must satisfy `low[t] ≤ P.upper`. If **P** is not present in snapshot **t−1**, the wick on **t** does **not** count (fail closed — no retroactive pool activation).
- **Recovery** at close **t** uses snapshot **t** and `close[t] > P.upper` for the frozen **P** from the touch episode.
- **PLACE** earliest at open of bar after recovery close (`next_5m_open_after_close`).

## Bar order (Short parity)

1. Buy-limit fill attempt  
2. EMA + EMA200−6 cancel  
3. Structural / deactivation / gap cancel  
4. Replace (strictly higher support)  
5. Touch / recovery → new pending  

Fill on bar **t** is never undone by cancel at close **t**.

## Modules

| Module | Role |
|--------|------|
| `long_ema_gate_v1.py` | 9/20/59 > 200 + EMA200[t] > EMA200[t−6] |
| `long_support_selection_v1.py` | Union lowers, 0.6% gap, tie-break |
| `long_touch_recovery_v1.py` | Causal touch/recovery FSM |
| `long_entry_chronology_v1.py` | Single-bar orchestration |
| `buy_limit_fill_v1.py` | `causal_buy_limit_v2` |
| `long_risk_audit_v1.py` | risk_pct audit, SL overlap |
| `pool_history_gate_v1.py` | 168h bar index gate |
| `weak_pool_audit_v1.py` | Variant B tags (disabled) |

## Phase 2B

- `research_runner_v1.py` — chronological entry + LONG A/B exits
- `long_exit_lifecycle_v1.py`, `long_atomic_tp_v1.py`, `upper_pool_gap_tp_v1.py`
- `coverage_preflight_v1.py`, `density_session_adapter_v1.py`
- Tie-break: same `upper_edge` → **smaller** `lower_edge` (wider region)


```bash
python3 -m pytest tools/long_v3_trend_continuation_v1/tests/ -q
```

Short regression (unchanged):

```bash
python3 -m pytest tools/phase_short_main_v2_combined_exit_v1/tests/ -q
```
