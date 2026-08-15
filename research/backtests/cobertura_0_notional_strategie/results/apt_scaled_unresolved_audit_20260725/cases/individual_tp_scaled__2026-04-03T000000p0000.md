# Unresolved Audit — `individual_tp_scaled__2026-04-03T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-04-03T00:00:00+00:00`
- start_price: `0.8583`
- long/short qty: `757.618` / `757.618`
- long/short avg: `0.922325857553861` / `0.8849596658969373`
- locked_loss: `28.3092993907352`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills

1. `2026-04-12T22:00:00+00:00` **overlay_short_add** qty=303.047 px=0.8068000000000001 fee=0.13447407578000004 overlay_after=303.047 econ=-29.858712476235166

## 4–6. Extremwerte

- max_overlay_qty: `303.047`
- max_overlay_notional: `374.7479202`
- best_economics: `-28.986837168135207` (2026-04-09T01:20:00+00:00)
- worst/adverse economics: `-159.93004288330513`
- max_drawdown: `130.94320571516994`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-70.35778028881518`
- distance_to_be_end: `70.60778028881518`
- overlay_qty: `303.047`
- net_exposure: `-303.0469999999999`

## 8. Offene Tranchen

- `R1-T1` rem=303.047 entry=0.8068000000000001 tp_fills=0.0/0.0/0.0 status=open

## 9. Warum BE nicht erreicht

- Ursachen: `TP_HARVEST_TOO_SLOW, V_REVERSAL`
- max_drop: `-0.061982989630665175`
- max_rally_from_low: `0.5380698049931685`
- overlay_grows_faster_than_tp: `True`

## 10. Extended horizons

- 90d: recovered=True status=RECOVERED_BE days=62.395833333333336 econ=0.7244414896348382
- 120d: recovered=True status=RECOVERED_BE days=62.395833333333336 econ=0.7244414896348382
- full_remaining: recovered=True status=RECOVERED_BE days=62.395833333333336 econ=0.7244414896348382

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-04-03T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
