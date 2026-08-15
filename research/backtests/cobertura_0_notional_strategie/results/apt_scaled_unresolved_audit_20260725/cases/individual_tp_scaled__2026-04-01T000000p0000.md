# Unresolved Audit — `individual_tp_scaled__2026-04-01T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-04-01T00:00:00+00:00`
- start_price: `0.8918`
- long/short qty: `729.159` / `729.159`
- long/short avg: `0.9583248278766553` / `0.919500209771512`
- locked_loss: `28.309319712928154`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-04T02:20:00+00:00` **overlay_short_add** qty=291.664 px=0.8383 fee=0.13447606216000002 overlay_after=291.664 econ=-29.806641557108122
2. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=145.832 px=0.8289000000000001 fee=0.06648407964000001 overlay_after=437.496 econ=-27.365662531228157
3. `2026-04-05T06:10:00+00:00` **overlay_short_add** qty=291.664 px=0.8294 fee=0.13304836688000002 overlay_after=437.496 econ=-27.365662531228157
4. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=72.916 px=0.8206 fee=0.03290917828 overlay_after=510.412 econ=-24.631928679348167
5. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=145.832 px=0.8201 fee=0.06577825276 overlay_after=510.412 econ=-24.631928679348167
6. `2026-04-07T12:05:00+00:00` **overlay_short_add** qty=291.664 px=0.8205 fee=0.1316206716 overlay_after=510.412 econ=-24.631928679348167
7. `2026-04-09T01:05:00+00:00` **overlay_tp_close** qty=72.916 px=0.8122 fee=0.032572306360000004 overlay_after=437.496 econ=-19.955679777028166
8. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=72.916 px=0.8119000000000001 fee=0.032560275220000004 overlay_after=510.412 econ=-17.475527719708168
9. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=145.832 px=0.8113 fee=0.06507242588 overlay_after=510.412 econ=-17.475527719708168
10. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=291.664 px=0.8115 fee=0.1301769348 overlay_after=510.412 econ=-17.475527719708168

## 4–6. Extremwerte

- max_overlay_qty: `510.412`
- max_overlay_notional: `631.1754791999999`
- best_economics: `-17.117481358658203` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-236.29300380140813`
- max_drawdown: `219.17552244274992`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-83.1003107521582`
- distance_to_be_end: `83.3503107521582`
- overlay_qty: `510.412`
- net_exposure: `-510.4119999999999`

## 8. Offene Tranchen

- `R1-T2` rem=72.916 entry=0.8294 tp_fills=145.832/72.916/0.0 status=partial
- `R1-T3` rem=145.832 entry=0.8205 tp_fills=145.832/0.0/0.0 status=partial
- `R1-T4` rem=291.664 entry=0.8115 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.09721910742318905`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=64.07986111111111 econ=1.9066245564617388
- 120d: recovered=True status=RECOVERED_BE days=64.07986111111111 econ=1.9066245564617388
- full_remaining: recovered=True status=RECOVERED_BE days=64.07986111111111 econ=1.9066245564617388

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-04-01T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
