# ASK Pool 02:27:36 Wall Public-Trade Reaction Audit (research)

Single-case causal research audit for BTCUSDT ASK pool arrival
`2026-08-26T02:27:36Z`. Not a strategy, short signal, or proven absorption.

## Allowed evidence label

`MIXED_WALL_REACTION`

## Semantics

- Wall A / Wall B are separate identities
- Canonical public trades (`public_trades_canonical`), dedupe by `trade_id`
- Buy/Sell = taker aggressor
- Trade-impact windows; refill/reduction/depletion only cautiously
- No queue reconstruction, no PnL/outcomes
- Reaction timestamp is causal; prefix-check required
- Classification uses no data after cluster end
