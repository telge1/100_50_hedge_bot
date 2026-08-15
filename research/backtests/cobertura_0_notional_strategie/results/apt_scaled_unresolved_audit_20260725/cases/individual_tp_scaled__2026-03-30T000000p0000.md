# Unresolved Audit — `individual_tp_scaled__2026-03-30T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-03-30T00:00:00+00:00`
- start_price: `0.9213`
- long/short qty: `705.811` / `705.811`
- long/short avg: `0.990025413683295` / `0.9499165096013613`
- locked_loss: `28.309305698973695`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-02T02:10:00+00:00` **overlay_short_add** qty=282.324 px=0.866 fee=0.1344709212 overlay_after=282.324 econ=-28.85404516155371
2. `2026-04-02T05:05:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8563000000000001 fee=0.06648236133 overlay_after=423.486 econ=-28.02870270350372
3. `2026-04-02T05:05:00+00:00` **overlay_short_add** qty=282.324 px=0.8568 fee=0.13304236176000003 overlay_after=423.486 econ=-28.02870270350372
4. `2026-04-02T15:45:00+00:00` **overlay_tp_partial** qty=70.581 px=0.8477 fee=0.032907332535 overlay_after=494.067 econ=-23.693179363503766
5. `2026-04-02T15:45:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8472000000000001 fee=0.06577584552000001 overlay_after=494.067 econ=-23.693179363503766
6. `2026-04-02T15:45:00+00:00` **overlay_short_add** qty=282.324 px=0.8476 fee=0.13161380232000003 overlay_after=494.067 econ=-23.693179363503766
7. `2026-04-04T02:20:00+00:00` **overlay_tp_close** qty=70.581 px=0.8390000000000001 fee=0.032569602450000006 overlay_after=494.067 econ=-19.76625719905374
8. `2026-04-04T02:20:00+00:00` **overlay_tp_partial** qty=70.581 px=0.8387 fee=0.032557956585 overlay_after=494.067 econ=-19.76625719905374
9. `2026-04-04T02:20:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8381000000000001 fee=0.06506932971000001 overlay_after=494.067 econ=-19.76625719905374
10. `2026-04-04T02:20:00+00:00` **overlay_short_add** qty=282.324 px=0.8384 fee=0.13018524288000002 overlay_after=494.067 econ=-19.76625719905374
11. `2026-04-05T06:10:00+00:00` **overlay_tp_close** qty=70.581 px=0.8301000000000001 fee=0.032224108455000004 overlay_after=494.067 econ=-15.341285147563788
12. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=70.581 px=0.8297 fee=0.032208580635000005 overlay_after=494.067 econ=-15.341285147563788
13. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8290000000000001 fee=0.06436281390000001 overlay_after=494.067 econ=-15.341285147563788
14. `2026-04-05T06:10:00+00:00` **overlay_short_add** qty=282.324 px=0.8292 fee=0.12875668344000002 overlay_after=494.067 econ=-15.341285147563788
15. `2026-04-07T12:05:00+00:00` **overlay_tp_close** qty=70.581 px=0.8212 fee=0.03187861446 overlay_after=494.067 econ=-12.16529012426875
16. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=70.581 px=0.8207 fee=0.031859204685 overlay_after=494.067 econ=-12.16529012426875
17. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8200000000000001 fee=0.06366406200000002 overlay_after=494.067 econ=-12.16529012426875
18. `2026-04-07T12:05:00+00:00` **overlay_short_add** qty=282.324 px=0.8200000000000001 fee=0.12732812400000004 overlay_after=494.067 econ=-12.16529012426875
19. `2026-04-09T01:05:00+00:00` **overlay_tp_close** qty=70.581 px=0.8123 fee=0.031533120465 overlay_after=423.486 econ=-7.645851404543736
20. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=70.581 px=0.8117000000000001 fee=0.031509828735 overlay_after=494.067 econ=-5.4002367737287615
21. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=141.162 px=0.8109000000000001 fee=0.06295754619 overlay_after=494.067 econ=-5.4002367737287615
22. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=282.324 px=0.8107000000000001 fee=0.12588403674000004 overlay_after=494.067 econ=-5.4002367737287615

## 4–6. Extremwerte

- max_overlay_qty: `494.067`
- max_overlay_notional: `610.9632521999999`
- best_economics: `-5.053656183463785` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-217.21048893853873`
- max_drawdown: `212.15683275507496`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-71.84468707881874`
- distance_to_be_end: `72.09468707881874`
- overlay_qty: `494.067`
- net_exposure: `-494.0670000000001`

## 8. Offene Tranchen

- `R1-T5` rem=70.581 entry=0.8292 tp_fills=141.162/70.581/0.0 status=partial
- `R1-T6` rem=141.162 entry=0.8200000000000001 tp_fills=141.162/0.0/0.0 status=partial
- `R1-T7` rem=282.324 entry=0.8107000000000001 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, OVERLAY_SATURATED, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.1261261261261261`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=66.05555555555556 econ=2.6001852379411625
- 120d: recovered=True status=RECOVERED_BE days=66.05555555555556 econ=2.6001852379411625
- full_remaining: recovered=True status=RECOVERED_BE days=66.05555555555556 econ=2.6001852379411625

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-03-30T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
