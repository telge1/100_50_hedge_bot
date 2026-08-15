# Unresolved Audit — `individual_tp_scaled__2026-02-19T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-19T00:00:00+00:00`
- start_price: `0.8778`
- long/short qty: `740.788` / `740.788`
- long/short avg: `0.9432804820701144` / `0.9050653556149734`
- locked_loss: `28.30930709645101`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-02-22T13:05:00+00:00` **overlay_short_add** qty=296.315 px=0.8251000000000001 fee=0.13446922857500002 overlay_after=296.315 econ=-29.904636398391002
2. `2026-02-23T01:00:00+00:00` **overlay_tp_partial** qty=148.15800000000002 px=0.8159000000000001 fee=0.06648516171000002 overlay_after=1037.102 econ=-30.637402371636206
3. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=296.315 px=0.8164 fee=0.1330513613 overlay_after=1037.102 econ=-30.637402371636206
4. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=296.315 px=0.8076 fee=0.1316171967 overlay_after=1037.102 econ=-30.637402371636206
5. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=296.315 px=0.7988000000000001 fee=0.1301830321 overlay_after=1037.102 econ=-30.637402371636206
6. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=74.07900000000001 px=0.8076 fee=0.032904410220000006 overlay_after=814.864 econ=-17.665965080576154
7. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=148.15800000000002 px=0.8073 fee=0.06578437437000001 overlay_after=814.864 econ=-17.665965080576154
8. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=148.15800000000002 px=0.7986000000000001 fee=0.06507543834000001 overlay_after=814.864 econ=-17.665965080576154
9. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=148.15800000000002 px=0.7899 fee=0.06436650231000002 overlay_after=814.864 econ=-17.665965080576154
10. `2026-02-23T01:05:00+00:00` **overlay_short_add** qty=296.315 px=0.79 fee=0.1287488675 overlay_after=814.864 econ=-17.665965080576154
11. `2026-02-23T01:10:00+00:00` **overlay_tp_close** qty=74.07799999999997 px=0.7994 fee=0.03256987425999999 overlay_after=592.6280000000002 econ=-17.732506774616173
12. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=74.07900000000001 px=0.7991 fee=0.03255809089500001 overlay_after=592.6280000000002 econ=-17.732506774616173
13. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=74.07900000000001 px=0.7905000000000001 fee=0.03220769722500001 overlay_after=592.6280000000002 econ=-17.732506774616173
14. `2026-02-23T01:20:00+00:00` **overlay_tp_close** qty=74.07799999999997 px=0.791 fee=0.0322276339 overlay_after=518.5500000000002 econ=-15.28323089601619

## 4–6. Extremwerte

- max_overlay_qty: `1037.102`
- max_overlay_notional: `842.1268240000002`
- best_economics: `-15.28323089601619` (2026-02-23T01:20:00+00:00)
- worst/adverse economics: `-186.6118587442262`
- max_drawdown: `171.32862784821`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-77.84909923973618`
- distance_to_be_end: `78.09909923973618`
- overlay_qty: `518.5500000000002`
- net_exposure: `-518.5500000000002`

## 8. Offene Tranchen

- `R1-T3` rem=74.07799999999997 entry=0.8076 tp_fills=148.15800000000002/74.07900000000001/0.0 status=partial
- `R1-T4` rem=148.15699999999998 entry=0.7988000000000001 tp_fills=148.15800000000002/0.0/0.0 status=partial
- `R1-T5` rem=296.315 entry=0.79 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.10230120756436545`
- max_rally_from_low: `0.4266497461928935`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-87.6704855495062
- 120d: recovered=True status=RECOVERED_BE days=105.08680555555556 econ=4.528324706078816
- full_remaining: recovered=True status=RECOVERED_BE days=105.08680555555556 econ=4.528324706078816

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-19T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
