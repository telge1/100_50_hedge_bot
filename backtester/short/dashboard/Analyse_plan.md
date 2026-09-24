# Analyseplan für Candles, Public Trades, Orderbuch, Liquidationen und OI

---

## Schritt 1: Volatilitäts-Event-Detector

### Ziel

Automatisch erkennen, wann die kurzfristige Volatilität eines Coins gegenüber seinem normalen Zustand stark ansteigt. Anschließend untersuchen wir, ob ein Pump, Dump, Squeeze oder eine Liquidationsbewegung entstanden ist.

### Volatilitätsmessung

Die kurzfristige Volatilität der letzten 1, 5 und 15 Minuten wird mit dem Median der vergangenen 24 Stunden verglichen:

```text
Volatilitätsmultiplikator =
aktuelle kurzfristige Volatilität /
normale Volatilität der vergangenen 24 Stunden
```

### Bewertung

* ab `2×`: erhöhte Volatilität
* ab `3×`: stark
* ab `5×`: extrem
* ab `10×`: außergewöhnliches Ereignis

### Zusätzlich messen

* Preisänderung über 1, 3, 5, 15 und 30 Minuten
* ATR- und Candle-Range-Multiplikator
* Handelsvolumen und Trade-Anzahl
* aggressives Kauf-/Verkaufsvolumen
* Open-Interest-Veränderung
* Long-/Short-Liquidationen
* Orderbuch-Imbalance, Walls und entfernte Liquidität

### Ereignisklassifikation

* Preis steigt + OI steigt → neuer Long-Aufbau
* Preis steigt + OI fällt → möglicher Short Squeeze
* Preis fällt + OI steigt → neuer Short-Aufbau
* Preis fällt + OI fällt → Long-Capitulation beziehungsweise Long-Liquidationen

Public Trades, Liquidationen und Orderbuchdaten müssen die Einordnung bestätigen.

### Live-Chart-Darstellung

Volatilitätsereignisse werden im Chart markiert:

* Gelb: ab `2×`
* Orange: ab `3×`
* Rot: ab `5×`
* Violett: ab `10×`

Beim Anklicken erscheint ein Kurzreport mit:

* Volatilitätsmultiplikator
* Preisbewegung
* Volumen und aggressiver Handelsrichtung
* OI-Veränderung
* Liquidationen
* Orderbuchverhalten
* Ereignisklassifikation
* Preisentwicklung nach 5, 15 und 30 Minuten

### Umsetzung

Zuerst werden historische Volatilitätsereignisse regelbasiert und ohne Lookahead erkannt. Danach vergleichen wir die verschiedenen Ereignistypen und prüfen, welche Kombinationen Pumps, Dumps, Reversals oder Fortsetzungen zuverlässig ankündigen. Erst nach erfolgreicher historischer Prüfung wird der Detector live eingesetzt.

---

## Schritt 2: Markt-Event-Kurzreport

Das wäre mein empfohlener nächster Schritt und gleichzeitig die Grundlage für fast alles Weitere.

Du klickst im Live-Chart auf eine Stelle. Das System analysiert beispielsweise 5 Minuten davor und danach:

* Preisbewegung
* aggressives Kauf-/Verkaufsvolumen
* CVD
* OI-Veränderung
* Liquidationen
* Orderbuch-Imbalance
* Walls und Liquiditätsentzug
* Ergebnis nach 5, 15 und 30 Minuten

Am Ende erscheint eine nachvollziehbare Einordnung wie:

```text
SELL_ABSORPTION_WITH_RECLAIM
SHORT_SQUEEZE
LONG_BUILDUP
FAILED_BREAKOUT
NO_CLEAR_CONFIRMATION
```

---

## Schritt 3: Liquidity-Pool-Reaktionsanalyse

Für jeden Poolkontakt untersuchen:

* wurde der Pool nur berührt oder vollständig durchhandelt?
* entstand Absorption?
* blieb die schützende Orderbuchseite bestehen?
* wurde Liquidität kurz vor dem Durchbruch entfernt?
* kam es zu Reclaim oder bestätigtem Breakout?
* was machten OI und Liquidationen dabei?

Damit können wir herausfinden, welche Pools wirklich halten und welche wahrscheinlich brechen.

---

## Schritt 4: Absorptions-Detector

Erkennt Situationen wie:

* sehr viele aggressive Verkäufe
* Preis fällt trotzdem kaum weiter
* Bid-Liquidität wird wiederholt aufgefüllt
* anschließend Reclaim

Das wäre potenzielle Kaufabsorption.

Umgekehrt:

* sehr viele aggressive Käufe
* Preis steigt kaum
* Ask-Seite wird aufgefüllt
* anschließend Ablehnung

Das wäre potenzielle Verkaufsabsorption.

---

## Schritt 5: Breakout- und Fakeout-Detector

Wir können echte Durchbrüche von falschen unterscheiden.

### Möglicher echter Breakout

* Liquidität vor dem Preis wird entfernt oder konsumiert
* aggressives Volumen bestätigt die Richtung
* OI steigt
* Preis hält nach dem Durchbruch
* kein schneller Reclaim

### Möglicher Fakeout

* hoher Trade-Druck, aber geringe Preiswirkung
* Gegenliquidität absorbiert
* OI fällt oder bleibt schwach
* schneller Reclaim
* Bewegung kehrt zurück in die vorherige Zone

---

## Schritt 6: Pump-/Dump-Ursachenanalyse

Nicht nur feststellen, dass der Preis gestiegen oder gefallen ist, sondern warum:

* neuer Long-Aufbau
* neuer Short-Aufbau
* Short Squeeze
* Long-Liquidationskaskade
* reine Liquiditätslücke
* aggressiver Spot-/Perpetual-Druck
* Bewegung ohne nachhaltigen Positionsaufbau

Das ist eine Erweiterung des Volatilitäts-Detectors.

---

## Schritt 7: Liquidationskaskaden-Detector

Erkennt:

* erste Liquidationen
* beschleunigte Preisbewegung
* weitere Liquidationen
* sinkendes OI
* Orderbuch-Tiefe bricht weg
* mögliches Erschöpfungs- oder Reversal-Level

Danach prüfen wir, ob der Preis:

* sofort weiterläuft
* kurzfristig bounced
* vollständig reclaimt
* nur kurz pausiert

---

## Schritt 8: OI-Regime-Analyse

Wir klassifizieren dauerhaft den Marktzustand:

| Preis  | OI     | Regime                 |
| ------ | ------ | ---------------------- |
| steigt | steigt | Long-Aufbau            |
| steigt | fällt  | Short-Covering         |
| fällt  | steigt | Short-Aufbau           |
| fällt  | fällt  | Long-Close/Liquidation |

Zusammen mit Funding wäre die Interpretation später noch besser, aber auch ohne Funding ist diese Analyse bereits nützlich.

---

## Schritt 9: Footprint- und Delta-Analyse

Aus Public Trades können wir pro Kerze berechnen:

* aggressives Buy-Volumen
* aggressives Sell-Volumen
* Delta
* kumulatives Delta
* Trade-Anzahl
* durchschnittliche Trade-Größe
* große Einzeltrades
* Delta-Divergenzen

### Beispiel

```text
Preis macht neues Tief
CVD macht kein neues Tief
Verkaufsdruck wird absorbiert
Bid-Seite wird aufgefüllt
→ mögliche bullische Divergenz
```

---

## Schritt 10: Orderbuch-Wall-Analyse

Wir können untersuchen:
sonst haben wir spater oh,
* ob sie näher an den Preis wandert 
* ob sie aufgefüllt wird
* ob sie kurz vor Kontakt verschwindet
* ob sie tatsächlich gehandelt wird
* wie der Preis nach Kontakt reagiert

Dadurch unterscheiden wir eher:

* echte Unterstützung/Widerstand
* kurzlebige Liquidität
* mögliches Spoofing
* konsumierte Wall
* gehaltene Wall

„Spoofing“ sollte dabei nur als Verdacht bezeichnet werden, weil wir die Absicht des Traders nicht sicher kennen.

---

## Schritt 11: Marktregime-Detector

Die Coins lassen sich in Zustände einteilen:

* ruhiger Markt
* Trend
* hohe Volatilität
* geringe Orderbuch-Tiefe
* Squeeze
* Liquidationsphase
* Range
* Breakout-Aufbau
* Erschöpfung

Dann können wir prüfen, in welchem Regime eure Stochastic-, Pool- oder Reclaim-Signale funktionieren.

---

## Schritt 12: Signal-Gewinner gegen Signal-Verlierer

Für jedes bestehende Frozen-Tier-A-Signal untersuchen wir den Zustand am kausalen Entry:

* Volatilität
* OI
* Liquidationen
* Trade-Delta
* Orderbuch-Imbalance
* nächste Walls
* Absorption
* Poollage
* Marktregime

Danach vergleichen wir Gewinner und Verlierer. So können wir herausfinden, ob bestimmte Merkmale als Filter einen echten Mehrwert bringen.

---

# Empfohlene Reihenfolge

1. Volatilitäts-Detector – bereits geplant
2. gemeinsamer Daten-Coverage- und Zeitsynchronitäts-Audit
3. Markt-Event-Kurzreport für einen anklickbaren Chartpunkt
4. Poolkontakt-, Breakout- und Reclaim-Report
5. Absorptions-Detector
6. Pump-/Dump- und Squeeze-Klassifikation
7. Liquidationskaskaden
8. Marktregime
9. Gewinner-/Verlierer-Vergleich der Tier-A-Signale
10. historischer Backtest der besten Filter
11. erst danach Live-Erkennung und mögliche Tradingentscheidung

Als nächste speicherbare Kurzfassung würde ich den Markt-Event-Kurzreport nehmen. Er wird unsere zentrale Analysefunktion: Alle späteren Detectoren können denselben Daten- und Report-Unterbau verwenden.


######################################################################################################################

Relevantes Preislevel
+ ungewöhnliche Volatilität
+ aggressiver Trade-Druck
+ Orderbuchreaktion
+ OI-Veränderung
+ Liquidationen
+ bestätigter Reclaim oder Durchbruch

ADAUSDT | 14:32:05 UTC
Preis
Volatilität
Buy-/Sell-Volumen
CVD
OI
Long-/Short-Liquidationen
Bid-/Ask-Depth
Orderbuch-Imbalance
Walls


if price_change > 0 and oi_change > 0:
    classification = "LONG_BUILDUP"

elif price_change > 0 and oi_change < 0:
    classification = "POSSIBLE_SHORT_SQUEEZE"

elif price_change < 0 and oi_change > 0:
    classification = "SHORT_BUILDUP"

elif price_change < 0 and oi_change < 0:
    classification = "LONG_CLOSE_OR_LIQUIDATION"


research/
  market_event_analyzer/
    coverage.py
    queries.py
    alignment.py
    features.py
    detectors/
      volatility.py
      absorption.py
      breakout.py
      liquidation_cascade.py
    classifiers.py
    outcomes.py
    reports.py
    runner.py
    config.yaml
    tests/


    Empfohlener erster Code-Pilot

Wir sollten noch nicht alle zwölf Detectoren gleichzeitig programmieren:

Coverage- und Synchronitäts-Audit erstellen.
Ein Symbol mit vollständiger gemeinsamer Abdeckung auswählen.
Volatilitäts-Detector V1 implementieren.
Zunächst einen UTC-Tag analysieren.
Danach sieben Tage dieses Symbols.
Für die zehn stärksten Ereignisse Kurzreports erzeugen.
Rohdaten und Reportwerte stichprobenartig vergleichen.
Erst danach auf alle 51 Coins erweitern.
Anschließend denselben Unterbau für den Markt-Event-Klickreport verwenden.

Das ist der einfachste und sauberste Weg: ein gemeinsamer Analyse-Unterbau, verschiedene kleine Detectoren und pro Ereignis ein automatisch erzeugter Report.