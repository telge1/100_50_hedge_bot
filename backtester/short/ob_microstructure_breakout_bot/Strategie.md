# ob_microstructure_breakout_bot Strategie

Diese Strategie sucht **EMA59-Touches auf voll geschlossenen 5m-Bars** und bewertet sie erst dann, wenn sowohl das Confirm-Fenster als auch das Follow-through-Fenster vollstaendig vorliegen. Dadurch wird kein Signal auf Basis einer noch offenen Kerze erzeugt.

## 1. Ziel der Strategie

Die Strategie will keine dauerhafte Trendfolge auf jeder Bewegung machen, sondern nur dann handeln, wenn ein EMA59-Touch durch

- die sofortige Reaktion am Touch,
- das Confirm-Fenster,
- das Follow-through-Fenster,
- und das Full-OB-Bild am Touch-Zeitpunkt

sauber bestaetigt oder verworfen werden kann.

Das Ergebnis ist eine kleine Anzahl von qualitativ hoeherwertigen Signalen statt vieler schwacher Entry-Punkte.

## 2. Welche Daten verwendet werden

Die Logik kombiniert vier Datensaetze:

1. **5m OHLC-Bars**
   - nur voll geschlossene Bloecke
   - kein Intrabar- oder 1m-Replay

2. **EMA-Snapshots**
   - EMA9
   - EMA20
   - EMA59
   - EMA200

3. **Public-trade-Fenster**
   - Context-Fenster vor dem Touch
   - Confirm-Fenster direkt am Touch
   - Follow-through-Fenster danach

4. **Full Order Book**
   - Sampling bei `touch.bar_ts`
   - 5bps und 10bps Band-Notional um den Mid

### 2.1 30m Kontext vor dem Touch

Vor jedem EMA59-Touch wird der Kontext der letzten 30 Minuten mit ausgewertet.
Dieser Kontext ist wichtig, um den Touch nicht isoliert zu sehen, sondern in die
vorgelagerte Marktstruktur einzuordnen.

Dabei werden vor allem diese Dinge betrachtet:

- Trade-Flow der letzten 30 Minuten
- Delta im Context-Fenster
- EMA-Struktur um den Touch herum
- ob die Bewegung bereits vor dem Touch Druck in dieselbe Richtung aufgebaut hat

Der 30m-Kontext ist damit die Vorstufe fuer die eigentliche Touch-Bewertung.
Er bestimmt nicht allein das Signal, hilft aber dabei, echte Reclaims,
schwache Retests und saubere Breakout-Ansatzpunkte zu unterscheiden.

## 3. Grundidee des Scanners

Der Scanner sucht im 5m-Frame nach Kerzen, die die EMA59 beruehren oder kreuzen.

Wichtige Punkte:

- `TouchEvent.bar_ts` ist der Start der vollstaendigen 5m-Kerze
- ein Signal wird erst dann ausgegeben, wenn:
  - das Confirm-Fenster vollstaendig ist
  - das Follow-through-Fenster vollstaendig ist
  - der finale Signalzeitpunkt `decision_ts` bekannt ist

Damit ist die Strategie bewusst **bar-close und lookahead-sicher**.

## 4. Long-Logik

### 4.1 Long-Setup

Ein Long-Setup entsteht vor allem bei einem Touch **von unten** an die EMA59.

Wichtig: Der Touch wird immer zusammen mit dem 30m-Kontext vor dem Touch
bewertet. Erst dadurch wird sichtbar, ob der Markt bereits Druck aufgebaut hat
oder ob es nur ein zufaelliger Beruehrungspunkt ist.

Danach werden folgende Dinge bewertet:

- Confirm-Delta
- Follow-through-Delta
- Full-OB-Balance im 5bps-Bereich
- EMA-Struktur

### 4.2 Long-Ergebnis

Die Long-Seite endet typischerweise in einem dieser States:

- `breakout_confirmed`
  - der Long-Ausbruch ist bestaetigt
  - Tier:
    - `tier1_valid`
    - `tier2_strong`

- `fakeout`
  - der Touch wirkte zunaechst stark, kippt aber im Follow-through gegen den Long

- `chop`
  - die Signale sind zu schwach oder uneindeutig

- `hold`
  - das Setup ist noch intakt, aber noch kein klarer Durchbruch oder Bruch sichtbar

## 5. Short-Logik

Die Short-Seite ist die Spiegelung der Long-Seite.

### 5.1 Short-Setup

Ein Short-Setup entsteht vor allem bei einem Touch **von oben** an die EMA59.

Dann werden die gleichen Bausteine spiegelverkehrt bewertet:

- negative Confirm-Deltas statt positiver Confirm-Deltas
- ask-dominantes Orderbook statt bid-dominantem Orderbook
- negative Follow-throughs statt positiver Follow-throughs

### 5.2 Short-Ergebnis

Die Short-Seite kann ebenfalls in folgende States laufen:

- `breakout_confirmed`
  - der Short-Ausbruch ist bestaetigt

- `fakeout`
  - der Abverkauf wird nicht bestaetigt und kippt gegen den Short

- `chop`
  - zu schwach oder uneindeutig

- `hold`
  - die Struktur ist noch nicht klar genug fuer einen echten Entry/Exit

## 6. Orderbook-Filter

Der Full-OB-Teil wird bei `touch.bar_ts` aus dem Archiv gelesen und auf 5bps und 10bps zusammengefasst.

Die Idee dahinter:

- **Longs** profitieren von bid-dominantem Orderbook
- **Shorts** profitieren von ask-dominantem Orderbook

Wichtig ist dabei:

- es wird nur der Stand **vor** dem Signal verwendet
- es gibt keinen Zugriff auf Updates aus der Zukunft
- der Replay-Fix sorgt dafuer, dass Records mit `event_time >= when` nicht mehr angewendet werden

## 7. Zeitlogik

Die Strategie arbeitet mit zwei Zeiten:

- `touch.bar_ts`
  - wann die Touch-Kerze beginnt

- `decision_ts`
  - wann das Signal vollstaendig bestaetigt ist

Das ist wichtig fuer das Charting:

- **Touch** = Setup
- **Decision** = Signalzeitpunkt

Wenn du die Strategie auf Charts pruefst, solltest du deshalb immer beide Zeitpunkte markieren.

## 8. Was die Strategie bewusst nicht macht

Diese Strategie ist absichtlich konservativ und macht **nicht**:

- keine Intrabar-Auswertung auf 1m-Basis
- keine Entscheidung nur auf dem Touch allein
- keine Signalgebung ohne vollstaendiges Confirm-Fenster
- keine Signalgebung ohne vollstaendiges Follow-through-Fenster
- keine Orderbook-Nutzung aus der Zukunft
- keine Schwellenwert-Aenderung zur Laufzeit

## 9. Warum die Strategie robust sein soll

Die Logik versucht nicht, jede kleine Bewegung zu handeln.
Stattdessen will sie nur dann aktiv werden, wenn mehrere Bedingungen zusammenpassen:

- Preis beruehrt EMA59
- Trade-Flow bestaetigt die Richtung
- Orderbook stuetzt die Bewegung
- der relevante Zeitabschnitt ist vollstaendig abgeschlossen

Das macht die Strategie langsamer, aber deutlich sauberer.

## 10. Praktische Lesart fuer Charts

Beim Chart-Check kannst du die Signale so lesen:

- **Long** = Touch von unten + bestaetigter Durchbruch
- **Short** = Touch von oben + bestaetigter Abverkauf
- **Hold** = nichts tun, Setup bleibt offen
- **Fakeout** = Setup wurde gebrochen oder umgedreht
- **Chop** = Signal zu schwach, keine klare Richtung

## 11. Kurzfazit

Die `ob_microstructure_breakout_bot`-Strategie ist eine **bar-close EMA59 Touch-Strategie** mit:

- Confirm-Fenster
- Follow-through-Fenster
- Full-OB-Filter
- Long- und Short-Spiegelung
- strenger Lookahead-Vermeidung

Sie ist darauf ausgelegt, nur dann ein handelbares Signal zu liefern, wenn Preis, Flow und Orderbook in dieselbe Richtung zeigen.

########################### 12. Strategie kurz und einfach #######################################

Ganz einfach gesagt:

1. **Der Preis beruehrt EMA59 auf einer 5m-Kerze.**
2. **Wir schauen auf die letzten 30 Minuten vor diesem Touch.**
   Das ist der Kontext, damit wir sehen, ob der Markt schon Druck aufgebaut hat.
3. **Dann warten wir auf Confirm und Follow-through nach dem Touch.**
4. **Zum Schluss pruefen wir das Orderbook am Touch-Zeitpunkt.**
5. **Nur wenn alles zusammenpasst, gibt es ein Signal.**

Wichtig:

- **30m Kontext = vor dem Touch**
- **Confirm = nach dem Touch**
- **Follow-through = nach dem Touch**

## 13. Coin-spezifische Schwellenwerte

Die Schwellenwerte werden **pro Coin vorher festgelegt**.
So trennt die Strategie echte Breakouts von Fakeouts.

Beispiele fuer solche Werte sind:

- Confirm-Deltas
- Follow-through-Grenzen
- Orderbook-Ratio-Grenzen
- Fakeout-Grenzen

Dadurch kann derselbe Scanner fuer verschiedene Coins benutzt werden, ohne dass jede Coin-Bewegung gleich bewertet wird.

## 14. 5-Zeilen-Kurzversion

1. Preis beruehrt EMA59 auf einer 5m-Kerze.
2. Wir schauen auf die letzten 30 Minuten vor dem Touch.
3. Danach warten wir auf Confirm und Follow-through nach dem Touch.
4. Dann pruefen wir das Orderbook und die EMA-Struktur.
5. Mit den Coin-Schwellenwerten entscheiden wir: echter Breakout, Fakeout, Hold oder Short.