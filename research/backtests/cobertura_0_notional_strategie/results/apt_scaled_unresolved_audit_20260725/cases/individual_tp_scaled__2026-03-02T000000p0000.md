# Unresolved Audit — `individual_tp_scaled__2026-03-02T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-03-02T00:00:00+00:00`
- start_price: `0.9274`
- long/short qty: `701.169` / `701.169`
- long/short avg: `0.9965804500704307` / `0.9562059817695674`
- locked_loss: `28.30932556404804`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-03-31T15:35:00+00:00` **overlay_short_add** qty=280.468 px=0.8718 fee=0.13448160132000003 overlay_after=280.468 econ=-29.22255645997804
2. `2026-04-02T02:15:00+00:00` **overlay_short_add** qty=280.468 px=0.8625 fee=0.13304700750000004 overlay_after=560.936 econ=-28.866885179067985
3. `2026-04-02T02:40:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8621000000000001 fee=0.06649265227000001 overlay_after=420.702 econ=-26.29548935153799
4. `2026-04-02T12:50:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8534 fee=0.032910816290000006 overlay_after=490.81899999999996 econ=-23.894804931682955
5. `2026-04-02T12:50:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8529 fee=0.06578306823 overlay_after=490.81899999999996 econ=-23.894804931682955
6. `2026-04-02T12:50:00+00:00` **overlay_short_add** qty=280.468 px=0.8532000000000001 fee=0.13161241368 overlay_after=490.81899999999996 econ=-23.894804931682955
7. `2026-04-02T16:00:00+00:00` **overlay_tp_close** qty=70.117 px=0.8447 fee=0.032575306445 overlay_after=490.81899999999996 econ=-18.800822423032947
8. `2026-04-02T16:00:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8443 fee=0.032559880705 overlay_after=490.81899999999996 econ=-18.800822423032947
9. `2026-04-02T16:00:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8437 fee=0.06507348419000002 overlay_after=490.81899999999996 econ=-18.800822423032947
10. `2026-04-02T16:00:00+00:00` **overlay_short_add** qty=280.468 px=0.8439000000000001 fee=0.13017781986000004 overlay_after=490.81899999999996 econ=-18.800822423032947
11. `2026-04-05T06:00:00+00:00` **overlay_tp_close** qty=70.117 px=0.8356 fee=0.032224370860000004 overlay_after=490.81899999999996 econ=-15.046055329237976
12. `2026-04-05T06:00:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8352 fee=0.032208945120000006 overlay_after=490.81899999999996 econ=-15.046055329237976
13. `2026-04-05T06:00:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8345 fee=0.06436390015000001 overlay_after=490.81899999999996 econ=-15.046055329237976
14. `2026-04-05T06:00:00+00:00` **overlay_short_add** qty=280.468 px=0.8347 fee=0.12875865178 overlay_after=490.81899999999996 econ=-15.046055329237976
15. `2026-04-05T12:40:00+00:00` **overlay_tp_close** qty=70.117 px=0.8266 fee=0.031877291710000005 overlay_after=490.81899999999996 econ=-11.597489173892978
16. `2026-04-05T12:40:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8261000000000001 fee=0.03185800953500001 overlay_after=490.81899999999996 econ=-11.597489173892978
17. `2026-04-05T12:40:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8254 fee=0.06366202898000001 overlay_after=490.81899999999996 econ=-11.597489173892978
18. `2026-04-05T12:40:00+00:00` **overlay_short_add** qty=280.468 px=0.8254 fee=0.12732405796000001 overlay_after=490.81899999999996 econ=-11.597489173892978
19. `2026-04-07T16:15:00+00:00` **overlay_tp_close** qty=70.117 px=0.8176 fee=0.03153021256000001 overlay_after=350.5849999999999 econ=-6.707103293312989
20. `2026-04-07T16:15:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8170000000000001 fee=0.031507073950000006 overlay_after=350.5849999999999 econ=-6.707103293312989
21. `2026-04-09T01:05:00+00:00` **overlay_tp_partial** qty=140.234 px=0.8162 fee=0.06295244494 overlay_after=490.81899999999996 econ=-5.637489496933058
22. `2026-04-09T01:05:00+00:00` **overlay_short_add** qty=280.468 px=0.8161 fee=0.12588946414000002 overlay_after=490.81899999999996 econ=-5.637489496933058
23. `2026-04-12T22:00:00+00:00` **overlay_tp_close** qty=70.117 px=0.8087000000000001 fee=0.03118698984500001 overlay_after=490.81899999999996 econ=-3.272138785533052
24. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=70.117 px=0.8079000000000001 fee=0.031156138365000006 overlay_after=490.81899999999996 econ=-3.272138785533052
25. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=140.234 px=0.807 fee=0.06224286090000001 overlay_after=490.81899999999996 econ=-3.272138785533052
26. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=280.468 px=0.8068000000000001 fee=0.12445487032000004 overlay_after=490.81899999999996 econ=-3.272138785533052

## 4–6. Extremwerte

- max_overlay_qty: `560.936`
- max_overlay_notional: `508.5866478`
- best_economics: `-2.92783662008809` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-115.12115653152307`
- max_drawdown: `112.19331991143498`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-95.54511912479303`
- distance_to_be_end: `95.79511912479303`
- overlay_qty: `490.81899999999996`
- net_exposure: `-490.81899999999985`

## 8. Offene Tranchen

- `R1-T6` rem=70.117 entry=0.8254 tp_fills=140.234/70.117/0.0 status=partial
- `R1-T7` rem=140.234 entry=0.8161 tp_fills=140.234/0.0/0.0 status=partial
- `R1-T8` rem=280.468 entry=0.8068000000000001 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, OVERLAY_SATURATED, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.1318740565020487`
- max_rally_from_low: `0.2882871692957395`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-66.37780710923809
- 120d: recovered=True status=RECOVERED_BE days=94.02083333333333 econ=2.019367869281899
- full_remaining: recovered=True status=RECOVERED_BE days=94.02083333333333 econ=2.019367869281899

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-03-02T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
