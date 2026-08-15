# Unresolved Audit — `individual_tp_scaled__2026-03-15T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-03-15T00:00:00+00:00`
- start_price: `0.9196`
- long/short qty: `707.116` / `707.116`
- long/short avg: `0.9881986002639294` / `0.948163705882353`
- locked_loss: `28.309314375522757`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-02T02:10:00+00:00` **overlay_short_add** qty=282.846 px=0.8644000000000001 fee=0.13447064532000003 overlay_after=282.846 econ=-29.307365722182748
2. `2026-04-02T10:35:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8548 fee=0.06648860922000001 overlay_after=424.269 econ=-26.93437404009274
3. `2026-04-02T10:35:00+00:00` **overlay_short_add** qty=282.846 px=0.8552000000000001 fee=0.13303944456000003 overlay_after=424.269 econ=-26.93437404009274
4. `2026-04-02T16:00:00+00:00` **overlay_tp_partial** qty=70.712 px=0.8461000000000001 fee=0.032906182760000006 overlay_after=494.98 econ=-22.02371257870773
5. `2026-04-02T16:00:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8457 fee=0.065780787105 overlay_after=494.98 econ=-22.02371257870773
6. `2026-04-02T16:00:00+00:00` **overlay_short_add** qty=282.846 px=0.8460000000000001 fee=0.13160824380000002 overlay_after=494.98 econ=-22.02371257870773
7. `2026-04-04T02:20:00+00:00` **overlay_tp_close** qty=70.711 px=0.8375 fee=0.032571254375000004 overlay_after=424.269 econ=-20.09658263977269
8. `2026-04-05T05:50:00+00:00` **overlay_tp_partial** qty=70.712 px=0.8371000000000001 fee=0.032556158360000004 overlay_after=494.98 econ=-19.598351333172708
9. `2026-04-05T05:50:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8366 fee=0.06507296499000001 overlay_after=494.98 econ=-19.598351333172708
10. `2026-04-05T05:50:00+00:00` **overlay_short_add** qty=282.846 px=0.8368 fee=0.13017704304000002 overlay_after=494.98 econ=-19.598351333172708
11. `2026-04-05T06:10:00+00:00` **overlay_tp_close** qty=70.711 px=0.8286 fee=0.03222512403 overlay_after=494.98 econ=-16.177989770157772
12. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=70.712 px=0.8281000000000001 fee=0.03220613396000001 overlay_after=494.98 econ=-16.177989770157772
13. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8275 fee=0.064365142875 overlay_after=494.98 econ=-16.177989770157772
14. `2026-04-05T06:10:00+00:00` **overlay_short_add** qty=282.846 px=0.8276 fee=0.12874584228 overlay_after=494.98 econ=-16.177989770157772
15. `2026-04-07T12:05:00+00:00` **overlay_tp_close** qty=70.711 px=0.8197 fee=0.031878993685 overlay_after=353.557 econ=-12.040569859872786
16. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=70.712 px=0.8191 fee=0.031856109560000005 overlay_after=353.557 econ=-12.040569859872786
17. `2026-04-07T16:15:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8184 fee=0.06365732076000001 overlay_after=494.98 econ=-10.224934683202742
18. `2026-04-07T16:15:00+00:00` **overlay_short_add** qty=282.846 px=0.8184 fee=0.12731464152000002 overlay_after=494.98 econ=-10.224934683202742
19. `2026-04-12T22:00:00+00:00` **overlay_tp_close** qty=70.711 px=0.8107000000000001 fee=0.03152897423500001 overlay_after=494.98 econ=-6.196381682602723
20. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=70.712 px=0.8101 fee=0.03150608516 overlay_after=494.98 econ=-6.196381682602723
21. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=141.423 px=0.8093 fee=0.062949498645 overlay_after=494.98 econ=-6.196381682602723
22. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=282.846 px=0.8092 fee=0.12588344076000002 overlay_after=494.98 econ=-6.196381682602723

## 4–6. Extremwerte

- max_overlay_qty: `494.98`
- max_overlay_notional: `612.092268`
- best_economics: `-5.849160635982776` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-218.39804417408277`
- max_drawdown: `212.5488835381`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-123.95391949344277`
- distance_to_be_end: `124.20391949344277`
- overlay_qty: `494.98`
- net_exposure: `-494.98`

## 8. Offene Tranchen

- `R1-T5` rem=70.711 entry=0.8276 tp_fills=141.423/70.712/0.0 status=partial
- `R1-T6` rem=141.423 entry=0.8184 tp_fills=141.423/0.0/0.0 status=partial
- `R1-T7` rem=282.846 entry=0.8092 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, OVERLAY_SATURATED, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.12451065680730745`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=81.05555555555556 econ=2.279136796962269
- 120d: recovered=True status=RECOVERED_BE days=81.05555555555556 econ=2.279136796962269
- full_remaining: recovered=True status=RECOVERED_BE days=81.05555555555556 econ=2.279136796962269

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-03-15T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
