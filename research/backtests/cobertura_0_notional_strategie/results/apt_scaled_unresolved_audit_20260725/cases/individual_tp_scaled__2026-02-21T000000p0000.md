# Unresolved Audit — `individual_tp_scaled__2026-02-21T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-21T00:00:00+00:00`
- start_price: `0.8826`
- long/short qty: `736.759` / `736.759`
- long/short avg: `0.9484385434894999` / `0.9100144484686438`
- locked_loss: `28.30929782347091`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-02-22T13:00:00+00:00` **overlay_short_add** qty=294.704 px=0.8296 fee=0.13446754112 overlay_after=294.704 econ=-29.250569698750912
2. `2026-02-23T01:00:00+00:00` **overlay_tp_partial** qty=147.352 px=0.8203 fee=0.06648006508000001 overlay_after=1326.168 econ=-31.53666747943086
3. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=294.704 px=0.8208000000000001 fee=0.13304117376000002 overlay_after=1326.168 econ=-31.53666747943086
4. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=294.704 px=0.812 fee=0.1316148064 overlay_after=1326.168 econ=-31.53666747943086
5. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=294.704 px=0.8032 fee=0.13018843904000002 overlay_after=1326.168 econ=-31.53666747943086
6. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=294.704 px=0.7943 fee=0.12874586296 overlay_after=1326.168 econ=-31.53666747943086
7. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=73.676 px=0.8121 fee=0.03290775378000001 overlay_after=810.4359999999999 econ=-14.126196158780788
8. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=147.352 px=0.8116 fee=0.06577498576 overlay_after=810.4359999999999 econ=-14.126196158780788
9. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=147.352 px=0.8029000000000001 fee=0.06506990644 overlay_after=810.4359999999999 econ=-14.126196158780788
10. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=147.352 px=0.7942 fee=0.06436482712000001 overlay_after=810.4359999999999 econ=-14.126196158780788
11. `2026-02-23T01:10:00+00:00` **overlay_tp_close** qty=73.676 px=0.8038000000000001 fee=0.03257142284000001 overlay_after=589.4079999999998 econ=-15.150693724820831
12. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=73.676 px=0.8034 fee=0.03255521412 overlay_after=589.4079999999998 econ=-15.150693724820831
13. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=73.676 px=0.7948000000000001 fee=0.03220672664 overlay_after=589.4079999999998 econ=-15.150693724820831
14. `2026-02-23T01:15:00+00:00` **overlay_tp_close** qty=73.676 px=0.7952 fee=0.03222293536 overlay_after=515.7319999999997 econ=-13.489478592640838

## 4–6. Extremwerte

- max_overlay_qty: `1326.168`
- max_overlay_notional: `1076.848416`
- best_economics: `-13.024335113890833` (2026-02-23T01:20:00+00:00)
- worst/adverse economics: `-183.42189616264073`
- max_drawdown: `170.3975610487499`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-88.58430910639079`
- distance_to_be_end: `88.83430910639079`
- overlay_qty: `515.7319999999997`
- net_exposure: `-515.7319999999997`

## 8. Offene Tranchen

- `R1-T3` rem=73.676 entry=0.812 tp_fills=147.352/73.676/0.0 status=partial
- `R1-T4` rem=147.352 entry=0.8032 tp_fills=147.352/0.0/0.0 status=partial
- `R1-T5` rem=294.704 entry=0.7943 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.10718332200317246`
- max_rally_from_low: `0.4266497461928935`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-106.05303086389075
- 120d: recovered=True status=RECOVERED_BE days=103.08680555555556 econ=7.548379706139212
- full_remaining: recovered=True status=RECOVERED_BE days=103.08680555555556 econ=7.548379706139212

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-21T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
