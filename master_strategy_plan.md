## Master Strategy Plan

### Ziel
Alle aktuellen Strategie-Ideen in einen klaren Umsetzungsplan bringen, damit wir die Architektur später Schritt für Schritt sauber bauen können.

---

## 1. Gesamtidee

Wir wollen nicht mehrere unabhängige Bots parallel gegeneinander arbeiten lassen.

Stattdessen bauen wir:

- einen gemeinsamen Master-Code
- einen gemeinsamen State-/Order-/Market-Unterbau
- mehrere klar getrennte Strategie-Module
- eine zentrale Zustandsmaschine, die entscheidet, welcher Modus aktiv ist

Wichtiger Grundsatz:

- immer nur **ein aktiver Modus gleichzeitig**

---

## 2. Ziel-Modi

### A. Burn-Modus
Ziel:

- Position kontrolliert verkleinern
- Notional abbauen
- nicht zu früh wieder aufblasen

### B. Repair-Modus
Ziel:

- Spread wieder heilen
- Ratio kontrollieren
- Long-Average nach unten ziehen

### C. Emergency 100:100 Modus
Ziel:

- Hedge einfrieren
- Marktstress überleben
- keine exponentielle Eskalation
- feste lineare Adds

### D. Bridge-Modus
Ziel:

- von `100:100` wieder zurück zu `100:80 -> 100:60 -> 100:50`
- danach Übergabe zurück an Burn oder Repair

---

## 3. State Machine

Geplante Zustände:

- `WAIT_FOR_HEDGE`
- `NORMAL_BURN`
- `NORMAL_REPAIR`
- `EMERGENCY_100`
- `BRIDGE_TO_NORMAL`
- `PAUSED`
- `FAILSAFE`

Wichtige Übergänge:

- `WAIT_FOR_HEDGE -> NORMAL_BURN`
- `NORMAL_BURN -> NORMAL_REPAIR`
- `NORMAL_REPAIR -> NORMAL_BURN`
- `NORMAL_BURN -> EMERGENCY_100`
- `NORMAL_REPAIR -> EMERGENCY_100`
- `EMERGENCY_100 -> BRIDGE_TO_NORMAL`
- `BRIDGE_TO_NORMAL -> NORMAL_REPAIR`
- `BRIDGE_TO_NORMAL -> NORMAL_BURN`
- jeder Zustand -> `FAILSAFE`, wenn Schutzbedingungen verletzt werden

---

## 4. Architektur

### Gemeinsame Basis-Schicht

Diese Teile sollen nicht pro Bot doppelt existieren:

- Market-State
- Position-State
- Order-State
- Risk-Engine
- Runtime-Store
- Decision-Logger
- Strategy-Context
- Action-Executor

### Strategie-Module

- `burn_strategy`
- `repair_strategy`
- `emergency_100_strategy`
- `bridge_strategy`

Jede Strategie darf:

- Entscheidungen vorschlagen
- Gründe liefern
- Kennzahlen liefern

Jede Strategie darf **nicht**:

- eigenständig State wechseln
- direkt Orders ans Exchange schicken

---

## 5. Entscheidungsmodell

Jede Strategie soll ein einheitliches Ergebnis zurückgeben:

- `mode`
- `allowed`
- `reason`
- `metrics`
- `actions`
- `priority`

Beispiel für Actions:

- `ADD_SHORT`
- `ADD_LONG`
- `REDUCE_SHORT`
- `REDUCE_LONG`
- `FREEZE_TO_100_100`
- `START_BRIDGE`
- `DO_NOTHING`

---

## 6. Logging-Konzept

Wir wollen bis ins Detail nachvollziehen können, was passiert.

### Log-Kategorien

#### A. Snapshot-Logs

Bei jedem Entscheidungs-Tick:

- Preis
- long_size
- short_size
- long_avg
- short_avg
- spread_pct
- ratio
- ATR
- Momentum / Geschwindigkeit
- aktueller Modus
- aktueller Zyklus

#### B. Transition-Logs

Bei jedem State-Wechsel:

- `from_state`
- `to_state`
- `reason`
- Trigger-Werte

#### C. Decision-Logs

- welche Strategie hat entschieden
- warum
- welche Aktion vorgeschlagen
- welche Aktion blockiert wurde

#### D. Order-Intent-Logs

- side
- qty
- price
- purpose
- source strategy
- cycle id
- order group id

#### E. Execution-Logs

- submit
- fill
- cancel
- reject
- retry
- fallback

#### F. Reconciliation-Logs

- Soll-/Ist-Positionsvergleich
- Order-Abweichungen
- Recovery-Aktionen

#### G. Cycle-Summary-Logs

Nach jedem Burn-/Repair-/Emergency-/Bridge-Zyklus:

- Spread vorher / nachher
- Ratio vorher / nachher
- Notional vorher / nachher
- Ergebnis
- nächster Zustand

---

## 7. Emergency 100:100 Strategie

### Notfall-Umschaltung

Beispielhafte Trigger:

- Spread >= `2.5% bis 3.0%`
- Preisbewegung schneller als `ATR`
- Ratio driftet stark
- mehrere Repair-Zyklen ohne Verbesserung
- Burn/Repair blähen die Struktur zu stark auf

Dann:

- Burn und Repair pausieren
- Hedge auf `100:100` einfrieren
- Ping-Pong-Logik aktivieren

### Ping-Pong Grundregel

- fixe Add-Größe
- z. B. immer `20$`
- nicht prozentual von der neuen Gesamtgröße

Beispiel:

- `100:100`
- `100:120`
- `120:120`
- `120:140`
- `140:140`

Ziel:

- lineares Wachstum
- kontrollierte Notfall-Anpassung

---

## 8. Bridge zurück zu 100:50

Nicht direkt von `100:100` zurück auf `100:50`.

Sondern:

- `100:100`
- `100:80`
- `100:60`
- `100:50`

Regel:

- nur den Short abbauen
- in festen Schritten
- nur wenn Markt ruhiger wird oder reboundet

Wenn der Markt wieder kippt:

- letzten Schritt zurückdrehen
- z. B. von `100:80` zurück auf `100:100`

---

## 9. Repair-Strategie Erkenntnisse

Bisherige Erkenntnisse aus der Analyse:

- `spread / 3` war robuster als `/4` oder `/5`
- aggressivere Divider allein lösen das Problem nicht
- wichtiger waren:
  - Short-Add-Timing
  - Caps
  - base step
  - saubere Umschaltung

Beste bisher gefundene Repair-Richtung:

- `divider = /3`
- `base_multiplier = 0.25`
- `base_step = 0.0025`
- `max_rebuy_usdt = 20`
- Short-Heilung kontrolliert und nicht chaotisch

---

## 10. Umsetzungsphasen

### Phase 1
Gemeinsamen Unterbau bauen:

- strategy_context
- runtime_store
- decision_logger
- order/position/market snapshots

### Phase 2
Burn-Logik und Repair-Logik in Module kapseln

- noch ohne Verhaltensänderung
- nur sauber trennen

### Phase 3
Master-State-Machine einführen

- Umschaltung Burn <-> Repair
- Logging der Übergänge

### Phase 4
Emergency 100:100 Modul hinzufügen

- Freeze
- Ping-Pong
- feste Add-Größe

### Phase 5
Bridge-Modul hinzufügen

- Rückkehr von `100:100` zu `100:50`

### Phase 6
Simulation / Dry-Run / Debug

- jede Strategie isoliert testen
- State-Wechsel testen
- Logs validieren

---

## 11. Konkrete ToDo-Reihenfolge

1. Gemeinsame Datenobjekte definieren
2. State Machine entwerfen
3. Einheitliches Decision-Format festlegen
4. Logging-Events definieren
5. Burn-Strategie kapseln
6. Repair-Strategie kapseln
7. Emergency-100-Strategie spezifizieren
8. Bridge-Strategie spezifizieren
9. Master-Controller bauen
10. Simulation und Debugging aufsetzen

---

## 12. Wichtigste Design-Regeln

- nur ein aktiver Modus gleichzeitig
- Strategien schlagen nur vor, sie exekutieren nicht selbst
- nur der Master wechselt den State
- nur der Executor sendet Orders an die Exchange
- alle Entscheidungen müssen vollständig geloggt werden
- Notfallmodus getrennt von Burn und Repair halten

---

## 13. Ziel am Ende

Ein übersichtliches System mit:

- klaren Zuständen
- klaren Zuständigkeiten
- gutem Logging
- einfacherem Debugging
- kontrollierter Notfalllogik
- nachvollziehbarer Übergabe zwischen Burn, Repair und 100:100
