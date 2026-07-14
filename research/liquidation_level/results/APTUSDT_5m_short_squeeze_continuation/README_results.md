# Short Squeeze Continuation Audit

## Disclaimer

Upper liquidation levels are **estimated** LuxAlgo-style levels.
They are **not** real exchange liquidation feeds.

Symbol: `APTUSDT`  
Feather: `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather`  
Candles: `52569`  
Period: `2025-12-27 00:00:00+00:00` → `2026-06-27 12:40:00+00:00`  
IS cut index: `36798`

Entry is never on the sweep candle. For reclaim trades, entry is the open after
the reclaim becomes known.

Trend models T1/T2/T3 are transparent EMA/structure filters on closed 15m/30m bars.
Trend-state-machine T4 was omitted (not reproducibly available without touching protected modules).

## 1–2. Does price fall after upper 50x / 25x sweeps?

Event counts (full): `{
  "upper_100x": 4279,
  "upper_50x": 4147,
  "upper_25x": 3487
}`

Key h12 hit rates (full): see `variant_comparison.csv` and `summary_full.json`.

## 3. Difference with vs without bearish reclaim

Reclaim counts: `{
  "immediate_reclaim": 5966,
  "delayed_reclaim_1_to_3": 3162,
  "no_reclaim_within_3": 2785
}`

Compare groups:
- `upper_50x__no_reclaim_within_3`
- `upper_50x__immediate_reclaim`
- `upper_50x__reclaim_within_3`

## 4–5. Stronger in downtrend? Which trend model?

Compare `__T1` / `__T2` / `__T3` variants in `variant_comparison.csv`.
Prefer the model with the most stable Full→OOS hit-rate/MFE profile, not the flashiest IS number.

## 6. OOS confirmation?

Compare `summary_out_of_sample.json` against full for the same keys.
OOS snapshot keys: `['upper_50x__sweep_only_h12_thr0.25', 'upper_50x__sweep_only_h12_thr0.5', 'upper_50x__sweep_only_h12_thr1.0', 'upper_50x__no_reclaim_within_3_h12_thr0.25', 'upper_50x__no_reclaim_within_3_h12_thr0.5', 'upper_50x__no_reclaim_within_3_h12_thr1.0']...`

## 7. vs matched controls?

`control_comparison.csv` (month/hour/range matched, non-sweep). Bootstrap CIs are
descriptive only — **no significance claim**.

Control rows (full): `[
  {
    "group": "upper_50x_reclaim_T1",
    "sample": "full",
    "event_n": 85,
    "control_n": 85,
    "event_mean_mfe_h12": 1.5113608406929617,
    "control_mean_mfe_h12": 1.6093716720897968,
    "event_minus_control_mfe": -0.09801083139683509,
    "event_hit_0_25": 87.05882352941177,
    "control_hit_0_25": 87.05882352941177,
    "event_hit_0_50": 68.23529411764706,
    "control_hit_0_50": 72.94117647058823,
    "event_hit_1_00": 51.76470588235294,
    "control_hit_1_00": 56.470588235294116,
    "event_mean_close_return_h12": 0.33671504136403935,
    "control_mean_close_return_h12": 0.3271640785038417,
    "event_fav_before_adv_0_25": 42.35294117647059,
    "bootstrap_mfe_diff_mean": -0.09801083139683509,
    "bootstrap_mfe_diff_ci95_low": -0.731186369654601,
    "bootstrap_mfe_diff_ci95_high": 0.48292492867294595,
    "note": "empirical only; not a formal significance claim"
  },
  {
    "group": "upper_25x_reclaim_T1",
    "sample": "full",
    "event_n": 9,
    "control_n": 9,
    "event_mean_mfe_h12": 1.2814177922135548,
    "control_mean_mfe_h12": 1.6127764434763074,
    "event_minus_control_mfe": -0.33135865126275266,
    "event_hit_0_25": 77.77777777777777,
    "control_hit_0_25": 100.0,
    "event_hit_0_50": 77.77777777777777,
    "control_hit_0_50": 88.88888888888889,
    "event_hit_1_00": 77.77777777777777,
    "control_hit_1_00": 66.66666666666667,
    "event_mean_close_return_h12": 0.5527828548637476,
    "control_mean_close_return_h12": 0.6941490215338741,
    "event_fav_before_adv_0_25": 11.11111111111111,
    "bootstrap_mfe_diff_mean": -0.33135865126275266,
    "bootstrap_mfe_diff_ci95_low": -1.112495462502028,
    "bootstrap_mfe_diff_ci95_high": 0.3766011818027156,
    "note": "empirical only; not a formal significance claim"
  }
]`

## 8. MAE before continuation

See `first_touch_outcomes.csv` (`mean_adverse_before_favorable_pct`) and horizon MAE columns.

## 9. After 0.12% costs?

Best first-signal-only TP/SL (full): `{
  "group": "combo_50x_25x__reclaim_within_3__T3",
  "sample": "full",
  "mode": "first_signal_only",
  "tp_pct": 2.0,
  "sl_pct": 0.25,
  "max_hold": 12,
  "trades": 1,
  "wins": 1,
  "losses": 0,
  "timeouts": 0,
  "winrate_pct": 100.0,
  "mean_gross_return_pct": 2.0408163265306145,
  "median_gross_return_pct": 2.0408163265306145,
  "sum_gross_return_pct": 2.0408163265306145,
  "mean_net_return_pct": 1.9208163265306144,
  "median_net_return_pct": 1.9208163265306144,
  "sum_net_return_pct": 1.9208163265306144,
  "profit_factor_gross": null,
  "profit_factor_net": null,
  "max_drawdown_serial_net_pct": 0.0
}`

If mean_net ≤ 0 or PF_net < 1, there is no clear tradable edge after costs.

## 10. 6 March 2026 events

March window summary: `{
  "window": "2026-03-05 .. 2026-03-10 UTC",
  "n_events_50_25": 144,
  "n_march_06": 17,
  "n_reclaim": 140,
  "n_t2": 0,
  "n_t3": 2,
  "disclaimer": "Estimated LuxAlgo levels; not real exchange liquidations.",
  "t4_trend_state_machine": "omitted \u2014 not reproducibly available without touching protected modules"
}`

See `march_downtrend_events.csv` (is_march_06 flag).

## Integration

No scanner / bot / live integration from this audit.
