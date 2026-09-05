Kurzfassung: Hedge-Bot mit Aufbau, Reduktion und Refill

Der Bot baut die Position über drei gute Bid-Level im Verhältnis 2:1 auf:

Bid 1 → kleine Position
Bid 2 → nachkaufen
Bid 3 → Zielgröße erreicht

Zielgröße:

Long  2.000 USDT
Short 1.000 USDT
Netto-Long 1.000 USDT

Nach jedem Fill werden alle Exit-Orders gelöscht, echte Mengen und Durchschnittspreise neu eingelesen und die Exits neu gesetzt.

Fällt der Preis nach Bid 3 weiter, wird nicht mehr nachgekauft, sondern proportional reduziert:

2.000/1.000
→ C1: 1.500/750
→ C2: 1.000/500
→ C3:   500/250
→ C4:   200/100

Das Verhältnis 2:1 und der fast gleiche Long-/Short-Average bleiben dabei erhalten. Nach C4 beträgt das Netto-Long-Risiko nur noch etwa 100 USDT.

Dann wartet der Bot auf einen bestätigten Wendepunkt:

Protected Low
+ starkes Bid-Level
+ Absorption
+ Reclaim
+ kein neuer bearish Strukturbruch

Erst dann wird tief wieder auf 2.000/1.000 aufgefüllt. Dadurch sinkt der Average deutlich. Der bisherige Verlust bleibt zwar bestehen, aber mit wieder 1.000 USDT Netto-Long kann ein kleiner Bounce den Gesamtverlust decken.

Der Exit wird nach dem Refill so berechnet, dass Folgendes gedeckt ist:

realisierte Verluste
+ offener Verlust
+ Gebühren
+ Funding
+ Exit-Kosten

Gesamtlogik:

über drei Bid-Level aufbauen
→ bei weiterer Schwäche bis C4 reduzieren
→ mit kleiner Position abwarten
→ erst nach bestätigtem Reclaim tief refillen
→ kleinen Bounce für Gesamt-Break-even nutzen




#############################################################################

Hedge-Aufbau mit Schutzmodus
1. Aufbau über drei Bid-Level

Die Position wird im Verhältnis 2:1 aufgebaut:

Bid 1: 400 Long / 200 Short
Bid 2: +600 Long / +300 Short
Bid 3: +1.000 Long / +500 Short

Nach Bid 3:

Long  2.000
Short 1.000
Netto-Long 1.000

Bei jeweils etwa 1,4 % Abstand zwischen den Bid-Levels liegt der gemeinsame Long-/Short-Average im Beispiel ungefähr bei:

98,18

Nach jedem Fill werden alte Exit-Orders gelöscht und mit den echten Mengen und Average-Preisen neu gesetzt.

2. Enge Schutzstufen C1 bis C3

C1 bis C3 liegen insgesamt nur etwa 0,5–0,75 % auseinander.

Beispiel:

C1: 96,73  → 1,47 % unter Avg
C3: 96,01  → 2,21 % unter Avg

Die Position wird so reduziert, dass der Short relativ immer stärker schützt:

Zustand	Long	Short	Netto-Long
nach Bid 3	2.000	1.000	1.000
C1	1.200	900	300
C2	600	480	120
C3	200	180	20

Ziel bei C3:

Long 200 / Short 180

Damit ist die Position fast neutral und ein weiterer Fall verursacht nur noch wenig zusätzlichen Verlust.

3. Temporärer Short-Schutz

Der größere Short ist nur ein Schutz während der Schwäche.

Long- und Short-Average bleiben nahezu gleich, weil nur Positionen reduziert und keine neuen Shorts eröffnet werden.

Steigt der Preis wieder zum Short-Average, wird der überschüssige Short nicht sofort komplett geschlossen, sondern in drei Stufen:

1. Nähe Short-Avg oder leicht darüber
   → 1/3 des überschüssigen Shorts schließen

2. 5m-Close über Short-Avg
   → weiteres Drittel schließen

3. Rücktest oder höheres Tief hält
   → letztes Drittel schließen

Danach ist die Position wieder ungefähr im normalen Verhältnis 2:1.

Fällt der Preis erneut unter den Average, werden alle weiteren Short-Reduktionen sofort gestoppt. Der verbleibende Short bleibt als Schutz bestehen.

4. Gesamtidee
über drei große Bid-Level auf 2.000/1.000 aufbauen
→ bei weiterem Fall innerhalb einer engen C-Zone schnell absichern
→ Long stärker reduzieren, Short größtenteils behalten
→ bei C3 nur noch 200/180 halten
→ weiteren Fall mit sehr kleinem Netto-Long überstehen
→ Schutz-Short bei bestätigtem Rücklauf stufenweise abbauen
→ später an einem bestätigten Boden tief refillen
→ Gesamtverlust mit dem anschließenden Bounce decken

Der wichtigste Vorteil ist:

Bei einem weiteren Fall wird das Risiko sehr schnell von 1.000 auf nur 20 USDT Netto-Long reduziert, ohne den Short-Average durch neue Short-Käufe zu verschieben