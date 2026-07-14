# Phase E Results — Momentum-Bestätigung & Forward-Pfad

## Kein Entry / kein PnL

Phase E prüft nur, ob nach der Phase-D-Decision eine kausale Momentum-
Bestätigung innerhalb M2/M3 auftritt, und misst den **strikt nachgelagerten**
Forward-Pfad. Keine Order, kein TP/SL, keine Gebühren, kein PnL.

## Timing

- Decision = Close von `signal_index + decision_offset`
- Erste Momentum-Candle = Decision + 1 (= Scanner age 0)
- `break_close` wird nach age0 auf Decision-Close gesetzt
- Forward startet bei `confirming_candle_index + 1`

## Kandidaten

Primär: R2 / loose / offset 6

Vergleich: R2/loose/1, R2/loose/3, R3/loose/6, R4/loose/6, R5/loose/6

R1 ausgeschlossen.

## Phase F

recommended_candidate_for_phase_f = keine Empfehlung (null) — Gates nicht erfüllt

phase_e_ready_for_phase_f = **False**
leakage_checks_passed = **True**
Hash: `20e3d787df4e3d1cd9207e064be25838ea2e6b1771ede2f0cd4104a4971d39d4`

Keine Trading-Edge-Aussage. Keine Scanner-Integration.

