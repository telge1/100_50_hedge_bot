# Unresolved Audit — `individual_tp_scaled__2026-02-20T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-20T00:00:00+00:00`
- start_price: `0.8659`
- long/short qty: `750.969` / `750.969`
- long/short avg: `0.9304927881345546` / `0.8927957295819154`
- locked_loss: `28.309322364216882`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=300.38800000000003 px=0.8139000000000001 fee=0.13446718626000004 overlay_after=901.1640000000001 econ=-35.84950784913682
2. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=300.38800000000003 px=0.8053 fee=0.13304635102000004 overlay_after=901.1640000000001 econ=-35.84950784913682
3. `2026-02-23T01:00:00+00:00` **overlay_short_add** qty=300.38800000000003 px=0.7966000000000001 fee=0.13160899444000004 overlay_after=901.1640000000001 econ=-35.84950784913682
4. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=150.19400000000002 px=0.8048000000000001 fee=0.06648187216000001 overlay_after=600.7760000000001 econ=-22.476287426156773
5. `2026-02-23T01:05:00+00:00` **overlay_tp_partial** qty=150.19400000000002 px=0.7963 fee=0.06577971521 overlay_after=600.7760000000001 econ=-22.476287426156773
6. `2026-02-23T01:10:00+00:00` **overlay_tp_partial** qty=75.09700000000001 px=0.7967000000000001 fee=0.03290637894500001 overlay_after=525.6790000000001 econ=-22.461094552306793
7. `2026-02-23T01:20:00+00:00` **overlay_tp_close** qty=75.09700000000001 px=0.7886000000000001 fee=0.032571821810000005 overlay_after=675.8730000000002 econ=-21.110124308826816
8. `2026-02-23T01:20:00+00:00` **overlay_tp_partial** qty=75.09700000000001 px=0.7883 fee=0.032559430805000004 overlay_after=675.8730000000002 econ=-21.110124308826816
9. `2026-02-23T01:20:00+00:00` **overlay_short_add** qty=300.38800000000003 px=0.788 fee=0.13018815920000004 overlay_after=675.8730000000002 econ=-21.110124308826816

## 4–6. Extremwerte

- max_overlay_qty: `901.1640000000001`
- max_overlay_notional: `757.5860457000002`
- best_economics: `-20.775353146386784` (2026-02-23T01:15:00+00:00)
- worst/adverse economics: `-244.3403657665119`
- max_drawdown: `223.56501262012512`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-119.35309681568184`
- distance_to_be_end: `119.60309681568184`
- overlay_qty: `675.8730000000002`
- net_exposure: `-675.873`

## 8. Offene Tranchen

- `R1-T2` rem=75.09700000000001 entry=0.8053 tp_fills=150.19400000000002/75.09700000000001/0.0 status=partial
- `R1-T3` rem=300.38800000000003 entry=0.7966000000000001 tp_fills=0.0/0.0/0.0 status=open
- `R1-T4` rem=300.38800000000003 entry=0.788 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, LARGE_OPEN_OVERLAY, V_REVERSAL`
- max_drop: `-0.0899641990992031`
- max_rally_from_low: `0.4266497461928935`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-129.23833043801181
- 120d: recovered=True status=RECOVERED_BE days=104.3888888888889 econ=0.7692807075434047
- full_remaining: recovered=True status=RECOVERED_BE days=104.3888888888889 econ=0.7692807075434047

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-20T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
