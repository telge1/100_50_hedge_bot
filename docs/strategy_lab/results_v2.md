# Strategy Lab result contracts (P2A)

Minimal immutable run/trade result types for later adapters. No execution logic.

## Types

- `StrategyTradeV2` — one isolated trade outcome
- `StrategyRunResultV2` — one compiled-strategy window result
- `SourceEventIdV2` — opaque legacy candidate/event id (`edc:…` / `csw:…`), never normalized
- `TradeExitReasonV2` / `StrategyRunStatusV2` — closed enums mapped from legacy

## Optionality (closed trade contract)

### Resolved exits (`tp_exit`, `sl_exit`, `time_exit`)

All required and non-`None`:

`exit_time`, `exit_price`, `gross_return_pct`, `roundtrip_cost_pct`, `net_return_pct`,
`gross_pnl_usdt`, `costs_usdt`, `net_pnl_usdt`

### Unresolved exits (`coverage_missing`, `incomplete_outcome_horizon`)

Legacy (`tpsl_pnl_engine` + `apply_costs` when gross is missing):

- `exit_time`, `exit_price`, returns, and all PnL/`costs_usdt` fields **must be `None`**
- `roundtrip_cost_pct` remains explicit (`>= 0`)
- `entry_*` / `decision_time` remain present

No half-filled mixes (e.g. `tp_exit` without exit price).

## Units and time

- Returns and roundtrip cost: **percent** (`RateUnit.PERCENT`; `0.11` = 0.11%)
- Prices / PnL: exact `Decimal` (no float)
- All timestamps: timezone-aware **UTC** (zero offset); no silent conversion
- `entry_time >= decision_time`; resolved: `exit_time >= entry_time`
- Run: `end > start`
- Legacy signal filter is half-open on candidate/event **open** time `[start, end)`
- `decision_time` is bar **close** → may equal or exceed `end` when the bar straddles the exclusive end (`decision_time == end` is allowed; no `decision_time < end` rule)
- Exit may be after `end` (outcome padding); no `exit_time <= end`

## Run status

`complete` / `failed` / `failed_parity` are status facts only. Failed/parity-failed legacy
runs typically have empty `trades` with `candidate_count >= 0`; the model does not invent
a separate error union.

## Derived properties (not stored)

- `trade_count`
- `gross_pnl_usdt` / `costs_usdt` / `net_pnl_usdt` (Decimal sums from `Decimal("0")`, skip `None`)
- `symbols_with_trades` (sorted unique)

## Deferred (not in P2A)

MFE/MAE, regime, explainability, winrate/drawdown, adapters, backtests.

## Arithmetic

Models do not recompute PnL. Exact `gross − cost` checks are deferred to adapter parity
because the legacy engine uses float + `round(..., 6)`.
