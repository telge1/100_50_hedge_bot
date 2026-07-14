# Liquidation Level Event Backtest Results

## What was tested

Causal event backtest on LuxAlgo-style **estimated** liquidation levels
(not real exchange liquidation feeds).

Pipeline:

1. Replay levels on APTUSDT 5m candles
2. Build sweep events (per level, per candle-side, per cluster)
3. Generate variants L1–L7, S1–S7, F_LONG, F_SHORT
4. Enter at the **open of the next candle** after the sweep candle closes
5. Evaluate fixed horizons and optional TP/SL grids
6. Compare against deterministic random-entry controls (seeded)

## Entry timing

A sweep is only known after the sweep candle **closes**.
Therefore entries are never on the sweep candle itself; the earliest entry is
the **next candle open**.

## Single level vs cluster

- **Candle sweep event**: all upper (or lower) levels swept on the same candle,
  aggregated once so many levels do not spawn duplicate identical trades.
- **Cluster**: research-only grouping of nearby active levels (default gap
  0.10%) before the sweep candle. A cluster sweep needs ≥2 swept members or
  swept strength ≥3.

## Data

- Feather: `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather`
- Candles: `52569`
- Level sweep events: `25370`
- Candle sweep events: `5754`
- Cluster sweeps: `4682`
- Signals total: `22582`
- Signals by variant: `{
  "L1": 2853,
  "L2": 1686,
  "L3": 1716,
  "L4": 2341,
  "L5": 498,
  "L6": 791,
  "L7": 437,
  "S1": 2901,
  "S2": 1803,
  "S3": 1758,
  "S4": 2341,
  "S5": 460,
  "S6": 677,
  "S7": 378,
  "F_SHORT": 846,
  "F_LONG": 1096
}`
- In-sample cut index: `36798`
- Round-trip cost: `0.12` %

## Highest signal quality (horizon 12, mean net after costs)

- Best long (full): L2 | n=1686 | mean_gross=0.028478539784168564 | mean_net=-0.09152146021583149
- Best short (full): S5 | n=460 | mean_gross=0.07420674919342718 | mean_net=-0.045793250806572906
- Best long (out-of-sample): L7 | n=140 | mean_gross=0.10762434468107644 | mean_net=-0.012375655318923633
- Best short (out-of-sample): S5 | n=160 | mean_gross=0.1127916930087776 | mean_net=-0.007208306991222479

## Costs (0.12%)

- Best long mean net (full): `-0.09152146021583149`
- Best short mean net (full): `-0.045793250806572906`

Positive **gross** with negative **net** means the edge does not clear costs.

## Control comparison (horizon 12, full)

- Long best variant control: event-control=0.05033127430696366 | frac_controls_better=0.0 (empirical share only; not a formal significance claim)
- Short best variant control: event-control=0.048116936696277604 | frac_controls_better=0.13 (empirical share only; not a formal significance claim)

The control share is an empirical bootstrap-style fraction only.
This audit does **not** claim classical statistical significance.

## Honest reading

- Interesting ≠ tradeable.
- No overlap filtering was applied (each event tested independently).
- No parameter selection was done on out-of-sample data in this runner.
- No scanner/live integration recommended from this run alone: treat results as research diagnostics until a variant is robust out-of-sample and clearly above its random-entry control.

## Not claimed

This does **not** prove a profitable live strategy and does **not** authorize
integration into the regime scanner, trend state machine, or bots.
