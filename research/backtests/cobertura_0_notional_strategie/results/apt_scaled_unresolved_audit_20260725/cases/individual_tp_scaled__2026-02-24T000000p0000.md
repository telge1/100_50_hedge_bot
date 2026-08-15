# Unresolved Audit — `individual_tp_scaled__2026-02-24T000000p0000`

## 1. Startzustand

- start_timestamp: `2026-02-24T00:00:00+00:00`
- start_price: `0.8092`
- long/short qty: `803.588` / `803.588`
- long/short avg: `0.8695631876180641` / `0.834334570247934`
- locked_loss: `28.309294175228068`
- seeding_mode: `relative_notional_invariant`

## 2–3. Chronologische Adds / TP-Fills


## 4–6. Extremwerte

- max_overlay_qty: `0.0`
- max_overlay_notional: `0.0`
- best_economics: `-29.01379977482807` (2026-02-24T07:00:00+00:00)
- worst/adverse economics: `-29.300110143348075`
- max_drawdown: `0.2863103685200059`

## 7. Zustand nach 60 Tagen

- status: `DATA_END_OPEN`
- final_economics: `-29.181130904068063`
- distance_to_be_end: `29.431130904068063`
- overlay_qty: `0.0`
- net_exposure: `0.0`

## 8. Offene Tranchen

- keine offenen Tranchen (oder alle flat in book)

## 9. Warum BE nicht erreicht

- Ursachen: `OTHER`
- max_drop: `-0.015818091942659457`
- max_rally_from_low: `0.4116022099447515`
- overlay_grows_faster_than_tp: `False`

## 10. Extended horizons

- 90d: recovered=False status=DATA_END_OPEN days=90.00347222222223 econ=-29.144889085268076
- 120d: recovered=True status=RECOVERED_BE days=101.2638888888889 econ=2.8486868159270715
- full_remaining: recovered=True status=RECOVERED_BE days=101.2638888888889 econ=2.8486868159270715

## 11. Replay-CLI

```bash
python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \
  --run-id individual_tp_scaled__2026-02-24T000000p0000 \
  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725
```
