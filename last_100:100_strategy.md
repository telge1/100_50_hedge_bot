## Letzte 100:100 Strategie Übersicht

### Ziele
- Starte nach dem Gleichgewicht mit `Long 1450 @ 98.5038` / `Short 1250 @ 97.5801` (≈1,33 % Spread).
- Reagiere nur auf starke Bewegungen (≥ 1 %), um Noise-Trading zu vermeiden.
- Spiegle die Logik für Auf- und Abwärtsbewegungen, damit der Hedge kontrolliert „fließt“.

### Grundlogik
| Triggertyp | Wann | Aktion | Anmerkungen |
| --- | --- | --- | --- |
| Marktstruktur (Breakout) | Preis ≥ 1 % über dem letzten relevanten Hoch | Schließe 10 % der aktuellen Short-Size | Optional: Nur wenn Ratio < 1,2 einen kleinen Long (2‑3 %) ergänzen, um Spread zu stabilisieren. |
| Marktstruktur (Pullback) | Preis fällt ≥ 1 % unter dem letzten Hoch | Baue 5 % zusätzliche Shorts auf | Nutze einen fixen Prozentsatz der Basis-Short-Size (z. B. 1250); nur echte Bewegungen triggern. |
| Marktstruktur (Rebound-Tief) | Preis steigt ≥ 1 % über das letzte relevante Tief | Baue 5 % Long auf | Spiegelbild zur Short-Logik; reagiert nur, wenn der Pullback bestätigt war. |
| Marktstruktur (Down-Move) | Preis ≤ 1 % unter dem letzten relevanten Tief | Schließe 10 % Longs | Optional: Kleiner Short, wenn Ratio die Long-Herrschaft korrigieren soll. |
| Inventory-Guard | Long Avg / Short Avg / Spread / Ratio | Entscheidet ob Zusatzaktionen erlaubt sind | Guard arbeitet nur als Filter, nicht als primärer Trigger; steuert „wie viel“ geschlossen oder geladen wird.

### Rebound-Guard
- Der „Rebound Guard“ koppelt Short-Closes/Adds strikt an den `price_path`: echte Up-Moves > 1 % erlauben Short-Closes, echte Pullbacks Short-Adds.
- Auf der Long-Seite bedeutet das: Longs schließen bei echten Down-Moves und werden erst nach einem bestätigten Tief/Rebound wieder aufgebaut.

### Trigger-Management
- **Last relevant High/Low**: Der „strukturell bestätigte Pivot“ ist ein Event-basiertes Extrem (höchster/tiefster Preis) nach dem letzten bestätigten Gegenereignis (Breakout, Pullback, Rebound). Nur dieser Wert erlaubt neue Trigger; Average-Werte lösen keine Aktionen aus.
- **Event-Kette**: Pullbacks oder Rebounds dürfen nur nach einem vorhergehenden Strukturereignis ausgelöst werden. Beispiel: erst Breakout → referenzielles Hoch setzen → erst dann löst der −1 %-Pullback das Short-Add aus.
- **Einmalige Trigger**: Pro Strukturereignis (Breakout / Pullback / Rebound) darf die jeweilige Aktion nur einmal feuern. Erst wenn ein neues Gegenereignis die Referenz aktualisiert, wird der nächste Trigger wieder freigegeben.
- **Prioritätsregeln**: Wenn sich mehrere Signale überlagern (z. B. Breakout und Guard), gilt: Hauptaktion (Close / Add) ausführen, evtl. anschließend Zusatzaktion. Es dürfen niemals Close und Add im selben Tick laufen. Guards (Ratio / Spread) überprüfen nur die Hauptaktion.

- `scripts/hedge_analysis.py` modelliert den gesamten Hedge-Aufbau (R1…R3, S1/S2, L4), die Bridge-Phasen + Rebound-vs-Clockwork sowie die CLI-Parameter (`--start-spread`, `--prices`); siehe direkt im Skript.
- Verwende `--start-spread`, um z. B. auf 2 % (Long 98.5038 / Short angepasst) umzuschalten, wenn du den Zustand mit 2 % Spread brauchst.
- Starte `python scripts/hedge_analysis.py --scenario <name> --start-spread 2` für den neuen Ausgangszustand und gib eigene Preisfolgen über `--prices` an.

### Nächste Schritte
- Kapsle die symmetrischen Up-/Down-Trigger direkt in die State-Maschine des Bots.
- Behalte den Ratio-Guard (Long-Add nur wenn Ratio < 1,2), damit der Hedge stets kontrolliert bleibt.
lliert bleibt.



