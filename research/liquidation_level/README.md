# Liquidation Levels (LuxAlgo) — Research Replication

Causal Python replication of the Pine indicator **Liquidation Levels [LuxAlgo]**.

## Scope

This folder is intentionally isolated:

- no wiring into `research/regime_scanner`
- no live bots
- no productive strategy / trend state machine
- no entry / TP / SL optimization yet

Goal of this first step:

1. replicate the Pine level logic causally
2. validate the level lifecycle (create → active → sweep / capacity remove)
3. export results on the APTUSDT 5m feather file
4. provide a stable base for a later strategy backtest

## Layout

```
research/liquidation_level/
├── liquidation_levels.py   # core engine
├── liquidation_audit.py    # CLI export
├── tests/
└── results/
```

## Default data

```
/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather
```

If that path is missing, the audit aborts and only prints nearby APT feather hints.
It will not silently switch coins.

## Run audit

```bash
PYTHONPATH=. python3 -m research.liquidation_level.liquidation_audit \
  --feather-file /home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather \
  --symbol APTUSDT \
  --output-dir research/liquidation_level/results/APTUSDT_5m
```

## Event backtest

```bash
PYTHONPATH=. python3 -m research.liquidation_level.run_liquidation_backtest \
  --feather-file /home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather \
  --symbol APTUSDT \
  --output-dir research/liquidation_level/results/APTUSDT_5m_backtest
```

Causal rules:

- sweep known only after candle close
- entry at next open
- variants L1–L7 / S1–S7 / F_LONG / F_SHORT
- horizon + TP/SL grids, 70/30 candle-index split, seeded controls

See `liquidation_features.py` and `liquidation_backtest.py`.


## Semantics (short)

- Reference price modes: `open`, `close`, `oc2`, `hl2`, `hlc3`, `ohlc4`, `hlcc4`
- Volume SMA period: 13 (NaN / no volume flags before warm-up)
- Create when `(lT or nzVd0)` and min-move `eC` and level outside the candle
- Strength 1 / 2 / 3 from `nzVd0` / `nzVd1` / `nzVd2`
- Sweep only on strict cross: `high > level` and `low < level`
- Max 500 active levels; oldest dropped with `removal_reason=max_active_limit`

## Disclaimer

These levels are **estimated**, not real exchange liquidations. This audit does
**not** claim profitability.
