# E1R Freeze V1

## Repository

- **repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`
- **branch:** `pools-ob-delta-bot`
- **pre-freeze HEAD:** `37fcc1c8bee0d25d830fed8df4f42c5b116255c8`
- **freeze commit:** see annotated tag `e1r_cluster3_ema200_shadow_freeze_v1` (`git rev-parse e1r_cluster3_ema200_shadow_freeze_v1^{commit}`)
- **tag:** `e1r_cluster3_ema200_shadow_freeze_v1`

### Git hygiene at freeze time (not part of this commit)

At freeze, the working tree also had **modified** files outside `results/pool_scan/context_study_v1/`:

- `backtester/short/dashboard/research_charts/workspace_session.py`
- `pool_pattern/__init__.py`, `machine.py`, `market.py`, `rules.md`, `test_pattern.py`

and **untracked** runtime dependencies used by reproduction (not staged in freeze commit):

- `backtester/short/dashboard/research_charts/lld_research_kernel.py`
- `pool_scan/`, `pool_state_maschine/`, parts of `pool_pattern/`

These were **not** discarded or cleaned. Freeze commit contains only audited `context_study_v1` artifacts listed below.

## Strategy status

- **Baseline** = existing `with_guard` cohort in `floor_guard_signal_list_v1.json`
- **E1R** = shadow / research only
- **Not** integrated into live strategy or `pool_pattern` production paths

## Fixed E1R logic

Canonical implementation: `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py`

- 15m EMA200 **bullish cross**
- **rising_3** structural lower clusters
- Bull-state: block shorts while active (`close > EMA200` and `rising_3`)
- On `close <= EMA200`: if structure intact → **one** 15m **PENDING** bar
- Successful reclaim → state stays active
- Failed reclaim / structure break → reset
- **No** GAP3 start requirement
- **No** D4 filter
- **No** entry-feature filters

## Test window

2026-06-01 through 2026-07-31 (UTC)

## Coins

- XRPUSDT
- ADAUSDT
- DOGEUSDT

## Final parity (verified 2026-10-03 reproduction run)

| Coin | Baseline trades | E1R trades |
|------|----------------:|-----------:|
| XRP | 25 (8 SL / 17 TP) | 15 (2 SL, 4 TP_FIRST lost) |
| ADA | 18 (7 SL / 11 TP) | 11 (2 SL, 2 TP_FIRST lost) |
| DOGE | 28 (8 SL / 19 TP) | 22 (3 SL, 1 TP_FIRST lost) |

Cross-coin E1R max SL streak: **2** (XRP/DOGE 1, ADA 2)

## Final PnL (non-compounded sum of `pnl_pct`, equity start 100)

| | Baseline | E1R |
|--|---------:|----:|
| Total % | 61.44 | 64.29 |
| Profit factor | 4.29 | 13.08 |
| Max drawdown % | 10.21 | 1.75 |
| Max SL streak | 4 | 2 |

## Blocked trades (baseline executed, E1R blocked)

- **23** total
- **7** winners removed: **+10.49 %** sum
- **16** losses removed: **−13.33 %** sum

## BAD-4 (XRP reference)

- **4/4** blocked by E1R
- Baseline sum PnL: **−4.83 %**

## SHA256 (reference file integrity)

| File | SHA256 |
|------|--------|
| `floor_guard_signal_list_v1.json` | `e88a4cd9bbc2ac510189eaf351eb109653f07dc64d1a31e0940e56791c77cc31` |
| `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py` | `b6c3da1906e5cbb74a707c4cbce0804ceebffd8a098537d730c31968c0e18378` |
| `analyze_crosscoin_e1r_final_pnl_comparison_v1.py` | `8edf72cd29f66554730c0f17031ca40291a764098286100176d824909e5f087c` |
| `analyze_crosscoin_e1r_final_pnl_comparison_v1.json` | `f97aea7b9b792435a5288e1d3508d8d4525097d913ebcfa4b3f23b91a479b2d8` |

## Files in freeze commit (reproducibility chain)

### Signal / baseline

- `floor_guard_signal_list_v1.json`
- `floor_guard_signal_list_v1.md`

### E1R shadow + outputs

- `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py`
- `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.json`
- `analyze_crosscoin_e1_onebar_reclaim_shadow_v1.md`
- `analyze_crosscoin_e1_onebar_reclaim_shadow_trades_v1.csv`

### Final PnL comparison

- `analyze_crosscoin_e1r_final_pnl_comparison_v1.py`
- `analyze_crosscoin_e1r_final_pnl_comparison_v1.md`
- `analyze_crosscoin_e1r_final_pnl_comparison_v1.json`
- `analyze_crosscoin_e1r_final_pnl_trades_v1.csv`
- `analyze_crosscoin_e1r_final_equity_curve_v1.csv`

### Direct Python dependencies (imports)

- `analyze_xrp_ema200_cluster3_persistent_shadow_v1.py`
- `analyze_xrp_bull_regime_exit_shadow_v1.py`
- `analyze_xrp_ema200_cluster_transition_shadow_v1.py`
- `analyze_xrp_pool_ladder_forensics_v1.py`
- `analyze_4h_lower_short_block_v1.py`
- `find_short_entry_15m_v1.py`

## Known limitations

- No fees, funding, or slippage in signal PnL
- No USDT notional / position sizing
- No long-signal validation
- Window limited to Jun–Jul 2026
- E1R not production-integrated
- Full rerun requires local candle data + untracked `lld_research_kernel` / `pool_pattern.market` as present on freeze machine

## Reproduction commands

From repository root (with Python path / data as on freeze machine):

```bash
python results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py
python results/pool_scan/context_study_v1/analyze_crosscoin_e1r_final_pnl_comparison_v1.py
```

Expect: `parity_ok True` and metrics matching this document.

## Freeze policy

**Do not modify V1 artifacts in place.**

Any future work must use:

- a new branch, and/or
- new versioned filenames (`_v2`, etc.)

Do not overwrite frozen V1 files; add successors alongside.
