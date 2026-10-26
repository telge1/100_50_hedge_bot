# DOGEUSDT – Downtrend Pool Short Strategy V1

**Status:** Strategieentwurf – noch nicht produktiv aktiviert  
**Basis:** Original 5m PD1 Exact Origin-Density, Depth 2  
**Ziel:** Nur klare Short-Setups in bestätigten Abwärtstrends handeln.

---

## 1. Grundprinzip

Wir suchen keine beliebigen Upper-Pools.

Wir suchen einen **kleinen, nahen und isolierten Upper-Pool innerhalb eines intakten Downtrends**, über dem ein großer freier Preisbereich bis zum nächsten separaten Upper-Pool liegt.

Der Preis soll bei einem kurzen Rücklauf unseren Short-Limit-Entry erreichen können.

Wir handeln keine Shorts in Seitwärtsphasen oder bei unsicherem Marktstatus.

## 2. Marktzustand bestimmen

### SHORT OK – bestätigter Downtrend

Folgende Merkmale sollen geprüft werden:

- EMA9 < EMA20 < EMA59 < EMA200.
- Der Preis liegt unter EMA59 und EMA200.
- EMA20 und EMA59 zeigen überwiegend nach unten.
- Die Kursstruktur zeigt einen fortbestehenden Abwärtstrend.
- Keine ausgeprägte Seitwärtsphase oder offensichtliche Trendauflösung.

Die EMA-Reihenfolge ist unsere erste Forschungshypothese. Die endgültige Kombination aus EMA-Steigung und Kursstruktur muss noch getestet werden.

### SHORT NO – Seitwärtsmarkt oder unsicherer Trend

Keine neuen Short-Orders, wenn beispielsweise:

- EMA9 und EMA20 wiederholt kreuzen und flach verlaufen.
- Der Preis längere Zeit innerhalb einer engen Handelsspanne pendelt.
- EMA59 flach wird und die kurzfristige EMA-Struktur nicht mehr klar bearish ist.
- Upper-Pools unmittelbar am Kurs zu einem dichten, wachsenden Stack werden.
- Die vorhandenen Informationen keinen bestätigten Downtrend ergeben.

**Grundregel: Im Zweifel kein neuer Short.**

## 3. Den richtigen Upper-Entry-Pool finden

Vom aktuellen Preis aus nach oben suchen.

Nur den nächsten strukturell geeigneten Upper-Pool berücksichtigen.

Gewünschte Eigenschaften:

- Kleines, lokales Preislevel.
- Mindestens 2 einzigartige ACTIVE Origins (Depth 2).
- Pool liegt oberhalb des aktuellen Preises.
- Pool ist nicht bloß ein winziges Fragment einer dichten zusammenhängenden Upper-Zone.
- Zum nächsten separaten Upper-Preislevel besteht ein deutlicher freier Gap.
- Keine dichte Folge weiterer Upper-Pools unmittelbar darüber.

**Bestehende Mindest-Gap-Regel: 0,5 %.**

Unsere idealen Referenzbeispiele vom 09.09.2026 hatten 1,17 % und 1,73 % Upper-Gap. Ob ein größerer Mindestabstand sinnvoll ist, bleibt offen.

## 4. Dynamisches Pool-Verhalten

### SHORT OK

Der Upper-Pool ist isoliert und bleibt strukturell geeignet. Die Pending-Limit-Order darf entsprechend der bestehenden Order-Verwaltung aktiv bleiben.

### SHORT NO / Kandidat ungültig

Den Kandidaten erneut prüfen und gegebenenfalls stornieren, wenn:

- der Pool seine ACTIVE-/Depth-2-Eigenschaft verliert,
- er nicht mehr der nächste geeignete Upper-Pool ist,
- der ursprüngliche freie Gap verschwindet,
- der Pool Teil einer dichten Upper-Zone wird,
- sich Pool-Grenzen oder Origins so verändern, dass die Order strukturell nicht mehr passt,
- der Marktstatus zu SIDEWAYS oder UNCERTAIN wechselt.

Wachsende Pools, neue Upper-Pools darüber, mehrfache Tests und langes Seitwärtsverhalten gelten zunächst als **Warnmerkmale**, nicht als bereits validierte harte Schwellenwerte.

Die spätere schnelle Abwärtsreaktion darf niemals rückwirkend Voraussetzung für einen Entry sein.

## 5. Entry, Stop-Loss und Take-Profit

Die bestehenden Handelsregeln bleiben unverändert.

**Short-Entry:** Unterkante des ausgewählten kleinen Upper-Entry-Pools.

**Stop-Loss:** 0,2 % über der Oberkante desselben relevanten Entry-Preislevels.

**Take-Profit:** Bestehende Lower-Pool-Auswahlregel.

**Mindest-TP-Abstand:** 0,5 % von der tatsächlichen Entry-Position zur Oberkante des geeigneten Lower-TP-Pools.

**Upper-Gap:** Bestehende Mindestregel von 0,5 %.

Keine Verbindung des kleinen Entry-Pools mit weit entfernten Upper-Pools über echte Preis-Gaps.

## 6. Order-Verwaltung

- Maximal eine aktive Short-Pending-Order.
- Maximal eine offene simulierte Short-Position.
- Limit-Order erst nach abgeschlossenem 5m-Decision-Close planen.
- Aktivierung nach bestehendem kausalem Backtester-Zeitvertrag.
- Pending bei jedem neuen 5m-Close neu prüfen.
- Ungültige Pending-Order stornieren oder bei gerechtfertigtem Kandidatenwechsel ersetzen.
- Nach einem modellierten Fill keine weiteren Short-Entries bis zum Positionsende.
- Bereits offene Positionen mit ihren festgelegten SL-/TP-Werten verwalten.

Ein bloßer größerer Kursrückgang ist kein automatischer Cancel-Grund.

## 7. Sicherheitsregel nach Stop-Loss

**SHORT PAUSE**

Nach dem ersten Stop-Loss:

1. Neue Short-Entries sofort sperren.
2. Eventuell vorhandene unbestätigte Short-Kandidaten verwerfen.
3. EMA9, EMA20, EMA59 und EMA200 neu bewerten.
4. Upper-/Lower-Pool-Landschaft erneut untersuchen.
5. Seitwärtsmarkt, Trendverlust und mögliche Aufwärtsfortsetzung prüfen.
6. Neue Short-Orders erst nach einer erneuten bestätigten Downtrend-Situation erlauben.

Ein Stop-Loss bedeutet nicht automatisch, dass der Downtrend beendet ist. Er löst aber verbindlich eine erneute Marktprüfung aus.

Die genaue Regel für die erneute Freigabe muss noch historisch validiert werden. Keine willkürliche Minuten- oder Kerzensperre.

## 8. Unsere drei Entscheidungszustände

**SHORT OK**

Bestätigter Downtrend, geeigneter isolierter Upper-Pool, ausreichender Gap, gültiger TP und keine aktive Sperre.

**SHORT NO**

Seitwärtsmarkt, unklarer Trend, dichter Upper-Stack, fehlender Gap oder kein gültiger Entry-/TP-Pool.

**SHORT PAUSE**

Stop-Loss aufgetreten oder laufende erneute Marktprüfung. Keine neuen Short-Orders bis zur Freigabe.

## 9. Verbindliche Referenzbeispiele

**Gewünschte Short-Setups:**

- 09.09.2026, 16:30–17:25 UTC
- 09.09.2026, 20:55–21:25 UTC

**Problematische Strukturbeispiele:**

- 11.09.2026, 12:25 und 12:35 UTC: atomare Mini-Pools innerhalb beziehungsweise nahe dichter Upper-Preisbereiche.
- 04./05.09.2026: Seitwärtsphase, wachsende Upper-Pool-Landschaft und anschließender Aufwärtsausbruch.

## 10. Was noch wissenschaftlich geprüft werden muss

- Reicht die EMA-Staffelung für einen bestätigten Downtrend?
- Welche EMA-Steigungen sind aussagekräftig?
- Wie erkennen wir Seitwärtsmärkte ohne willkürliche Grenzen?
- Wie unterscheiden wir isolierte Pools von wachsenden Upper-Stacks?
- Wie bestimmen wir die Relevanz eines kleinen Preislevels kausal?
- Wann verschwindet ein zuvor gültiger Upper-Gap?
- Wie schnell muss eine Pending-Order bei Strukturänderung storniert werden?
- Unter welchen nachweisbaren Bedingungen endet SHORT PAUSE?

---

**Leitsatz der Strategie:**

*Nur im bestätigten Downtrend den nächsten isolierten kleinen Upper-Pool mit großem freien Gap darüber handeln. Bei Seitwärtsmarkt, Strukturverlust oder nach einem Stop-Loss neue Shorts sperren, bis die Bedingungen wieder bestätigt sind.*