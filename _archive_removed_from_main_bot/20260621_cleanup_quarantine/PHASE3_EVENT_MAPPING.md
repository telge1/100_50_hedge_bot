## Phase 3 Event Mapping (Tier A)

| Event | Derived Features | Guards | Exclusions | Target Engines |
|-------|------------------|--------|------------|----------------|
| **volatility_expansion** | `price_move_vs_atr`, `spread_vs_atr`, `atr_regime_zscore` | >= `VOLATILITY_EXPANSION_THRESHOLD` or `>= VOLATILITY_EXPANSION_Z` | `None` inputs, `trade_intensity_score` missing | Fast (trigger), Mid (context) |
| **thin_orderflow_instability** | `spread_ratio_zscore`, `spread_stress_score`, `trade_intensity_score` | `spread_ratio_zscore >= SPREAD_EXPANSION_Z` AND `trade_intensity_score < TRADE_SURGE_Z * 0.5` | no spread, high participation | Fast, Router via instability flag |
| **fresh_long_build_up** | `oi_abs_zscore`, `oi_delta_zscore`, `price_oi_alignment`, `trade_intensity_score` | price aligned long + `oi_abs_zscore >= OI_BUILD_Z` + `oi_delta_zscore >= OI_BUILD_Z * 0.5` | `price_oi_alignment != 1`, `trade_intensity_score` missing | Slow (conviction), Mid (validation) |
| **fresh_short_build_up** | same as above with `price_oi_alignment == -1` | same analog | same analog | Slow/Mid |
| **high_participation_breakout** | `trade_intensity_score`, `avg_trade_size`, `price_move_vs_atr` | `trade_intensity_score >= TRADE_SURGE_Z`, `avg_trade_size` not None, `price_move_vs_atr >= 0.8` | microburst invalid | Fast, Mid |
| **weak_move_low_participation** | `trade_intensity_score`, `price_move_vs_atr` | `trade_intensity_score < PARTICIPATION_WEAK_THRESHOLD`, `price_move_vs_atr` present | high intensity | Fast/Mid |
| **panic_liquidation_phase** | `panic_liq_score`, `exhaustion_reversal_score`, `price_move_vs_atr` | both scores >= `PANIC_LIQ_THRESHOLD`, `price_move_vs_atr >= VOLATILITY_EXPANSION_THRESHOLD` | derived None | Fast, Mid |
| **squeeze_exhaustion_reversal** | same liquidation scores + `oi_delta_zscore` | panic + exhaustion high + `oi_delta_zscore` sign opposed to last move | low ATR | Fast/Mid |
| **spread_stress_phase** | `spread_stress_score`, `trade_intensity_score` | `spread_stress_score >= SPREAD_STRESS_THRESHOLD`, `trade_intensity_score < TRADE_SURGE_Z` | no volume | Fast, Mid |
| **dirty_breakout_risk** | `spread_stress_score`, `trade_intensity_score`, `volatility_expansion` | `spread_stress_score >= SPREAD_STRESS_THRESHOLD`, `volatility_expansion` true, `high_participation_breakout` false | high participation | Router instability flag |

Each event is strictly derived: no raw fields, no states, and no router logic. Guards and exclusions are literal translations of the Spec’s context rules.

