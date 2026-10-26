# PHASE BT-D9 — First Complete 24h Depth2 Short PnL Backtest V1

**Verdict:** `BT_D9_24H_PNL_PASS`

**Strategy label:** `EXPERIMENTAL_NOT_FINAL_APPROVED` — `pool_landscape_v1_candidate`, TP `next_eligible_band`.

**Window (half-open):** DOGEUSDT `2026-09-11T12:00:00Z` → `2026-09-12T12:00:00Z` (end exclusive).

> Technical completion is **not** proof of profitable strategy.

## 1. Preflight

- Initial: `BT_D9_24H_PNL_PASS` blockers=[]
- Final (post-cache): `BT_D9_24H_PNL_PASS`

## 2. Cache integrity

- Build: {
"bundle_dir": "/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
"entry_count": 288,
"reused_entries": 25,
"cold_computed_entries": 263,
"cold_compute_total_s": 1520.677,
"wall_s": 1521.08
}
- Verify: {
"completeness": {
"ok": true,
"missing_count": 0,
"sample_close": "2026-09-11T12:00:00Z"
},
"manifest_entry_count": 288,
"sample_bands": 221
}

## 3. Activity counts

- Signals: **3**
- Orders: **5** (includes pending placements, cancellations, no-fill outcomes)
- Trades: **0** — no realized round-trips; **not** a strategy performance claim

## 4. Gesamt-PnL

| Field | Value |
|-------|-------|
| initial_equity_usdt | 10000.0 |
| ending_equity_usdt | 10000.0 |
| total_net_pnl_usdt | 0.0 |
| total_return_pct | 0.0 |
| gross_pnl_usdt | 0.0 |
| trading_fees_usdt | 0.0 |
| slippage_cost_usdt | 0.0 |
| funding_fees_usdt | 0.0 |
| total_trades | 0 |
| winning_trades | 0 |
| losing_trades | 0 |
| win_rate_pct | 0.0 |
| profit_factor | None |
| max_drawdown_pct | 0.0 |

## 5. Trade ledger

```json
[]
```

## 6. Equity / drawdown

```json
[
  {
    "utc": "start",
    "equity_usdt": 10000.0
  }
]
```

## 7. Config

- Config hash: `304d6a24413822df6bd474c87e685f8b156dfc48930b54a342a2384fbce27bbc`
- Experiment: `/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d6_config_experiments_v1/bt_d9_24h_run_304d6a244138_20261025T105617Z`

## 8. Runtime

- Backtest wall: **0.23 s**

## 9. Limitations

- Depth-2 exact unique-origin density via validated BT-D8 cache only.
- TP cluster user rule (`dense_cluster`) not executed.
- Single symbol, fixed 24h window, no optimization.

## 10. Density provenance

```json
{
  "bundle": "/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
  "pipeline_line_id": "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
  "config_hash": "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
  "entry_count": 288
}
```
