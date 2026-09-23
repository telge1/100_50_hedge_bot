# Exit Backtest Plan

Backtest SL/TP exits for calibrated breakout signals.

This folder owns the exit simulation. Signal discovery stays in `calibration/`.
`calibration_v2_dump/` is experiment-only and is not an input source.

## Frozen baselines (do not casually retune)

Two locked-10 snapshots for later A/B when collectors cover more history:

| Baseline | Report | Idea | Locked-10 |
|---|---|---|---|
| **phase1h** (default) | `long_exit_phase1h_fee_filter.json` | 5m ladder + 1m mass TP + fee skip | 8 traded / 2 ignored · WR 62.5% · mean **+0.84%** · sum **+6.71%** · TP/SL 5/3 |
| **phase1e** | `long_exit_phase1e_5m_meaningful.json` | 5m meaningful ladder (pre mass/fee) | 10 traded · WR 80% · mean **+0.80%** · sum **+7.98%** · TP/SL 8/2 |

Code / thresholds currently match **phase1h** live simulation. phase1e is frozen as the
report artifact + dashboard overlay only (replay the JSON, do not silently retune gates).

Dashboard: choose either baseline in the Research Backtester dropdown, then **Backtester**.

Scanner cohort check (phase1h rules): `reports/long_exit_full_history_phase1h.json`
→ `by_source.scanner_breakout`.

Out of scope for this freeze: spike/EMA9-flow runner (separate phase later).

## Phase 1 scope

- **Only the 10 calibrated Long signals** (plus optional `--universe full` for checks)
- Gross PnL in reports; fee used only as **entry skip** on phase1h (`reward_below_fees`)
- Rules from `EXIT_RULES.md` in this folder
- No-lookahead: only data known at each closed 5m bar

## Input signals (calibrated YAML run)

Source: `calibration/events/DOGEUSDT_backtest_legacy_vs_calibrated.json`
→ `calibrated.breakouts` where `side == "long"`.

| decision_ts | tier |
|---|---|
| 2026-09-07T08:45:00Z | tier2_strong |
| 2026-09-07T19:35:00Z | tier1_valid |
| 2026-09-08T20:25:00Z | tier2_strong |
| 2026-09-11T02:15:00Z | tier1_valid |
| 2026-09-13T14:25:00Z | tier2_strong |
| 2026-09-14T02:15:00Z | tier1_valid |
| 2026-09-15T17:50:00Z | tier2_strong |
| 2026-09-15T23:30:00Z | tier1_valid |
| 2026-09-16T18:15:00Z | tier2_strong |
| 2026-09-17T23:20:00Z | tier1_valid |

Shorts are out of scope until Long exits are stable.

## Per-trade steps

1. Entry = `decision_ts` close (or open of next closed bar — freeze one rule in code).
2. SL = last confirmed lower-low candle low × `0.9985` (0.15% buffer).
3. TP from **5m** liquidity pools as of entry (first / second upper pool per `EXIT_RULES.md`).
4. Walk 5m bars forward until SL or TP hits (SL checked before TP on same bar).
5. Record exit reason and raw P/L %.

## Output fields per trade

- `decision_ts`, `tier`, `side`
- `entry_price`, `sl_price`, `tp_price`
- `exit_ts`, `exit_price`, `exit_reason` (`sl` | `tp` | `ignored` | `open`)
- `pnl_pct` (raw, no fees)
- optional: first/second pool edges used

## Aggregate metrics

- winrate
- mean / sum P/L %
- best / worst trade
- count ignored vs traded

## Folder layout

```text
exit_backtest/
  PLAN.md
  EXIT_RULES.md
  events/      # optional frozen trade inputs
  reports/     # JSON/MD results
  tests/
```

## Explicit non-goals for Phase 1

- Shorts
- Fees / slippage
- Partial exits / BE move (document later; full exit on SL/TP first)
- Re-running Phase A/B/C discovery
