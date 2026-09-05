# Liquidity Pool Market Response Strategy V0

## 1. Status

**Research-Zwischenstand – noch keine bewiesene Trading-Edge.**

Diese Datei dokumentiert den aktuellen Wissensstand, damit bereits gewonnene
Erkenntnisse bei der weiteren Forschung nicht verloren gehen.

Die Strategie ist noch nicht eingefroren und noch nicht für Live-Trading
freigegeben. Regeln dürfen erst nach kausaler Prüfung, Prefix-Parität,
manueller Chartkontrolle und ausreichender Forward-Stichprobe als bestätigt
gelten.

Aktueller technischer Stand:

- Pool-Detection-Foundation commit:
  `9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4`
- Arrival-/Wall-Monitor commit:
  `4117335c6b30e58c17c1c7a591de21d5b954af0a`
- Wall-/Trade-Reaktionsaudit commit:
  `0d469e3e30c2f49c1a2a53139bd9bddf366c5ea4`
- verwendete Pool-Engine:
  `indicators.liquidity_location.engine.run_liquidity_location`
- Raw-Orderbuch:
  OB200 mit vollständigen Preislevels
- Public Trades:
  canonical, dedupliziert, Aggressor Buy/Sell

## 2. Grundprinzip

Wir handeln nicht einen Liquidity Pool, eine Orderbook-Wall, ein
EMA-Signal oder aggressives Volumen isoliert.

Wir handeln nur eine kausal erkennbare Marktreaktion an einem bereits
bekannten relevanten Liquidity Pool.

Die zentrale Frage lautet:

> Bewegt die aggressive Marktseite den Preis tatsächlich, oder wird ihre
> Aggression von passiver Liquidität aufgenommen, ohne nachhaltigen
> Preisfortschritt zu erzeugen?

Ein Pool ist zunächst nur eine Entscheidungszone. Er sagt nicht automatisch,
ob der Preis abgewiesen wird oder durchbricht.

## 3. Informationshierarchie

### 3.1 Ort

Liquidity Location bestimmt, wo eine relevante Entscheidung stattfinden
kann.

Die Pool-Erkennung muss dieselbe Engine und Semantik wie der Research-Chart
verwenden.

### 3.2 Kontext

Höhere Timeframes bestimmen:

- übergeordnete Marktstruktur;
- Trendrichtung;
- verfügbaren Raum;
- nächsten relevanten Gegenpool;
- mögliche Ziel- und Gefahrenzonen.

Vorläufige Hierarchie:

- 1h und 30m: Kontext, Raum und große Zielpools;
- 15m: mittlere Struktur und Richtungsbestätigung;
- 5m: eigentliche Poolreaktion und späterer Entry-Kontext;
- Raw OB200 und Public Trades: Mikrostruktur innerhalb der Entscheidung.

Die Pools jedes Timeframes sind eigenständige Objekte und dürfen nicht
miteinander verwechselt werden.

### 3.3 Passive Liquidität

Raw OB200 zeigt:

- sichtbare ASK- und BID-Walls;
- Wall-Größe und Full-Side-Rank;
- Persistenz;
- Refill;
- Trade-Depletion;
- Cancel oder Move;
- neu erscheinende Liquidität;
- mögliche gerichtete Wall-Retreats.

Eine sichtbare Wall ist allein niemals ein Entry-Signal.

### 3.4 Aggressive Liquidität

Public Trades zeigen:

- aggressives Buy-Notional;
- aggressives Sell-Notional;
- Brutto-Flow beider Seiten;
- Angriffszeitpunkt;
- tatsächliche Preiswirkung;
- Effizienz oder Ineffizienz des Angriffs.

Netto-Delta allein reicht nicht. Buy- und Sell-Aggression müssen getrennt
betrachtet werden.

### 3.5 Entscheidung

Acceptance, Reclaim, Re-Entry, Fortschritt und Invalidierung zeigen, welche
Marktseite tatsächlich Kontrolle übernimmt.

## 4. Kausaler Entscheidungsablauf

1. Einen bestätigten Liquidity Pool erkennen.
2. Warten, bis der Preis den Pool tatsächlich aus einer definierten Richtung
   erreicht.
3. Markt- und Timeframe-Kontext bestimmen.
4. Prüfen, ob bis zum nächsten relevanten Pool ausreichend Raum für Kosten
   und Risiko vorhanden ist.
5. Aggressive Käufer und Verkäufer an Poolkante und innerhalb des Pools
   beobachten.
6. Preiswirkung der jeweiligen Aggression messen.
7. Gleichseitige und gegenüberliegende OB200-Walls beobachten.
8. Trade-Depletion, Refill, Cancel/Move und Wall-Retreat unterscheiden.
9. Acceptance, Reclaim, Local Exit und Re-Entry kausal verfolgen.
10. Nur bei einem einfachen und stabilen Kontrollwechsel einen
    Entry-Kandidaten zulassen.
11. Bei widersprüchlicher oder wiederholt invalidierter Evidenz:
    `NO_TRADE`.
12. Am nächsten relevanten Pool die Situation neu bewerten.

## 5. Vorläufige Reaktionspfade

### 5.1 Rejection / Absorption

Beispiel ASK-Pool:

- Käufer greifen aggressiv an;
- Buy-Flow erzeugt wenig nachhaltigen Aufwärtsfortschritt;
- passive ASK-Liquidität hält oder refilled;
- Preis wird unter die Eintrittskante zurückgeführt;
- Reclaim bleibt stabil;
- Verkäufer übernehmen anschließend kausal.

Mögliche Entscheidung:

`SHORT_REJECTION_CANDIDATE`

Spiegelbildlich am BID-Pool:

- starke aggressive Verkäufer;
- geringe nachhaltige Abwärtswirkung;
- BID-Liquidität hält oder refilled;
- Preis reclaimt die Eintrittskante nach oben;
- Käufer übernehmen.

Mögliche Entscheidung:

`LONG_REJECTION_CANDIDATE`

Eine Rejection ist nicht bestätigt, wenn die betreffende Wall niemals
sinnvoll attackiert wurde.

### 5.2 Breakout / Acceptance

Beispiel ASK-Pool:

- Käufer erzielen positiven Preisfortschritt;
- interne ASK-Walls werden tatsächlich gehandelt und überwunden oder weichen
  kausal;
- Preis erreicht die gegenüberliegende Poolkante;
- Preis wird oberhalb der gesamten Poolkomponente akzeptiert.

Mögliche Entscheidung:

`LONG_BREAKOUT_CANDIDATE`

Spiegelbildlich am BID-Pool:

`SHORT_BREAKOUT_CANDIDATE`

Ein einzelner Tick oder Docht jenseits der Poolkante reicht nicht.
Acceptance-Zeit und Re-Entry müssen kausal berücksichtigt werden.

### 5.3 Pressure Inside Pool

Möglicher bullischer Zustand im ASK-Pool:

- Preis bleibt überwiegend innerhalb oder oberhalb der unteren Poolkante;
- aggressive Verkäufer erzeugen keinen stabilen Downside-Fortschritt;
- Rücksetzer werden wieder in den Pool aufgenommen;
- Käufer erzeugen anschließend positiven Fortschritt;
- interne ASK-Liquidität wird schrittweise überwunden oder zieht sich nach
  oben zurück.

Vorläufige Bezeichnung:

`BULLISH_PRESSURE_INSIDE_ASK_POOL`

Spiegelbildlich:

`BEARISH_PRESSURE_INSIDE_BID_POOL`

Dieser Pfad ist noch nicht bestätigt und darf noch nicht als eigenständiger
Entry-Trigger verwendet werden.

### 5.4 Wiederholte Invalidierung

Wenn mehrere vermeintliche Kontrollwechsel erneut scheitern:

- wiederholter Bruch der Eintrittskante;
- Buy-/Sell-Takeover wird mehrfach invalidiert;
- kein stabiler Fortschritt;
- langer zweiseitiger Poolkampf.

Dann:

`NO_TRADE_REPEATED_INVALIDATION`

Der Pool wird nicht aus Hoffnung gehandelt. Ein späterer Ausbruch beweist
keinen früher handelbaren Entry.

### 5.5 Unklare Reaktion

Bei widersprüchlichen, fehlenden oder nicht kausal trennbaren Daten:

`AMBIGUOUS_POOL_CONTEST_NO_TRADE`

`NO_TRADE` ist ein korrekter und erfolgreicher Ausgang der Strategie.

## 6. Wall-Semantik

Folgende Zustände müssen getrennt bleiben:

### 6.1 Trade Depletion

Eine Wall wurde durch aggressiven Handel reduziert oder überwunden.

`TRADE_DEPLETION`

### 6.2 Cancelled Before Touch

Eine Wall verschwand, bevor ein ausreichender Angriff oder Trade-Kontakt
belegt wurde.

`CANCELLED_BEFORE_TOUCH`

Dies ist keine Absorption und keine konsumierte Wall.

### 6.3 Reappeared Higher / Lower

Bedeutende Liquidität erscheint nach dem Verschwinden einer Wall auf einem
anderen Preislevel.

ASK höher:

`ASK_LIQUIDITY_REAPPEARED_HIGHER`

BID tiefer:

`BID_LIQUIDITY_REAPPEARED_LOWER`

Aus normalen Orderbuchdaten kann nicht sicher behauptet werden, dass es sich
um dieselbe einzelne Order handelt.

### 6.4 Repeated Wall Retreat

Potentiell bullisch:

`REPEATED_ASK_WALL_RETREAT_WITH_PRICE_FOLLOW`

Potentiell bearish:

`REPEATED_BID_WALL_RETREAT_WITH_PRICE_FOLLOW`

Ein Wall-Retreat ist nur unterstützende Evidenz und kein alleiniger
Entry-Trigger.

Er benötigt mindestens:

- wiederholtes gerichtetes Verschwinden bedeutender Walls vor Kontakt;
- Wiedererscheinen bedeutender Liquidität weiter in Bewegungsrichtung;
- Preis folgt der Verschiebung;
- Marktstruktur bleibt stabil;
- keine wiederholte Invalidierung der Eintrittskante.

## 7. Aggressor-Effizienz

Grundlegende Interpretation:

| Aggressor-Beobachtung | Preiswirkung | Vorläufige Bedeutung |
|---|---|---|
| starkes Buying | deutlicher Anstieg | Käufer effizient |
| starkes Buying | kaum Anstieg | mögliche Buy-Absorption |
| starkes Selling | deutlicher Rückgang | Verkäufer effizient |
| starkes Selling | kaum Rückgang | mögliche Sell-Absorption |
| hoher Flow beider Seiten | geringe Bewegung | zweiseitiger Poolkampf |
| geringer Flow | geringe Bewegung | keine belastbare Aussage |

Absorption darf nur behauptet werden, wenn ein ausreichend großer echter
Angriff vorhanden war.

Preishalten ohne relevanten Angriff ist kein Absorptionsbeweis.

## 8. Mindestbedingungen für einen Kontrollwechsel

Ein Re-Entry allein ist kein Takeover.

Ein möglicher Kontrollwechsel benötigt vorläufig:

1. einen belegten Angriff;
2. fehlenden nachhaltigen Fortschritt des Angreifers;
3. Reclaim oder Re-Entry;
4. Übernahme durch die Gegenseite;
5. messbaren Preisfortschritt;
6. Überwindung eines kausal sichtbaren Hindernisses;
7. Stabilität ohne sofortige erneute Invalidierung.

Ein Kontrollwechsel darf nicht rückwirkend durch den späteren Chartverlauf
bestätigt werden.

## 9. Verbindliche NO-TRADE-Fälle

Vorläufig kein Trade bei:

- Wall sichtbar, aber nicht sinnvoll attackiert;
- Wall-Cancel ohne gerichtete Preisfolge;
- Re-Entry ohne Takeover;
- hohem Two-Sided-Flow ohne klare Kontrolle;
- mehreren invalidierten Kontrollwechseln;
- unklarer Wall-Identität;
- fehlender Prefix-Parität;
- Datenlücken;
- zu engem Raum bis zum nächsten Gegenpool;
- widersprüchlichen Timeframe-Signalen;
- erst rückblickend erkennbarem Setup;
- zu vielen notwendigen Sonderregeln.

Wir müssen nicht jeden Pool handeln.

## 10. CASE_02 – dokumentierte Erkenntnis

Referenz:

- BTCUSDT 5m
- 2026-08-25T00:47:13Z
- ASK-Pool `[79678.7, 80116.8]`
- Upper-Edge-Cross `02:17:52Z`
- 5s-Acceptance `02:17:56Z`

Beobachtung:

- starker zweiseitiger Flow an der Unterkante;
- aggressive Seller häufig ineffizient;
- mehrere Local Exits und Re-Entries;
- etwa 40 Lower-Edge-Reset-Episoden;
- Start-Wall `79700` wurde tatsächlich attackiert und überwunden;
- mehrere interne Walls zeigten Trade-Depletion, Refill und Cancel/Move;
- fünf Buy-Takeover-Kandidaten wurden später invalidiert;
- kein früher oder mittlerer Kontrollwechsel blieb bis zum Breakout stabil;
- letzter gefährlicher Rückfall brach Poolunterkante und kausale EMA20;
- erst deutlich später vollständiger bullischer Pool-Breakout.

Entscheidung:

`NO_TRADE_REPEATED_INVALIDATION`

Der spätere bullische Ausbruch macht einen früheren Long nicht nachträglich
gültig.

## 11. EMA-Semantik

EMA-Werte dürfen nur aus kausal verfügbaren geschlossenen Kerzen stammen.

Eine visuelle spätere EMA-Nähe darf nicht rückwirkend dem Arrival-Zeitpunkt
zugerechnet werden.

EMAs liefern Kontext und mögliche dynamische Entscheidungsbereiche. Sie
ersetzen nicht die Mikrostrukturentscheidung am Pool.

## 12. Vorläufige Entry-Varianten

Noch nicht bestätigt:

### Variante A – Rejection Entry

Entry erst nach:

- ineffizientem Angriff;
- stabilem Reclaim;
- kausaler Gegenübernahme;
- ausreichendem Raum zum nächsten Pool.

### Variante B – Pressure-Inside Entry

Entry erst nach:

- wiederholt ineffizientem Gegenangriff;
- stabiler Poolstruktur;
- gerichtetem Wall-Retreat oder interner Wall-Überwindung;
- messbarer Übernahme;
- keiner erneuten Invalidierung.

Diese Variante ist noch besonders unsicher.

### Variante C – Breakout Entry

Entry erst nach:

- vollständigem Cross der gegenüberliegenden Poolkante;
- kausaler Acceptance;
- Prüfung des verbleibenden Raums bis zum nächsten höheren
  Timeframe-Gegenpool.

Ein großer Breakout darf nicht blind hinterhergekauft werden.

## 13. Vorläufige Exit-Idee

Noch nicht bestätigt und nicht implementiert:

Keine pauschale feste Haltedauer.

Der nächste relevante Pool ist die neue Entscheidungszone.

Mögliche Logik:

- Rejection oder Absorption am Zielpool:
  Position schließen;
- Pool konsumiert und jenseits akzeptiert:
  Position weiter bis zum nächsten Pool beobachten;
- unklare Reaktion:
  Gewinn schützen oder schließen;
- gegenteiliger stabiler Kontrollwechsel:
  Exit.

Diese Pool-to-Pool-Exit-Logik muss separat kausal untersucht werden.

## 14. Noch offene Forschungsfragen

1. Existiert ein einfacher stabiler Rejection-Trigger?
2. Existiert ein einfacher stabiler Breakout-Trigger?
3. Ist gerichteter Wall-Retreat zusätzliche belastbare Evidenz?
4. Welche Mindeststabilität verhindert falsche Re-Entries?
5. Wie werden überlappende Pools kausal als Encounter-Episode behandelt?
6. Wie wird der nächste relevante Gegenpool über mehrere Timeframes gewählt?
7. Wie viel Abstand ist nach Kosten und Slippage notwendig?
8. Wann ist ein Breakout-Entry bereits zu spät?
9. Wie unterscheiden sich ASK- und BID-Semantik empirisch?
10. Funktioniert die Logik auf weiteren Coins und Tagen?
11. Wie wird Trendkontext ohne Outcome-Fitting integriert?
12. Wie wird ein Trade am Zielpool dynamisch fortgeführt oder beendet?

## 15. Forschungsregeln

Für alle weiteren Untersuchungen:

- keine Outcome-Nutzung für Matching;
- keine Outcome-Nutzung für Schwellen;
- keine Outcome-Nutzung für Zustandsdefinitionen;
- Event-Auswahl offen kennzeichnen;
- Prefix-Parität prüfen;
- Raw- und State-aligned Outcomes trennen;
- keine nachträgliche Erzählung;
- keine Grid-Suche auf kleinen Stichproben;
- keine Trading-Edge behaupten, bevor sie forward bestätigt ist;
- Gebühren und Slippage vor einer Trading-Freigabe berücksichtigen;
- Dirty Worktree und Live-Prozesse schützen;
- Research-Artefakte zunächst ohne Commit prüfen.

## 16. Aktueller nächster Schritt

CASE_02 bleibt:

`NO_TRADE_REPEATED_INVALIDATION`

Als Nächstes wird ein nach dem bestätigten Breakout kausal verfügbarer,
eindeutigerer Pool untersucht.

Dort prüfen wir erneut:

- Arrival-Richtung;
- Aggressor-Effizienz;
- Wall-Angriff;
- Trade-Depletion;
- Refill;
- Cancel/Move;
- gerichteten Wall-Retreat;
- Acceptance oder Rejection;
- Raum bis zum nächsten höheren Timeframe-Pool.

Es wird kein Entry erzwungen.
