+# Short Bot Source Map
+
+This file documents where the short backtester gets its OB, delta, and pool-liquidity data from.
+
+## What this project is
+
+- `backtester/short` holds the frozen short-side research and backtest code.
+- The final bot will later share the same source modules for long and short.
+- The current short strategy baseline is the frozen Rank 1 / Rank 2 version.
+
+## Pool liquidity source
+
+The 5m pool / liquidity-location information comes from:
+
+- `backtester/short/dashboard/research_charts/service.py`
+
+The short backtester calls the research-chart overlay helper from there to load active pools.
+The main entry point used by the backtest is:
+
+- `liquidity_location_overlay_bundle(...)`
+
+The short backtest then turns those overlays into:
+
+- active upper pools
+- active lower pools
+- clustered pools
+- ranked clusters by mass / strength
+
+Relevant short-backtest code:
+
+- `backtester/short/ob_microstructure_breakout_bot/exit_backtest/pool_bounce_backtest.py`
+- `backtester/short/ob_microstructure_breakout_bot/exit_backtest/run_pool_bounce_backtest.py`
+
+## Orderbook source
+
+The orderbook confirmation is read from:
+
+- `backtester/short/ob_microstructure_breakout_bot/data/orderbook.py`
+
+The key helper is:
+
+- `sample_ob_bands(symbol, when)`
+
+The short backtest uses this to compute:
+
+- `ob_ratio_at_watch`
+- `ob_ratio_at_touch`
+- flow confirmation via the `OB >= 1.05` gate
+
+## Public-trade / delta source
+
+The trade-flow confirmation is read from:
+
+- `backtester/short/ob_microstructure_breakout_bot/data/trades.py`
+
+The key helper is:
+
+- `load_trade_window(symbol, start, end)`
+
+The short backtest uses this to compute:
+
+- `delta_at_watch`
+- `delta_at_touch`
+- flow confirmation via the `delta >= 100_000` gate
+
+## Candle / bar source
+
+The entry, touch, and TP/SL simulation uses 5m bars from:
+
+- `backtester/short/ob_microstructure_breakout_bot/data/bars.py`
+
+The bar loader is used to:
+
+- find the first touch candle
+- detect the short-entry reversal candle
+- simulate the trade to TP / SL / timeout
+
+## Frozen strategy rules
+
+The frozen short rule is documented in:
+
+- `backtester/short/results/ob_pool_5m_bounce_rule.md`
+
+Final short baseline report:
+
+- `backtester/short/results/ob_pool_5m_bounce_baseline_rank12.md`
+
+Reference backtest outputs:
+
+- `backtester/short/results/ob_pool_5m_bounce_backtest_full.md`
+- `backtester/short/results/ob_pool_5m_bounce_backtest_full.json`
+- `backtester/short/results/ob_pool_5m_bounce_backtest_locked10.md`
+- `backtester/short/results/ob_pool_5m_bounce_backtest_locked10.json`
+- `backtester/short/results/ob_pool_5m_bounce_backtest_scanner.md`
+- `backtester/short/results/ob_pool_5m_bounce_backtest_scanner.json`
+
+## OB+delta calibration reference
+
+The analysis that motivated the OB / delta gate is mirrored here:
+
+- `backtester/short/results/ob_pool_delta_calibration_analysis.md`
+
+## Short version of the data flow
+
+1. Load 5m bars.
+2. Load active liquidity pools from the research-chart overlay service.
+3. Rank pools and keep Rank 1 / Rank 2 only.
+4. Wait for the touch candle.
+5. Load orderbook snapshot at watch/touch.
+6. Load 10m trade window at watch/touch.
+7. Require OB + delta confirmation.
+8. Load lower pools again at the short-entry candle.
+9. Set TP to the nearest lower pool with enough entry-to-TP room.
+10. Set SL to touched pool top + 0.2%.
+11. Simulate TP / SL / timeout.
