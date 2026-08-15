# Trend Forecast Validation (isolated research)

Causal audit of whether **existing** C3.4 / C3.5 trend-scanner events forecast subsequent APTUSDT price paths.

## Scope

- No hedge / bot / runtime changes
- No new trading signals
- No optimization / grid search in v1
- Outputs only under `research/trend_forecast_validation/results/`

## Reused components

- `research.regime_scanner.data_loader.load_symbol_candles`
- `research.regime_scanner.pullback_entry_c3_5.enrich_indicators` / `attach_structure_edges` / `asof_htf_context`
- `research.regime_scanner.market_structure_c3_4b.apply_protected_structure`
- `timeframes.aggregate_candles` (30m) and `aggregate_complete_from_5m` (4h)

Forecast labels such as `BULLISH_EXTERNAL_BOS_AFTER_PULLBACK` are **adapters** over rising edges of existing flags (`external_bos_up` after a pullback/CHOCH prior state). They do not invent a second market structure.

## Run

```bash
PYTHONPATH=. python -m research.trend_forecast_validation.run_apt_forecast_validation \
  --coin APTUSDT \
  --timeframe 5m \
  --data-source mysql \
  --warmup-start 2026-01-01 \
  --development-start 2026-04-01 \
  --development-end 2026-05-31 \
  --oos-start 2026-06-01 \
  --output-dir research/trend_forecast_validation/results/aptusdt_forecast_validation_20260721
```

If MySQL is unavailable the loader falls back to feather and records that in `data_quality.json`.

## Tests

```bash
PYTHONPATH=. python -m pytest research/trend_forecast_validation/tests -q
```

## Causality rules (summary)

1. Scanner sees only candles ≤ t at decision t  
2. Forecast stored at close of t  
3. Outcomes evaluate from t+1 only  
4. HTF bars only when fully closed  
5. Warm-up signals excluded from DEV/OOS stats  
6. OOS is evaluation-only (no parameter search)
