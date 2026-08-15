# Position state semantics (`blocker_cycle_timelines.csv`)

## Writer

`research/backtests/run_tem_continuous_27_blocker_root_cause.py` → `build_cycle_timeline()`.

## What each cycle row means

| Field | Semantics |
|---|---|
| `start_bar` / `first_leg_fill_bar` | Absolute bar of the first attributed cycle fill (typically `CYCLE_N_LONG_ADD`) |
| `long_qty` / `short_qty` / `long_avg` / `short_avg` | Inventory **after the last fill attributed to this cycle** (first- and second-leg fills overwrite) |
| `second_leg_fills` | Count of non-first-leg fills in the cycle |
| `duration_bars` | `last_fill_bar - start_bar + 1` |
| `cycle_open_mtm` | Unrealized MTM at the **last-fill** candle close |
| `cycle_total_pnl` | Cumulative realized (through last fill) + that MTM |
| `first_leg_realized_loss` / `realized_cover_net` | Partial realized components from fill PnL fields |

This is **not** a cycle-start snapshot and **not** a first-leg-only snapshot unless `second_leg_fills == 0` and only one fill occurred.

## Fill-level availability

Root-cause artifacts do **not** retain a per-fill ledger with timestamps for the 27 blockers.
`tem_fd_resolved_timelines.json` only repeats the same cycle aggregates.

Therefore exact inventory at an arbitrary `signal_available_ts` is only proven when:

`last_fill_bar < tradeable_signal_bar`

for the latest cycle that traded before the signal, with no later cycle starting before the signal.

If `first_leg_fill_bar < tradeable_bar <= last_fill_bar`, the cycle is **active across the signal** and the state is `POSITION_SEMANTICS_UNRESOLVED` (no interpolation).

## Fees

No cumulative fee field exists in these cycle timelines. `fees_before` is left empty with flag `FEES_NOT_IN_SOURCE`.
