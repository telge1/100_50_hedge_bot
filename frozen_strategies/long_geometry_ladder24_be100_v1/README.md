# LONG GEOMETRY LADDER24 BE100 V1

Frozen research reference for the validated long candidate. **Not live-integrated.** No orders.

## Entry

- Existing **long geometry V1** on 15m (mirrored short pool logic).
- Bearish 15m candle, lower attach from above, bridge gap, TP at next upper, SL below entry lower (`STOP_PAD`).
- Entry price = **15m bar close**.
- One signal per `pool_id` (dedup).
- **No** mandatory `up_4h` filter.

## Quality filter (frozen)

- `m15_lower_2_age_h <= 24.0` at entry decision time (causal pool ladder age).

## Risk management (frozen)

- When favorable excursion reaches **+1.00%** vs entry, stop moves to **entry** on the **next** management bar.
- Management: **1m** bars when data exists in `[2026-05-01, 2026-08-15]` UTC; otherwise **15m** fallback (same as OOS validation).

## Why this package

| Step | Jun–Jul (research) | Apr–May (OOS) |
|------|-------------------|---------------|
| Geometry only | DD ~9.05% | DD ~13.50% |
| + Ladder ≤24h | DD ~2.98% | DD ~4.50% |
| + BE @1.00% | DD ~1.91% | DD ~3.16% |

## Assumptions

- Jun–Jul: in-sample filter development window.
- Apr–May: out-of-sample validation.
- PnL = sum of per-trade % (no fees, funding, slippage).
- Pools/scanner via repo ClickHouse + LLD causal kernel (see runtime deps below).

## Reproduce

From repo root:

```bash
python -m frozen_strategies.long_geometry_ladder24_be100_v1.reproduce --period jun-jul
python -m frozen_strategies.long_geometry_ladder24_be100_v1.reproduce --period apr-may
python -m frozen_strategies.long_geometry_ladder24_be100_v1.reproduce --write-hashes
python -m frozen_strategies.long_geometry_ladder24_be100_v1.reproduce --verify-hashes
```

## Runtime dependencies (not vendored)

- `pool_pattern.market.ensure_paths`
- `dashboard.research_charts.lld_research_kernel` (15m pane + causal pools)
- `find_short_entry_15m_v1.build_15m_bars` (repo research path for bar shape only)
- ClickHouse candles for 15m / 1m

Frozen logic lives in this directory; do not edit in place — create `v2` for changes.

## Files

| File | Role |
|------|------|
| `config.py` | All frozen constants |
| `geometry.py` | Entry setup |
| `ladder.py` | Ladder age filter |
| `outcome.py` | Baseline + BE simulation |
| `strategy.py` | Period runner |
| `metrics.py` | Aggregates |
| `reproduce.py` | Parity checks vs research |
| `manifest.json` | Freeze metadata |
