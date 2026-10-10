# Historical LMV2 — Floor-Guard Short Baseline V1

Isolated research backtester using **original** Live Micro LLD V2 snapshot JSONs.

## Strategy source (unchanged)

Imports from `results/pool_scan/context_study_v1/`:

- `find_short_entry_15m_v1` — entry geometry, `down_4h()`
- `short_block_4h_guard` — floor guard semantics (V2 snapshot via `guard_v2.py`)
- `analyze_floor_guard_signal_list_v1` / `analyze_xrp_floor_guard_pnl_v1` — reference loops

E1R is **not** used.

## Variants

Six runs: 15m PD1/PD5/PD10 × floor guard OFF / ON (4h PD10 when ON).

`down_4h()` is always ON (4h EMA from candles).

## Research limitations

- **Fill:** signal entry price = **15m bar close** (no intrabar entry, no fees, no slippage).
- **Pools:** V2 ACTIVE snapshot only — no legacy chart selection.
- **Catalog:** only `MONTH_STATUS=COMPLETE` and `BACKTESTER_CANDIDATE=true` months.

## Usage (phase 1)

```bash
cd backtester/short
python -m dashboard.research_charts.historical_lmv2.runner \
  --production-root dashboard/research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history \
  --list-catalog
```

Full historical walks: use `run_baseline_walk()` from tests/smoke once enabled.
