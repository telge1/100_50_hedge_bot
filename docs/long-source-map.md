+# Long Bot Source Map
+
+This file documents where the long-side backtester and calibration logic come from.
+It is the blueprint for the future `backtester/long` area.
+
+## What the long side is
+
+The long side is the signal/entry/TP calibration path that works on breakout-long
+signals, not the pool-bounce short setup.
+
+It uses the same shared market-data primitives as the short side:
+
+- 5m bars
+- orderbook snapshots
+- trade-window delta
+
+## Shared market-data sources
+
+These modules provide the raw inputs for both long and short:
+
+- `ob_microstructure_breakout_bot/data/bars.py`
+- `ob_microstructure_breakout_bot/data/orderbook.py`
+- `ob_microstructure_breakout_bot/data/trades.py`
+
+Typical responsibilities:
+
+- bars: candle history, entry candles, forward simulation
+- orderbook: bid/ask balance and OB ratios
+- trades: public-trade windows and delta / notional flow
+
+## Long-side strategy and calibration code
+
+These are the long-specific modules that should be mirrored into `backtester/long`:
+
+- `ob_microstructure_breakout_bot/exit_backtest/run_longs.py`
+- `ob_microstructure_breakout_bot/exit_backtest/simulate_long.py`
+- `ob_microstructure_breakout_bot/exit_backtest/calibrate_tp_from_touches.py`
+- `ob_microstructure_breakout_bot/exit_backtest/cluster_mass.py`
+- `ob_microstructure_breakout_bot/exit_backtest/ema59_touch_cluster.py`
+
+What they do:
+
+- build the long signal universe
+- load the long-entry context
+- calibrate TP logic from touch events
+- simulate long trades with fees / exits
+- aggregate results and summary metrics
+
+## Long research / report outputs
+
+The current long-side report family in the source repo is:
+
+- `results/long_exit_phase1h_fee_filter.json`
+- `results/long_exit_phase1h_fee_filter.md`
+- `results/long_exit_full_history_phase1h.json`
+- `results/long_exit_full_history_phase1h.md`
+- `results/long_exit_phase1g_calibrated_mass.json`
+
+Other long-side calibration and comparison reports may be added here later.
+
+## Long-side data flow
+
+1. Load the signal universe.
+2. Load the relevant 5m bar window.
+3. Check entry / approach context.
+4. Read OB and delta around the decision candle.
+5. Simulate the long trade forward.
+6. Apply fees and the selected TP/SL logic.
+7. Aggregate results into the long baseline report.
+
+## How this maps to the future project layout
+
+The future cleaned layout should be:
+
+- `backtester/long/` for long strategy code and reports
+- `backtester/short/` for the frozen short baseline
+- `bot/` for the final unified bot that consumes both sides
+
+## Relation to the frozen short baseline
+
+The long side should stay separate from the frozen short baseline.
+Do not mix short freeze files into the long calibration area.
+
