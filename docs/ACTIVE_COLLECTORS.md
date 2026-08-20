# Active data-collection stack

## Naming: `orderbook_v2` is not obsolete

The package `src/orderbook_analyse/orderbook_v2/` is the **shared feature library**.
Live and historical paths both use it. Feature rows are tagged
`parser_version = ob200_v3` (V3 semantics live *inside* this package).

| Package | Role | Keep? |
|---|---|---|
| `orderbook_v2` | Book, features, dynamics, ClickHouse writer for `ob200_v3` | **YES** |
| `orderbook_v2_live` | Live WebSocket collector (ada / shadow3 / universe51) | **YES** |
| `oi_liquidation_collector` | Live OI + liquidations (no orderbook) | **YES** |

Do **not** delete or archive `orderbook_v2` when cleaning research audits.

## How to start (reference only)

```bash
# OI + liquidations (production-style; do not disrupt casually)
python -m orderbook_analyse.oi_liquidation_collector --mode live --duration 0

# Orderbook live 51 (shadow / later systemd — needs --confirm-universe-51)
python -m orderbook_analyse.orderbook_v2_live --mode universe51 --confirm-universe-51
```

## Canonical PASS artifact dirs (keep under `results/`)

- `results/orderbook_v3_live_pilot/`
- `results/orderbook_v3_live_multisymbol_pilot/`
- `results/orderbook_v3_live_51_shadow/`
- `results/orderbook_v3_51_coin_final_audit/`
- `results/orderbook_v3_48_coin_rollout/`
- `results/oi_liquidation_collector/`

Research one-offs belong under `archive/20260820_cleanup/results/`.
