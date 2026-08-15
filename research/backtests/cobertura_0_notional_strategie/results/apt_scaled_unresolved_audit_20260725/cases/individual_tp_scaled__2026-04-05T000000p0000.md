# Unresolved Audit — `individual_tp_scaled__2026-04-05T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-04-05T00:00:00+00:00`
- start_price: `0.8494`
- long/short qty: `765.557` / `765.557`
- long/short avg: `0.9127619520054172` / `0.8757832228974235`
- locked_loss: `28.309324919728343`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills


## 4–6. Extremwerte

- max_overlay_qty: `0.0`
- max_overlay_notional: `0.0`
- best_economics: `-28.98840460100834` (2026-06-03T20:55:00+00:00)
- worst/adverse economics: `-29.350681484548367`
- max_drawdown: `0.3608452919500209`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-28.996152037848347`
- distance_to_be_end: `29.246152037848347`
- overlay_qty: `0.0`
- net_exposure: `0.0`

## 8. Offene Tranchen

- keine offenen Tranchen (oder alle flat in book)

## 9. Warum BE nicht erreicht

- Ursachen: `INSUFFICIENT_REBOUND`
- max_drop: `-0.05556863668471865`
- max_rally_from_low: `0.04263275991024683`
- overlay_grows_faster_than_tp: `False`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=60.458333333333336 econ=1.287424677526797
- 120d: recovered=True status=RECOVERED_BE days=60.458333333333336 econ=1.287424677526797
- full_remaining: recovered=True status=RECOVERED_BE days=60.458333333333336 econ=1.287424677526797

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-04-05T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
