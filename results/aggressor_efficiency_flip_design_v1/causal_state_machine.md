# Causal state machine — AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1

**Contract version:** `aef_causal_contract/v1`  
**Scope:** Design only. LONG described; SHORT is the exact mirror (Buy↔Sell, up↔down, high↔low).

## Clock and inputs

| Input | Rule |
|---|---|
| Trades | `public_trades_canonical`, event order = `(trade_ts ASC, trade_id ASC)` |
| Price | Public-trade bucket OHLC (see feature_window_contract.md) |
| OI | `open_interest_5s` closed buckets only; label layer |
| Candles 1m | optional structure/acceptance confirm; `is_closed=1` only |
| OB1s / pools | optional flags; never required to emit a trade-only episode |

A **1s bucket** `s` is closed at `s+1s`. Features for bucket `s` may be used only when `as_of >= s+1s`.

## States

```text
NEUTRAL
  → AGGRESSOR_BURST
  → IMPACT_COMPRESSION
  → COUNTER_SIDE_WATCH
  → EFFICIENCY_FLIP
  → STRUCTURE_CONFIRM_PENDING
  → ACCEPTANCE_PENDING
  → CANDIDATE_CONFIRMED
  ↘ INVALIDATED | TIMEOUT (from most non-terminal states)
```

### NEUTRAL

- **Enter:** default / after cooldown.
- **Data:** rolling past-only notional baselines.
- **Exit:** dominant-side burst gate fires → `AGGRESSOR_BURST`.

### AGGRESSOR_BURST (LONG = Sell-dominant)

- **Enter:** closed flow window `[t0,t1)` with Sell notional score high, dominance share high, min notional gate, min liquidity gate (symbol-local, past-only).
- **Assigned ts:** `burst_decision_ts = t1` (flow window closed).
- **Invalidation:** opposite-side dominance immediately overwhelms; data gap.
- **Timeout:** if impact window cannot close within `T_impact_max`.

### IMPACT_COMPRESSION

- **Enter:** after burst, **impact window** `(t1, t2]` closed and compression gates hold (low adverse directional impact vs high aggressor notional → low efficiency / high compression score).
- **Assigned ts:** `compression_decision_ts = t2`.
- **Aggressor VWAP:** computed on flow `[t0,t1)` only (Sell trades for LONG).
- **Invalidation:** large adverse continuation beyond compression extreme; technical gap.
- **Timeout:** `T_watch_max` while waiting for counter side → `TIMEOUT`.

### COUNTER_SIDE_WATCH

- **Enter:** compression confirmed; waiting for opposite aggressor burst.
- **Constraint:** price must not make a new relevant adverse extreme beyond compression invalidation level (LONG: no new micro-low beyond sell-burst low by margin).
- **Exit:** counter-side burst detected → measure its impact → `EFFICIENCY_FLIP` or fail.

### EFFICIENCY_FLIP

- **Enter:** counter-side flow `[u0,u1)` + impact `(u1,u2]` closed; flip efficiency score clears two-stage gates vs compression episode; `u0 >= compression_decision_ts`.
- **Assigned ts:** `flip_decision_ts = u2`.
- **Delay:** `flip_delay_seconds = u0 - compression_decision_ts` (must lie in `[D_min, D_max]`).
- **Not yet an entry.**

### STRUCTURE_CONFIRM_PENDING

- **Enter:** flip confirmed; waiting for break of local micro-high / range edge defined **causally** from prices ≤ `flip_decision_ts` (level frozen at flip decision; break detected later).
- **Break ts:** first closed bucket that trades/closes beyond level by tick/bps gate.
- **Invalidation:** failed break / full reclaim against level before acceptance.

### ACCEPTANCE_PENDING

- **Enter:** structure break observed.
- **Acceptance (V1 proposal):** ≥ `N` closed 1s (or one closed 1m) holding on the new side with limited reclaim depth; optional min notional on new side.
- **Assigned ts:** `acceptance_ts` when rule first true using only closed buckets.
- **Then:** `final_decision_ts = max(flip_decision_ts, structure_break_ts, acceptance_ts)`.

### CANDIDATE_CONFIRMED

- **Enter:** required stages complete (V1 discovery default: Compression + Flip + Structure break + Acceptance).
- **earliest_entry_ts:** **next closed 1s bucket after `final_decision_ts`** (Discovery variant D1 — see below).
- **earliest_entry_price:** last trade price of that entry bucket (or first trade if empty—then skip/defer).
- **Cooldown:** episode row emitted; enter cooldown.

### INVALIDATED / TIMEOUT

- Terminal without candidate; still logable as negative/incomplete episodes for F5 ablation if desired.

## Cooldown and dedupe

- **Cooldown:** no new `AGGRESSOR_BURST` same direction for `T_cd` after `final_decision_ts` or invalidation.
- **Episode merge:** overlapping compression windows same side → keep highest notional score / earliest decision (deterministic tie: earlier `compression_decision_ts`, then `trade_id` seed).
- **Primary key:** `(symbol, direction, compression_decision_ts, flip_decision_ts, feature_version)` → `episode_id` hash.
- **Prefix parity:** state up to `as_of=T` must equal prefix of longer run ending after T.

## Entry timing recommendation (Discovery)

**Chosen variant D1 — next closed 1s after `final_decision_ts`.**

| Variant | Pros | Cons |
|---|---|---|
| D1 next closed 1s | Matches trade-grain causality; minimal delay | Noisier than 1m |
| D2 next 1m open after acceptance 1m close | Aligns with candle strategies | Adds up to ~1m lag; mixes grains |

**Reason:** Discovery measures trade-efficiency phenomena on seconds; entry should not wait for an unrelated 1m boundary. Later strategy adapters may map D1 → D2 without changing episode identity.

## LONG vs SHORT mirror

| Stage | LONG | SHORT |
|---|---|---|
| Compression side | Sell | Buy |
| Adverse impact | down | up |
| Flip side | Buy | Sell |
| Structure | break micro-high / range high | break micro-low / range low |
| Acceptance | hold above | hold below |
| Invalidation | new low beyond level | new high beyond level |
