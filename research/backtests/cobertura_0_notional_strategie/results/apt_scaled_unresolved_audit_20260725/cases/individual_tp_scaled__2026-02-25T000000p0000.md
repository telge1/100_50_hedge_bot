# Unresolved Audit — `individual_tp_scaled__2026-02-25T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-25T00:00:00+00:00`
- start_price: `0.8228`
- long/short qty: `790.306` / `790.306`
- long/short avg: `0.8841776949729894` / `0.848357`
- locked_loss: `28.309310161323317`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills


## 4–6. Extremwerte

- max_overlay_qty: `0.0`
- max_overlay_notional: `0.0`
- best_economics: `-29.011821067783313` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-29.28374955626331`
- max_drawdown: `0.26054017901999416`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-29.149263184243324`
- distance_to_be_end: `29.399263184243324`
- overlay_qty: `0.0`
- net_exposure: `0.0`

## 8. Offene Tranchen

- keine offenen Tranchen (oder alle flat in book)

## 9. Warum BE nicht erreicht

- Ursachen: `OTHER`
- max_drop: `-0.02151191054934363`
- max_rally_from_low: `0.26270028567879755`
- overlay_grows_faster_than_tp: `False`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-29.14882851594331
- 120d: recovered=True status=RECOVERED_BE days=100.25347222222223 econ=2.0176297943514143
- full_remaining: recovered=True status=RECOVERED_BE days=100.25347222222223 econ=2.0176297943514143

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-25T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
