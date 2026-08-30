## Liquidity-Pool-OB-Recovery-Strategie

### Einstieg und Nachkäufe

* Start am aktuellen Preis mit gleich großem Long und Short, z. B. jeweils 100 USDT.
* Nachkäufe erfolgen ausschließlich an relevanten Liquidity Pools, die mit großen Orderbook-Walls übereinstimmen.
* Wandert eine Wall vor dem Fill tiefer, wird die offene Limit-Order entsprechend verschoben.
* Nach jedem bestätigten Rebuy wird der Long vergrößert und der Short auf 50 % der Long-Größe gesetzt.
* Beispiel: Long 2.700 USDT, Short 1.350 USDT.
* Nachkauf nur bei Wall-Defense, Absorption oder Reclaim – nicht bei einer konsumierten Wall.

### Crash-Option A: Beide Positionen um 50 % reduzieren

Beispiel beim Preis von 92 USDT:

* Long-PnL vollständig: −83,54 USDT
* Short-PnL vollständig: +42,71 USDT
* 50 % Long schließen: −41,77 USDT
* 50 % Short schließen: +21,36 USDT
* Netto vor Schließgebühr: −20,41 USDT
* Schließgebühr: ca. −1,08 USDT
* Realisierter Verlust: **ca. −21,49 USDT**

Ergebnis:

* ungefähr 50 % der Margin wird freigegeben,
* das verbleibende Kursrisiko wird halbiert,
* das freie Kapital kann an einem tieferen bestätigten Pool eingesetzt werden.

### Crash-Option B: Position vollständig einfrieren

Beim Preis von 92 USDT:

* Long: 28,4398 Coins, Durchschnitt 94,9375 USDT
* Short vorher: 14,2097 Coins, Durchschnitt 95,0057 USDT
* zusätzlicher Short: 14,2301 Coins beziehungsweise 1.309,17 USDT
* Short danach: 28,4398 Coins
* neuer Short-Durchschnitt: 93,5018 USDT

Spread nach dem Freeze:

* Long-Durchschnitt: 94,9375 USDT
* Short-Durchschnitt: 93,5018 USDT
* Spread: **1,4358 USDT beziehungsweise ca. 1,51 %**
* eingefrorener Kursverlust: **ca. −40,83 USDT**
* zusätzliche Short-Gebühr: ca. −0,72 USDT
* eingefrorener Verlust inklusive neuer Gebühr: **ca. −41,55 USDT**

Ergebnis:

* Long und Short besitzen dieselbe Coin-Menge.
* Weitere Preisbewegungen verändern den Gesamt-PnL kaum.
* Es wird keine Margin freigesetzt; der zusätzliche Short benötigt weitere Margin.
* An einem tieferen bestätigten Pool kann der Short teilweise mit Gewinn geschlossen und der Long wieder aktiviert werden.

### Grundregel

Option A schafft Kapital für tiefere Nachkäufe, realisiert aber einen Teilverlust.
Option B stoppt das weitere Kursrisiko, bindet jedoch zusätzliches Kapital.


#############################################################################################

## Regel für den letzten Liquidity Pool

Die letzte Stufe ist kein automatischer DCA-Nachkauf, sondern ein bestätigter Reversal-Einstieg.

* An den vorherigen Pools wird die normale Long-/Short-Recovery-Logik verwendet.
* Am letzten Pool wird zunächst nur eine kleine Long-Probe von 10–20 % gesetzt.
* Die restliche große Long-Order wird erst nach Bid-Wall-Defense, Absorption und bestätigtem Reclaim eröffnet.
* Wandert oder verschwindet die Wall, werden offene Long-Orders storniert.
* Wird die Wall konsumiert und scheitert der Reclaim, erfolgt kein weiterer Long-Nachkauf.
* Stattdessen wird der vorhandene Short von 50 % auf 75–100 % erhöht.
* Unter dem letzten Pool sind weitere DCA-Nachkäufe verboten.

Grundregel:

**Wall hält und Preis reclaimt → letzte Long-Stufe aktivieren.
Wall bricht und Reclaim scheitert → Long stoppen und Short-Hedge erhöhen.**



##############################################################################################

Professionelle Option C: Dynamischer Hedge statt sofortiger Freeze

Der Short wird bei einem Wall-Break schrittweise erhöht:

Marktsituation	Short relativ zum Long
Bid-Wall hält	50 %
Wall wird schwächer	60–65 %
Wall wird konsumiert	75–80 %
Failed Reclaim	90 %
bestätigter Crash	100 % Freeze

Vorteil:

Bei einem kurzen Fake-Break bleibt noch Rebound-Potenzial.
Bei einem echten Crash wird das Delta zunehmend neutralisiert.
Wir müssen nicht sofort eine riesige zusätzliche Short-Position eröffnen.
Der Hedge reagiert auf die tatsächliche Mikrostruktur.

Das wäre meine bevorzugte Lösung.

Option D: Nur die letzte aggressive Stufe zurücknehmen

Statt beide Gesamtpositionen um 50 % zu reduzieren, reduzieren wir hauptsächlich den letzten großen Long-Nachkauf.

Beispiel:

letzter Long-Rebuy bei 94 $: 1.800 USDT
diese Stufe hat die Positionsgröße stark erhöht
Wall bricht und der Preis fällt weiter
wir schließen beispielsweise 50 % dieser letzten Stufe
der ältere Kern bleibt bestehen
der profitable Short finanziert einen Teil dieses Verlustes

Das ist professioneller als pauschal den ganzen Long und Short zu halbieren, weil die zuletzt eröffnete Stufe meistens das schlechteste Chance-Risiko-Verhältnis besitzt, sobald ihre Wall scheitert.

Option E: Stop-and-Reseed

Wenn der letzte erwartete Pool eindeutig bricht:

Basket bei einem vorher festgelegten Maximalverlust schließen.
Keine weiteren Nachkäufe während des Crashs.
Neue Bodenbildung abwarten.
Erst bei neuer Bid-Wall, Absorption und Reclaim mit kleiner Startgröße neu beginnen.

Das realisiert zwar den Verlust, verhindert aber, dass wir eine immer größere alte Position mitschleppen. Professionelle Systeme akzeptieren lieber einen kontrollierten Verlust, als unbegrenzt Margin nachzulegen.

Meine empfohlene Kombination

Ich würde einen dreistufigen Crash-Schutz verwenden:

Stufe 1: Defense unsicher
keine weitere Long-Vergrößerung,
offene Limit-Orders stornieren,
Short von 50 % auf etwa 65 % erhöhen.
Stufe 2: Wall konsumiert und Reclaim scheitert
letzten Long-Rebuy teilweise reduzieren,
Short auf 80–90 % erhöhen,
auf den nächsten Pool warten.
Stufe 3: Letzter geplanter Pool bricht

Dann entscheidet eine vorher definierte Kapitalregel:

genügend Margin und neue tiefere Struktur sichtbar: 100-%-Freeze,
Margin soll freigesetzt werden: proportionale Reduktion,
Markt im echten Crash-Regime: vollständiger Stop-and-Reseed.
Noch wichtiger: Startgröße rückwärts planen

Die beste Crash-Lösung beginnt vor dem ersten Einstieg. Wir bestimmen:

wie viele relevante Pools unter dem Preis liegen,
welche maximale Gesamtposition erlaubt ist,
wie viel Crash-Reserve unberührt bleiben muss,
welcher maximale Verlust akzeptiert wird.

Dann rechnen wir die Startgröße rückwärts. Wenn unser Positionslimit 4.050 USDT beträgt, dürfen wir nicht mit 500 USDT starten und hoffen, dass genügend Kapital bleibt. Die Startgröße muss so klein sein, dass alle vorgesehenen Pool-Stufen und der Crash-Hedge bereits finanziert sind.

Mein professionelles Urteil

Die Strategie sollte nicht nur zwei Notfallknöpfe haben:

A: Position halbieren,
B: vollständig einfrieren.

Die robustere Lösung ist:

Short-Hedge dynamisch von 50 % auf 65 %, 80 %, 90 % und schließlich 100 % erhöhen, während gleichzeitig die zuletzt eröffnete Long-Stufe zuerst reduziert wird.

Dadurch schützen wir das Konto früher, behalten bei kleinen Rebounds aber noch Gewinnpotenzial. Der vollständige Freeze oder die 50-%-Reduktion bleibt nur der letzte Circuit Breaker – nicht die erste Reaktion auf fallende Preise.