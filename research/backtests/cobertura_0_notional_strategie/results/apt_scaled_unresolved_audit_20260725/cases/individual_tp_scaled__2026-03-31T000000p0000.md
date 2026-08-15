# Unresolved Audit — `individual_tp_scaled__2026-03-31T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-03-31T00:00:00+00:00`
- start_price: `0.8901`
- long/short qty: `730.551` / `730.551`
- long/short avg: `0.9564980144572897` / `0.9177474060525037`
- locked_loss: `28.30929572072478`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-05T05:50:00+00:00` **overlay_short_add** qty=292.22 px=0.8367 fee=0.13447526070000002 overlay_after=292.22 econ=-29.690399956644768
2. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=146.11 px=0.8274 fee=0.06649027770000002 overlay_after=438.33000000000004 econ=-28.079145623484784
3. `2026-04-05T06:10:00+00:00` **overlay_short_add** qty=292.22 px=0.8278000000000001 fee=0.13304484380000003 overlay_after=438.33000000000004 econ=-28.079145623484784
4. `2026-04-07T12:05:00+00:00` **overlay_tp_partial** qty=73.055 px=0.8190000000000001 fee=0.03290762475 overlay_after=657.4950000000001 econ=-26.172971177964822
5. `2026-04-07T12:05:00+00:00` **overlay_short_add** qty=292.22 px=0.8189000000000001 fee=0.13161442690000003 overlay_after=657.4950000000001 econ=-26.172971177964822
6. `2026-04-07T16:15:00+00:00` **overlay_tp_partial** qty=146.11 px=0.8186 fee=0.0657831053 overlay_after=511.3850000000001 econ=-22.601426187604787
7. `2026-04-12T22:00:00+00:00` **overlay_tp_close** qty=73.055 px=0.8106 fee=0.032570110650000005 overlay_after=511.3850000000001 econ=-18.286112987079846
8. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=73.055 px=0.8103 fee=0.03255805657500001 overlay_after=511.3850000000001 econ=-18.286112987079846
9. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=146.11 px=0.8098000000000001 fee=0.06507593290000001 overlay_after=511.3850000000001 econ=-18.286112987079846
10. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=292.22 px=0.81 fee=0.13018401000000004 overlay_after=511.3850000000001 econ=-18.286112987079846

## 4–6. Extremwerte

- max_overlay_qty: `657.4950000000001`
- max_overlay_notional: `632.3786910000001`
- best_economics: `-17.927384079584886` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-237.52072245330987`
- max_drawdown: `219.59333837372498`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-82.34484646832988`
- distance_to_be_end: `82.59484646832988`
- overlay_qty: `511.3850000000001`
- net_exposure: `-511.3850000000001`

## 8. Offene Tranchen

- `R1-T2` rem=73.055 entry=0.8278000000000001 tp_fills=146.11/73.055/0.0 status=partial
- `R1-T3` rem=146.11 entry=0.8189000000000001 tp_fills=146.11/0.0/0.0 status=partial
- `R1-T4` rem=292.22 entry=0.81 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.09549488821480728`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=65.07986111111111 econ=1.485773154605119
- 120d: recovered=True status=RECOVERED_BE days=65.07986111111111 econ=1.485773154605119
- full_remaining: recovered=True status=RECOVERED_BE days=65.07986111111111 econ=1.485773154605119

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-03-31T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
