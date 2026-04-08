## 100 : 100 Emergency Transition Plan

### Ziel
Definiere klare Trigger für den Wechsel von Burn/Repair in den `100 : 100` Notfall-Hedge und wieder zurück, damit wir nur in echten Krisen umschalten.

### Switch into 100 : 100 (Notfall)
| Bedingung | Beschreibung |
|---|---|
| Spread ≥ 2.5 % *und* Markt fällt weiter | Die Hedge-Spread-Agenda ist aufgerissen; Burn/Repair kommt nicht mehr hinterher. |
| Spread-Anstieg > 1.2 × ATR (14) | Die Bewegung ist nicht nur groß, sondern auch schnell (beschleunigter Dump). |
| Ratio driftet deutlich (z. B. Short/Long < 0.4 oder > 0.6) | Die Balance ist weg, normale Strategie verliert die Kontrolle. |
| ≥ 2 Repair-Zyklen ohne deutliche Verbesserung | Hinweise auf endlose Heilversuche; besser auf Emergency umschalten. |
| Burn > 1.5 × typische Burn-Größe | zu viel Deleveraging auf einmal, Risiko steigt |

**Aktion:** Burn- und Repair-Bots pausieren, Hedge auf `100 : 100` einfrieren, Ping-Pong-Modus aktivieren (feste 20 $ Adds).

### Emergency-Trigger Monitoring
- ATR wird im Hintergrund getracked (z. B. 14-Period).  
- Spread wird mit den aktuellen Averages berechnet.  
- Wenn mehrere Bedingungen gleichzeitig erfüllt sind, wechselt der Controller unverzüglich in den Notfallmodus.

### Rückkehr in Burn/Repair
| Bedingung | Beschreibung |
|---|---|
| Spread ≤ 1.0 % | Die Spread-Heilung ist deutlich fortgeschritten. |
| Markt läuft nicht schneller als ATR (Momentum beruhigt) | Keine neue Beschleunigung mehr. |
| Short wurde stufenweise zurückgebaut | z. B. `100:80`, `100:60`, `100:50` (Bridge-Schritte abgeschlossen) |
| Keine weiteren Notfall-Adds erforderlich | Zusatz-Orders neutralisiert oder geschlossen |

**Aktion:** Ping-Pong-Modus beenden; zuerst Bridge-Phase (Short zurück auf 50 %), dann Repair-Bot wieder aktivieren. Burn bleibt passiv, bis wieder ein New-Low entsteht.

### Vereinfachte Ablauf-Checkliste
1. Stichwort „Spread ≥ 2.5 % + Momentum“ → Scan für Notfall.  
2. Rebound-Impulse werden ignoriert, solange Notfallziele aktiv sind.  
3. Schäden mit festen 20 $ Adds über Ping-Pong adressieren.  
4. Erst wenn Bridge-Schritte abgeschlossen sind, schreibe den Reset in die Burn/Repair-Queue.

### Anmerkung
Die neuen Regeln lassen sich als separate State-Machine realisieren:  
`NORMAL (Burn/Repair)` → `EMERGENCY (100:100)` → `BRIDGE` → `NORMAL`.





##################################


Wie du in der Praxis dahinkommst
Weil der Hedge eingefroren ist (100:100), passiert nichts automatisch. Deswegen brauchst du diese drei Bausteine:

Mini-Rebound‑Heilung

Wenn der Preis leicht zurückkommt (z. B. +0.75% bis +1.5%), öffnest du einen kleinen Zusatz-Long (z. B. 10–20 $).
Wenn der Rebound bestätigt ist, schließt du den Zusatz-Long mit Gewinn und nutzt ihn, um den Spread zu drücken.
Dadurch sinkt der Spread schon im eingefrorenen Zustand – er bewegt sich in Richtung 1.0%.
Short‑Schrittweise abbauen

Du reduzierst den Short in festen 20$-Blöcken (100:100 → 100:80 → 100:60).
Mit jedem Schritt sinkt der Spread automatisch, weil das Short-Avg nicht mehr so tief liegt.
ATR-gestützter Heilcheck

Du trackst den Spread nach jedem Zusatz-Trade (nach Short-Add/Rebalance).
Wenn der Spread unter 1.0% fällt, hast du das Kriterium erfüllt.
Solange er über 1.0% bleibt, machst du weiter mit 1 + 2.
Worst‑Case Absicherung
Wenn der Markt nach einem Schritt wieder fällt:

dreht die Bridge um (z. B. 100:80 → 100:100)
stoppt den Heal
wartet auf den nächsten Rebound
Fazit
Nein, du erreichst Spread ≤ 1.0% nicht automatisch, aber mit dieser gezielten Mini-Heilung (kleiner Zusatz-Long + stufenweiser Short-Abbau) kannst du den Spread langsam drücken, während der Hedge eingefroren bleibt. Wenn der Spread dann tatsächlich ≤ 1.0% liegt, kannst du zurück in Burn/Repair wechseln.

Wenn du willst, schreibe ich dir jetzt noch eine kleine Checkliste („Step 1: Zusatz-Long… Step 2: Spread messen…“), damit das genau in den Notfallmodus passt. Soll ich das noch ergänzen?




##########################################################################

Wie du in der Praxis dahinkommst
Weil der Hedge eingefroren ist (100:100), passiert nichts automatisch. Deswegen brauchst du diese drei Bausteine:

Mini-Rebound‑Heilung

Wenn der Preis leicht zurückkommt (z. B. +0.75% bis +1.5%), öffnest du einen kleinen Zusatz-Long (z. B. 10–20 $).
Wenn der Rebound bestätigt ist, schließt du den Zusatz-Long mit Gewinn und nutzt ihn, um den Spread zu drücken.
Dadurch sinkt der Spread schon im eingefrorenen Zustand – er bewegt sich in Richtung 1.0%.
Short‑Schrittweise abbauen

Du reduzierst den Short in festen 20$-Blöcken (100:100 → 100:80 → 100:60).
Mit jedem Schritt sinkt der Spread automatisch, weil das Short-Avg nicht mehr so tief liegt.
ATR-gestützter Heilcheck

Du trackst den Spread nach jedem Zusatz-Trade (nach Short-Add/Rebalance).
Wenn der Spread unter 1.0% fällt, hast du das Kriterium erfüllt.
Solange er über 1.0% bleibt, machst du weiter mit 1 + 2.
Worst‑Case Absicherung
Wenn der Markt nach einem Schritt wieder fällt:

dreht die Bridge um (z. B. 100:80 → 100:100)
stoppt den Heal
wartet auf den nächsten Rebound
Fazit
Nein, du erreichst Spread ≤ 1.0% nicht automatisch, aber mit dieser gezielten Mini-Heilung (kleiner Zusatz-Long + stufenweiser Short-Abbau) kannst du den Spread langsam drücken, während der Hedge eingefroren bleibt. Wenn der Spread dann tatsächlich ≤ 1.0% liegt, kannst du zurück in Burn/Repair wechseln.

Wenn du willst, schreibe ich dir jetzt noch eine kleine Checkliste („Step 1: Zusatz-Long… Step 2: Spread messen…“), damit das genau in den Notfallmodus passt. Soll ich das noch ergänzen?