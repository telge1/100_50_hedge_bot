# Unresolved Audit — `individual_tp_scaled__2026-03-13T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-03-13T00:00:00+00:00`
- start_price: `0.9161`
- long/short qty: `709.817` / `709.817`
- long/short avg: `0.9844375138122942` / `0.9445549923432184`
- locked_loss: `28.309291741614963`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-02T02:40:00+00:00` **overlay_short_add** qty=283.927 px=0.8611000000000001 fee=0.13446924683500003 overlay_after=283.927 econ=-29.279062391709928
2. `2026-04-02T12:50:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8515 fee=0.0664852903 overlay_after=425.89000000000004 econ=-28.006032920489915
3. `2026-04-02T12:50:00+00:00` **overlay_short_add** qty=283.927 px=0.8520000000000001 fee=0.13304819220000003 overlay_after=425.89000000000004 econ=-28.006032920489915
4. `2026-04-02T16:00:00+00:00` **overlay_tp_partial** qty=70.982 px=0.8429000000000001 fee=0.032906900290000006 overlay_after=354.908 econ=-23.253260668209897
5. `2026-04-03T18:25:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8425 fee=0.0657825685 overlay_after=496.87100000000004 econ=-23.153575489019882
6. `2026-04-03T18:25:00+00:00` **overlay_short_add** qty=283.927 px=0.8428 fee=0.13161152158000003 overlay_after=496.87100000000004 econ=-23.153575489019882
7. `2026-04-05T06:00:00+00:00` **overlay_tp_close** qty=70.98100000000002 px=0.8343 fee=0.032570696565000015 overlay_after=496.87100000000004 econ=-19.75061655925997
8. `2026-04-05T06:00:00+00:00` **overlay_tp_partial** qty=70.982 px=0.8340000000000001 fee=0.032559443400000006 overlay_after=496.87100000000004 econ=-19.75061655925997
9. `2026-04-05T06:00:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8334 fee=0.06507203868 overlay_after=496.87100000000004 econ=-19.75061655925997
10. `2026-04-05T06:00:00+00:00` **overlay_short_add** qty=283.927 px=0.8337 fee=0.13019046694500003 overlay_after=496.87100000000004 econ=-19.75061655925997
11. `2026-04-05T12:40:00+00:00` **overlay_tp_close** qty=70.98100000000002 px=0.8255 fee=0.03222714852500001 overlay_after=638.835 econ=-16.671169553679857
12. `2026-04-05T12:40:00+00:00` **overlay_tp_partial** qty=70.982 px=0.8250000000000001 fee=0.032208082500000006 overlay_after=638.835 econ=-16.671169553679857
13. `2026-04-05T12:40:00+00:00` **overlay_short_add** qty=283.927 px=0.8245 fee=0.12875379632500003 overlay_after=638.835 econ=-16.671169553679857
14. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8244 fee=0.06436931688 overlay_after=496.87100000000004 econ=-14.125349570489858
15. `2026-04-07T16:15:00+00:00` **overlay_tp_close** qty=70.98100000000002 px=0.8166 fee=0.03187969653000001 overlay_after=425.89 econ=-11.251745087629846
16. `2026-04-09T01:05:00+00:00` **overlay_tp_partial** qty=70.982 px=0.8161 fee=0.031860625610000005 overlay_after=496.87100000000004 econ=-10.147386021194878
17. `2026-04-09T01:05:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8153 fee=0.06365878706000001 overlay_after=496.87100000000004 econ=-10.147386021194878
18. `2026-04-09T01:05:00+00:00` **overlay_short_add** qty=283.927 px=0.8153 fee=0.127317125705 overlay_after=496.87100000000004 econ=-10.147386021194878
19. `2026-04-12T22:00:00+00:00` **overlay_tp_close** qty=70.98100000000002 px=0.8077000000000001 fee=0.03153224453500002 overlay_after=496.87100000000004 econ=-7.681661707799851
20. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=70.982 px=0.8071 fee=0.031509264710000005 overlay_after=496.87100000000004 econ=-7.681661707799851
21. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=141.964 px=0.8062 fee=0.06294825724000001 overlay_after=496.87100000000004 econ=-7.681661707799851
22. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=283.927 px=0.8062 fee=0.12589607107 overlay_after=496.87100000000004 econ=-7.681661707799851

## 4–6. Extremwerte

- max_overlay_qty: `638.835`
- max_overlay_notional: `614.4306786`
- best_economics: `-7.333114153374889` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-220.69400996924986`
- max_drawdown: `213.36089581587498`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-166.9181015722499`
- distance_to_be_end: `167.1681015722499`
- overlay_qty: `496.87100000000004`
- net_exposure: `-496.8710000000001`

## 8. Offene Tranchen

- `R1-T5` rem=70.98100000000002 entry=0.8245 tp_fills=141.964/70.982/0.0 status=partial
- `R1-T6` rem=141.96300000000002 entry=0.8153 tp_fills=141.964/0.0/0.0 status=partial
- `R1-T7` rem=283.927 entry=0.8062 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, OVERLAY_SATURATED, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.12116581159262088`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=83.05555555555556 econ=1.6652493648250952
- 120d: recovered=True status=RECOVERED_BE days=83.05555555555556 econ=1.6652493648250952
- full_remaining: recovered=True status=RECOVERED_BE days=83.05555555555556 econ=1.6652493648250952

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-03-13T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
