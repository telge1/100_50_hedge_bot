# Unresolved Audit — `individual_tp_scaled__2026-02-23T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-23T00:00:00+00:00`
- start_price: `0.8403`
- long/short qty: `773.847` / `773.847`
- long/short avg: `0.9029831272311656` / `0.8664005676956734`
- locked_loss: `28.309303948862006`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-02-23T01:05:00+00:00` **overlay_short_add** qty=309.539 px=0.7899 fee=0.134477670855 overlay_after=309.539 econ=-30.98978588804199

## 4–6. Extremwerte

- max_overlay_qty: `309.539`
- max_overlay_notional: `346.96226509999997`
- best_economics: `-29.000504089262` (2026-02-23T01:00:00+00:00)
- worst/adverse economics: `-132.046165478052`
- max_drawdown: `103.04566138879`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-79.96517329229701`
- distance_to_be_end: `80.21517329229701`
- overlay_qty: `309.539`
- net_exposure: `-309.539`

## 8. Offene Tranchen

- `R1-T1` rem=309.539 entry=0.7899 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, V_REVERSAL`
- max_drop: `-0.062239676306081175`
- max_rally_from_low: `0.4266497461928935`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-84.68569256017702
- 120d: recovered=True status=RECOVERED_BE days=102.12152777777777 econ=0.9790731851829755
- full_remaining: recovered=True status=RECOVERED_BE days=102.12152777777777 econ=0.9790731851829755

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-23T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
