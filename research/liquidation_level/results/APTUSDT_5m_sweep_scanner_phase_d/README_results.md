# Phase D Results — Kausale Pfad-Klassifikation

## Sweep ist weiterhin kein Entry

Phase D klassifiziert nur die *sichtbare* Anschlussentwicklung nach dem
oberen 50x-Sweep. Es gibt keine Entry-Simulation, kein TP/SL, keine Gebühren
und keinen PnL.

## Was unterscheiden die Klassen?

- **SHORT_REVERSAL**: Der Sweep wird zurückgewiesen; sichtbare Daten
  sprechen überwiegend für fallende Fortsetzung.
- **BULLISH_BREAKOUT_CONTINUATION**: Preis akzeptiert oberhalb des Levels;
  sichtbare Daten sprechen für bullische Fortsetzung.
- **UNCLEAR**: widersprüchlich, schwach oder unzureichend (bewusst häufig).
- **TECHNICAL_INVALID**: nur bei fehlenden/beschädigten Pflichtdaten,
  nicht wegen „ungünstiger“ Preisbewegung.

## Timeframes

- 5m-Pfad bis Decision-Offset (1/3/6/12 Folgcandles)
- zuletzt kausal geschlossener 15m- und 30m-Zustand am Decision-Zeitpunkt
- PRE/SWEEP-Kontext aus Phase C (frozen), keine END-Features späterer Fenster

## Score-Komponenten

Getrennt und gewichtet in `config.json`:

- level_response, trend_5m, structure_5m, volatility_5m, volume_5m
- context_15m, structure_15m, context_30m, structure_30m
- blocker_score (HTF-/Akzeptanz-Blocker)

Vorzeichen: negativ = Short/Reversal-Unterstützung, positiv = Bull-Breakout.

## Warum UNCLEAR wichtig ist

Lieber keine Richtungsaussage als eine erzwungene. Coverage unter 100 % ist
ein Feature, kein Fehler.

## Stabilität / Phase E

recommended_rule_for_phase_e = {"rule_family": "R2", "variant": "loose", "decision_offset": 6, "coverage_pct": 52.85608308605341, "short_precision_new_low_is": 1.0, "short_precision_new_low_oos": 1.0, "bull_precision_new_high_is": 0.7683823529411765, "bull_precision_new_high_oos": 0.8192090395480226, "gate_passed": "short_new_low", "selection_basis": "predefined_gates_not_oos_search"}

phase_d_ready_for_phase_e = **True**
leakage_checks_passed = **True**
Hash: `cf301399bde97d95d81016ba14ca0a52471beaa6514a70d9ee241833bec42a2a`

Keine Trading-Edge- oder PnL-Aussage. Keine Scanner-Integration.

