# June 2026 frozen E1R V1 reproduction

**Verdict:** `FROZEN_JUNE_REPRODUCTION_PASS`

## Import audit
- Entry: `/home/telgenbuescher/projects/pools+ob+delta_bot/frozen_strategies/e1r_cluster3_ema200_v1/reproduction_test_june_2026/run_reproduction_june_2026.py`
- E1R module: `/home/telgenbuescher/projects/pools+ob+delta_bot/frozen_strategies/e1r_cluster3_ema200_v1/code/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py` (frozen=True)
- Signal baseline: `analyze_floor_guard_signal_list_v1` (context_study_v1, documented pipeline)
- E1R sim window: Apr–Jul 2026 (same as 4mo OOS)

## Metrics (E1R allowed, June)

| | Reference OOS | Reproduction |
|---|---:|---:|
| Trades | 20 | 20 |
| TP | 19 | 19 |
| SL | 1 | 1 |
| PnL % | 35.8015 | 35.8015 |
| PF | 52.7662 | 52.7662 |
| Max DD % | 0.6916 | 0.6916 |
| Max SL streak | 1 | 1 |

- with_guard June: ref funnel 37 / repro 37
- blocked June: ref 17 / repro 17
- allowed ID parity: True
