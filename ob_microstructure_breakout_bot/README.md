# ob_microstructure_breakout_bot

Rule engine for distinguishing **fakeouts** from **real breakouts** using:

- Public Trades (buyer/seller aggression / delta)
- Order Book bid/ask band notional (5bps / 10bps)
- EMA9 / EMA20 / EMA59 structure

## Goal

Prefer fewer high-quality signals. Coin thresholds are **config-driven** (DOGE first).

## Layout

```
ob_microstructure_breakout_bot/
  config/doge_usdt.yaml   # coin-specific thresholds
  models.py               # shared dataclasses
  thresholds.py           # config loader
  ema.py                  # EMA helpers + exit hierarchy
  rule_engine.py          # classify setup / breakout / exit
  data/                   # trade + OB loaders (backtest)
  backtest/               # DOGE window runner + known cases
  tests/
```

## Quick start

```bash
# from repo root
PYTHONPATH=. python -m ob_microstructure_breakout_bot.backtest.runner --symbol DOGEUSDT --list-cases
PYTHONPATH=. python -m ob_microstructure_breakout_bot.backtest.runner --symbol DOGEUSDT --all-cases
# live: ClickHouse public trades + Full-OB archives (known labeled cases)
PYTHONPATH=. python -m ob_microstructure_breakout_bot.backtest.runner --symbol DOGEUSDT --all-cases --live
# discovery: find EMA59 touches automatically (no hardcoded labels)
PYTHONPATH=. python -m ob_microstructure_breakout_bot.backtest.runner --scan \
  --scan-from 2026-09-16T00:00:00 --scan-to 2026-09-20T00:00:00
PYTHONPATH=. python -m pytest ob_microstructure_breakout_bot/tests -q
```

## States

| State | Meaning |
|-------|---------|
| `setup` | EMA59 touch / pre-break context |
| `accumulation` | Box hold with bid absorption |
| `breakout_confirmed` | Accepted expansion (tier 1 or 2) |
| `fakeout` | Failed push / rejection (tier 0) |
| `chop` | Flipping bid/ask, no durable leader |
| `exit_warning` | EMA9 lost EMA59 / early weakness |
| `exit_confirmed` | EMA20 lost EMA59 or structure failed |

## Status

Scaffold + DOGE thresholds from calibration research. Live trading not wired yet.
