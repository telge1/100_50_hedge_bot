-- TRADE horizon convention for Frozen BE50 path outcomes.
-- Stored in existing signal_outcomes with horizon = 'TRADE'.
-- Trade fields live in metadata JSON (result, entry_*, exit_*, be50_*, pnl_pct, duration_seconds).
-- Multi-horizon MFE/MAE rows (15m/30m/1h/4h) remain unchanged.
-- Idempotent: no schema alteration required.
SELECT 1;
