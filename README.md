+# pools+ob+delta_bot
+
+New unified workspace for the pooled liquidity / OB / delta bot.
+
+## Layout
+
+- `backtester/short` - frozen short-side research, backtests, and reports
+- `backtester/long` - future long-side research and backtests
+- `bot` - final combined trading bot
+- `docs` - source maps and architecture notes
+- `results` - shared outputs and reports
+
+## Short baseline
+
+The frozen short baseline is documented in:
+
+- `docs/short-source-map.md`
+- `backtester/short/results/ob_pool_5m_bounce_rule.md`
+- `backtester/short/results/ob_pool_5m_bounce_baseline_rank12.md`
+
+## Long baseline
+
+The long-side source map is documented in:
+
+- `docs/long-source-map.md`
+
+## Data sources
+
+Shared market data comes from the copied `ob_microstructure_breakout_bot`
+and `dashboard` code trees inside `backtester/short`.
+
+## Purpose
+
+This workspace replaces the old branch-based workflow with a single clean
+project structure for:
+
+- short research
+- long research
+- final combined bot work
+
