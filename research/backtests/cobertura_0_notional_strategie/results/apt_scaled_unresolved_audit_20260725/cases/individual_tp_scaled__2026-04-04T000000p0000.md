# Unresolved Audit — `individual_tp_scaled__2026-04-04T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-04-04T00:00:00+00:00`
- start_price: `0.8475`
- long/short qty: `767.273` / `767.273`
- long/short avg: `0.9107202193602437` / `0.873824206976179`
- locked_loss: `28.30931410995848`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills


## 4–6. Extremwerte

- max_overlay_qty: `0.0`
- max_overlay_notional: `0.0`
- best_economics: `-28.991350752388477` (2026-04-12T22:25:00+00:00)
- worst/adverse economics: `-29.353004880938506`
- max_drawdown: `0.361654128550029`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-29.005867557548477`
- distance_to_be_end: `29.255867557548477`
- overlay_qty: `0.0`
- net_exposure: `0.0`

## 8. Offene Tranchen

- keine offenen Tranchen (oder alle flat in book)

## 9. Warum BE nicht erreicht

- Ursachen: `V_REVERSAL`
- max_drop: `-0.05002949852507373`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `False`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=62.114583333333336 econ=1.9612520076815847
- 120d: recovered=True status=RECOVERED_BE days=62.114583333333336 econ=1.9612520076815847
- full_remaining: recovered=True status=RECOVERED_BE days=62.114583333333336 econ=1.9612520076815847

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-04-04T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
