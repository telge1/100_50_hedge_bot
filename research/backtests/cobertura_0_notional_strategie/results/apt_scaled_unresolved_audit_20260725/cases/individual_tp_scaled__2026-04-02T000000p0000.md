# Unresolved Audit — `individual_tp_scaled__2026-04-02T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-04-02T00:00:00+00:00`
- start_price: `0.8897`
- long/short qty: `730.88` / `730.88`
- long/short avg: `0.9560681760056742` / `0.9173349816480312`
- locked_loss: `28.309317092114114`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-05T05:50:00+00:00` **overlay_short_add** qty=292.35200000000003 px=0.8363 fee=0.13447168768000003 overlay_after=292.35200000000003 econ=-29.807920752914086
2. `2026-04-05T06:10:00+00:00` **overlay_tp_partial** qty=146.17600000000002 px=0.8270000000000001 fee=0.06648815360000002 overlay_after=438.528 econ=-28.25431252507406
3. `2026-04-05T06:10:00+00:00` **overlay_short_add** qty=292.35200000000003 px=0.8274 fee=0.13304062464000002 overlay_after=438.528 econ=-28.25431252507406
4. `2026-04-07T16:15:00+00:00` **overlay_tp_partial** qty=73.08800000000001 px=0.8186 fee=0.03290641024000001 overlay_after=511.616 econ=-22.803241382674095
5. `2026-04-07T16:15:00+00:00` **overlay_tp_partial** qty=146.17600000000002 px=0.8182 fee=0.06578066176000001 overlay_after=511.616 econ=-22.803241382674095
6. `2026-04-07T16:15:00+00:00` **overlay_short_add** qty=292.35200000000003 px=0.8185 fee=0.1316095616 overlay_after=511.616 econ=-22.803241382674095
7. `2026-04-12T22:00:00+00:00` **overlay_tp_close** qty=73.08800000000001 px=0.8103 fee=0.032572763520000006 overlay_after=511.616 econ=-18.493163087634073
8. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=73.08800000000001 px=0.8099000000000001 fee=0.03255668416000001 overlay_after=511.616 econ=-18.493163087634073
9. `2026-04-12T22:00:00+00:00` **overlay_tp_partial** qty=146.17600000000002 px=0.8094 fee=0.06507316992 overlay_after=511.616 econ=-18.493163087634073
10. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=292.35200000000003 px=0.8096 fee=0.13017849856000002 overlay_after=511.616 econ=-18.493163087634073

## 4–6. Extremwerte

- max_overlay_qty: `511.616`
- max_overlay_notional: `632.6643455999999`
- best_economics: `-18.134272137874127` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-237.826803526674`
- max_drawdown: `219.6925313887999`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-91.75818697435409`
- distance_to_be_end: `92.00818697435409`
- overlay_qty: `511.616`
- net_exposure: `-511.6160000000001`

## 8. Offene Tranchen

- `R1-T2` rem=73.08800000000001 entry=0.8274 tp_fills=146.17600000000002/73.08800000000001/0.0 status=partial
- `R1-T3` rem=146.17600000000002 entry=0.8185 tp_fills=146.17600000000002/0.0/0.0 status=partial
- `R1-T4` rem=292.35200000000003 entry=0.8096 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.09508823198831066`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=63.079861111111114 econ=1.390548273005828
- 120d: recovered=True status=RECOVERED_BE days=63.079861111111114 econ=1.390548273005828
- full_remaining: recovered=True status=RECOVERED_BE days=63.079861111111114 econ=1.390548273005828

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-04-02T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
