# StrategySpec V2 — run_intent / candidate_discovery (implemented)

**Status:** implemented on `strategy_spec/v2` (additive; no silent V1 fallback)  
**Strategy:** `strategies/strategy_lab/ema_zone_microstructure_confirmation_v1.yaml`

## Run intent

| Value | Root type | Trade fields |
|-------|-----------|--------------|
| `trade_backtest` (default if omitted) | `TradeBacktestStrategySpecV2` | entry/exit/costs/portfolio required |
| `candidate_discovery` | `CandidateDiscoveryStrategySpecV2` | entry/exit/costs/portfolio **forbidden** |

Missing `run_intent` on existing YAML → `trade_backtest` (backward compatible).

## Compiler safety

- `compile_strategy_v2` → trade only; rejects candidate with `CANDIDATE_DISCOVERY_NOT_TRADE_BACKTEST`
- `compile_candidate_discovery_v2` → candidate only; no dummy entries / PnL

## Plugin contract

`CandidatePluginDescriptorV2` for `ema_zone_microstructure_confirmation`:

- `contract_status: research_contract_only`
- no entry enums
- `candidate_states` required

## Data sources

| Kind | Meaning |
|------|---------|
| `orderbook_ob200_v3_1m` | existing 1m aggregate |
| `orderbook_ob200_v3_raw` | per-level closed raw OB200 archive |
| `public_trades_1m` | existing 1m aggregate |
| `public_trades_native` | native/tick trades |

Metadata registration only — no new productive loader / no live collector changes.
