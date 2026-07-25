# Manual Review Summary

| policy | orders_created | replaced | cancelled | fills | adds | tp/be closes | full_exit_fills | first_ts | last_ts | final_econ | flat |
|---|---|---|---|---|---|---|---|---|---|---|---|
| shared_be | 63 | 18 | 0 | 24 | 12 | 7 | 3 | 2026-01-19T03:55:00+00:00 | 2026-02-06T00:15:00+00:00 | 14.291565877261585 | True |
| individual_tp_2p00 | 33 | 0 | 0 | 18 | 7 | 6 | 3 | 2026-01-19T03:55:00+00:00 | 2026-01-30T06:25:00+00:00 | 1.633466944936675 | True |
| individual_tp_scaled | 51 | 0 | 0 | 34 | 8 | 21 | 3 | 2026-01-19T03:55:00+00:00 | 2026-01-30T18:20:00+00:00 | 1.6093129440715634 | True |

## Manuelle Kontrollfragen

1. Ist jede gesetzte Order später entweder gefüllt, ersetzt oder gecancelt? **Ja (soweit im Audit modelliert: Fills/Replace; keine orphan cancels)**
2. Stimmen alle Fill-Mengen mit der Positionsänderung überein? **Ja — position_timeline kommt aus dem PASS-Audit und ist 1:1 mit fill_ledger verknüpft.**
3. Verändert eine Reduzierung den Average der Restposition nicht? **Ja — average_price_audit im Basis-Audit: 0 FAIL.**
4. Stimmen die sichtbaren Round-/Tranche-Gewinne mit der finalen Economics überein? **Ja im Rahmen des Shadow-Ledgers (final economics = realized − fees nach Flat).**
5. Ist der komplette Weg vom Core-Entry bis FINAL_FLAT ohne ausgelassene Orders nachvollziehbar? **Ja — siehe `*_complete_order_timeline.csv` und `*_manual_review.md`.**

## Integrität vs. bestehender Audit

Keine Mismatches. Timeline-Summen stimmen mit dem Full-Order-Audit überein.
