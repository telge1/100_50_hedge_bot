# Fill-level replay semantics

## Engine path

1. Continuous TEM blockers come from
   `staging_profiles_continuous_1000_500_20260722` (`two_early_medium`, 1000/500).
2. Root-cause and this runner isolated-replay each trade via
   `run_isolated_blocker` → `run_historical_backtest`
   (`config_source=live`, `fill_model=conservative`, profile `two_early_medium`).
3. Candle source: `load_candles_for_symbol` + `normalize_candles`, limit 50000
   (same as continuous / root-cause).

## Cutoff

`before_signal = (fill_timestamp < signal_available_ts)`

- Fills **at** `signal_available_ts` are **not** in the pre-signal book.
- No same-candle lookahead: signal-bar fills are excluded entirely when
  `include_signal_bar_fills=false` (default).
- Open orders at cutoff: last order-log state with event timestamp `< cutoff`;
  no fills applied on/after cutoff.

## Book state

Pre-signal inventory is the `*_after` fields of the last fill with
`before_signal=true`. If no such fill exists but entry is before signal,
state may be empty flat or entry-only depending on fills.

## Fees

Prefer explicit `entry_fee` + `exit_fee` on the fill log.
If missing → do **not** invent; flag `FEE_RECONSTRUCTION_UNRESOLVED`.

## Fingerprint

Full replay (no cutoff) is compared to `tem_end_blockers_27.csv`
(final qty, realized, open_mtm, total_pnl, highest_cycle, duration).
Mismatch → `REPLAY_MISMATCH` (not ready).
