# 4h-Pools: ein Cluster ist eine Zone

Stand: Beispiel DOGEUSDT, 29. April 2026, 00:00 UTC. Die gemessene Zone war Unterkante 0,10079 und Oberkante 0,10959.

## Ein einzelner Pool

Ein 4h-Pool kommt aus einer 4h-Kerze. Die halbe Spanne dieser Kerze ist die Pool-Höhe.

- Oberer Pool, über dem Preis: Unterkante ist das Hoch der Kerze, Oberkante ist dieses Hoch plus die halbe Spanne.
- Unterer Pool, unter dem Preis: Oberkante ist das Tief der Kerze, Unterkante ist dieses Tief minus die halbe Spanne.

## Ein Cluster

Mehrere solcher Pools, die sich überlappen oder nur eine kleine Lücke haben, sind **eine Zone**.

- Eine Lücke unter 3 % ist keine Trennung. Die 0,13-%-Lücke in diesem Beispiel (zwischen 0,10336 und 0,10349) ist irrelevant.
- Eine Lücke von 3 % und mehr trennt zwei Cluster.
- Die Chart-Regel mit 0,1 % Lücke zerschneidet diese Zone in zwei Cluster. Für die Definition hier gilt die 3-%-Lücke, und die Zone bleibt eine.

Mit 3 % Lücke ist das Beispiel ein Cluster aus acht Pools, von 0,10080 bis 0,10956. Das sind die äußeren Kanten der Engine. Die gemessenen Kanten 0,10079 und 0,10959 sind dieselbe Zone.

## Die acht Pools im Beispiel

Am 29. April 00:00 UTC stehen alle acht noch über dem Preis. Entstanden sind sie früher, nicht in dieser Kerze.

| Unterkante | Oberkante | entstanden | Ursprungs-Hoch |
| --- | --- | --- | --- |
| 0,10080 | 0,10179 | 27. Apr 04:00 | 0,10080 |
| 0,10094 | 0,10197 | 28. Apr 08:00 | 0,10094 |
| 0,10179 | 0,10290 | 18. Mär 04:00 | 0,10179 |
| 0,10216 | 0,10336 | 17. Apr 20:00 | 0,10216 |
| 0,10349 | 0,10539 | 16. Mär 16:00 | 0,10349 |
| 0,10427 | 0,10768 | 4. Mär 20:00 | 0,10427 |
| 0,10445 | 0,10650 | 17. Mär 04:00 | 0,10445 |
| 0,10618 | 0,10956 | 26. Feb 00:00 | 0,10618 |

Die vier jüngeren Pools vom 17. bis 28. April verlängern den Cluster nur nach unten bis 0,10080. Die Dichte sitzt in den älteren Bändern darüber.

## Alleinstehender Pool

Ein Pool ist alleinstehend, wenn der nächste Pool auf derselben Seite mindestens 3 % entfernt ist. Solche Pools sind relevante Reaktionsebenen. Der Preis gibt dort fast immer einen Rebound.

Beispiel, unterer Pool, gemessen am Chart:

- Pool 0,10447 bis 0,10535, entstanden 30. April 2026 20:00 UTC. Oberkante ist das Tief der Kerze 30. April 16:00 UTC (0,10535), Unterkante liegt eine halbe Spanne darunter.
- Der nächste untere Pool darunter endet an der Oberkante 0,10053 (Pool 0,09865 bis 0,10053, entstanden 29. April 20:00 UTC).
- Abstand von 0,10447 nach 0,10053: 3,77 %. Das ist eine echte Trennung, keine Cluster-Lücke.

## Intensität

An diesen acht Pools ist kein Stärkewert gespeichert. Die Intensität ist, wie viele Bänder denselben Preis gleichzeitig bedecken.

Im ganzen Cluster liegen höchstens drei Bänder übereinander. Die Ränder sind ein Band dick.

| Preisbereich | Bänder übereinander |
| --- | --- |
| 0,10080–0,10336 | 1 bis 2 |
| 0,10450–0,10535 | 3, die dichteste Stelle |
| 0,10620–0,10645 | 3, schmale zweite Stelle |
| 0,10770–0,10956 | 1 |

Die dichteste Stelle, 0,10450 bis 0,10535, ist die Verschachtelung vom 4. März (0,10427–0,10768), vom 16. März (0,10349–0,10539) und vom 17. März (0,10445–0,10650).

## Liquidation

Der Preis hat diesen Cluster komplett liquidiert.

- Die Kerze 29. April 04:00 UTC geht mit dem Hoch 0,10547 in den Cluster hinein. Der Close ist 0,10487. Dabei fallen die vier unteren Bänder weg, Invalidierung 04:00 UTC.
- Die Kerze 29. April 08:00 UTC öffnet bei 0,10487, Hoch 0,11209, Close 0,10962. Das Hoch und der Close liegen über der Cluster-Oberkante 0,10956. Dabei fallen die vier oberen Bänder weg, Invalidierung 08:00 UTC.

Nach dieser Kerze ist die Zone von 0,10080 bis 0,10956 nicht mehr über dem Preis.

## Treppe nach unten

Der Preis läuft zuerst in einen großen oberen Pool. Während der Preis steigt, legt jedes Tief einen unteren Pool unter den Preis. Fällt der Preis danach und die Rücksetzer nach oben bleiben klein, entstehen auf jedem dieser Tiefs weitere untere Pools. Der nächste Abwärtsimpuls holt genau diese frisch gelegten Pools. Das wiederholt sich stufenweise, bis der Preis den großen unteren Pool erreicht, der schon vorher unter dem ganzen Weg lag.

Beispiel DOGEUSDT, Abstieg auf den Pool bei 0,0969, Kerze 23. Mai 2026 04:00 UTC. Das Tief dieser Kerze ist 0,09692. Getroffen wird die alte untere Zone, die seit dem 23. bis 27. April steht, Oberkante 0,09751 und 0,09674. Das ist nicht der Pool, den die Kerze selbst erzeugt.

Die Stufen davor, jeweils ein Tief, ein neuer unterer Pool, dann die Liquidation durch den nächsten Abverkauf:

| Stufe | neuer unterer Pool | geholt |
| --- | --- | --- |
| 7. Mai, Tief 0,10666 | 0,10421–0,10666 | 18. Mai 04:00, Tief 0,10351 |
| 18. Mai 04:00 | 0,10178–0,10351 | 22. Mai 20:00 |
| 18. Mai 16:00, Tief 0,10244 | 0,10121–0,10244 | 23. Mai 00:00 |
| 20. Mai bis 21. Mai, kleine Rücksetzer | Pools 0,10116–0,10485 | 22. Mai 16:00 bis 23. Mai 00:00 |
| 23. Mai 04:00, Tief 0,09692 | alte Zone 0,09545–0,09751 | erreicht, nicht neu erzeugt |

## Großer oberer Pool und die Lücke darüber

Schwache einzelne Bänder direkt unter dem Anstieg zählen nicht. Der relevante obere Pool ist der verschachtelte Stapel. Endet der, und der nächste Pool liegt rund 4,5 % darüber, ist diese Lücke schwer zu nehmen. Dafür braucht es ein starkes Delta.

Beispiel DOGEUSDT, 4h, Kerze 14. Mai 2026 16:00 UTC. Das Hoch ist 0,11859.

Nicht relevant, der Preis nimmt sie auf dem Weg mit:

| Unterkante | Oberkante | warum schwach |
| --- | --- | --- |
| 0,11175 | 0,11248 | ein dünnes Band vom 11. Mai, am 13. Mai 04:00 schon weg. Die Kerze 13. Mai 00:00 hat das Hoch 0,11171 |
| 0,11280 | 0,11603 | ein Band vom 11. Mai, am 14. Mai 12:00 durchhandelt |

Ab 0,11459 liegen mehrere Bänder ineinander. Der Preis läuft in diesen Stapel hinein und bleibt darunter. Die Oberkante des Stapels ist 0,12123, gemessen 0,12117. Das sind die alten Bänder vom 30. Januar (0,11761–0,12047 und 0,11864–0,12124) und vom 15. Februar (0,11750–0,11933). Das Hoch 0,11859 sitzt in diesem Stapel, nicht darüber.

Der nächste relevante Pool darüber beginnt bei 0,12657, gemessen 0,12665. Von 0,12124 bis 0,12667 sind 4,48 %. Dazwischen liegt nur ein dünnes Band 0,12231–0,12319, das zählt nicht. Eine Lücke dieser Größe wird nicht nebenbei durchlaufen. Sie braucht ein starkes Delta.

## Ableitung dieser Sequenz

Dieselbe Folge kommt immer wieder. Der Anstieg holt den relevanten oberen Pool und stoppt in ihm, wenn der nächste Pool mehrere Prozent darüber liegt und kein starkes Delta da ist. Unter dem Preis liegen dann schon kleine untere Pools. Der Fall baut daraus eine Treppe: jedes kleine Hoch legt den nächsten unteren Pool, der nächste Abverkauf liquidiert ihn. Nach oben entsteht in dieser Phase kein neuer verschachtelter Pool. Ein einzelnes Band, das dabei auftaucht, ist schwach. Die EMA-Bänder drehen mit dem Preis nach unten. Die Sequenz endet am alten unteren Pool, der schon vor dem Anstieg dort lag.

Beispiel, 4h, 14. bis 23. Mai 2026. Am 14. Mai 16:00 liegt der Close 0,11609 über EMA9, EMA20, EMA59 und EMA200. Am 15. Mai 16:00 ist der Close schon unter EMA9. Am 18. Mai 04:00 liegt er unter EMA9, EMA20 und EMA59. Am 23. Mai 04:00, Tief 0,09692, liegt der Close 0,09883 auch unter EMA200. Das Ziel dieser Treppe ist die alte untere Zone bei 0,09545 bis 0,09751.

Dieselbe Folge eine Etage tiefer, 12. bis 30. Juni 2026. Das Hoch ist die Kerze 12. Juni 12:00, 0,09243. Daraus entsteht der obere Pool 0,09243–0,09534, der stehen bleibt. Die dünnen Bänder darunter, 0,085 bis 0,089, holt der Anstieg selbst. Unter dem Preis liegen die unteren Pools vom 9. bis 22. Juni, etwa 0,081 bis 0,088. Ab dem 16. Juni ist der Close unter EMA9, und EMA9, EMA20 und EMA59 liegen schon unter EMA200. Der Fall holt die Stufen: 16. bis 18. Juni die Zone um 0,084–0,088, 22. bis 23. Juni die um 0,081–0,083, 24. Juni 12:00 das Tief 0,07455, 30. Juni 12:00 das Tief 0,06942. Nach oben bleiben in dieser Zeit nur einzelne Bänder, kein neuer Stapel.

## Regel für die untere Treppe

Wenn der Preis in einen oberen Pool hineinläuft und danach unter dem Preis keine neuen oberen Cluster mehr entstehen, dann wird die Bewegung als **untere Treppe** gelesen.

Erkennungsregeln:

1. Der Preis hat zuerst einen relevanten oberen Pool geholt.
2. Auf dem Weg nach oben entstehen kleine neue Pools.
3. Nach der oberen Pool-Abholung im Preisfall werden diese neuen Pools stufenweise wieder liquidiert.
4. EMA9, EMA20 und EMA59 drehen mit dem Preis nach unten.
5. Sobald der Preis den letzten alten unteren Pool erreicht, ist die Treppe abgeschlossen.

Konsequenz:

- Solange die Treppe aktiv ist, ist der Markt weiter im Abverkauf.
- Ein einzelner schwacher Pool unter dem Preis ist noch kein Trendwechsel.
- Ein echter Wechsel kommt erst, wenn die untere Treppe leer ist und der Preis die alte tiefe Zone erreicht oder zurückerobert.

## Regel für die obere Stufenbildung

Wenn der Preis in einen ersten großen oberen Pool läuft, dann bauen sich auf dem weiteren Weg nach oben oft kleine neue Pools in Stufen auf.

Erkennungsregeln:

1. Der erste große obere Pool wird angefahren und zumindest teilweise abgearbeitet.
2. Danach läuft der Preis seitwärts oder leicht zurück und baut unter dem Preis neue kleine Pools auf.
3. Anschließend geht der Preis wieder hoch in den nächsten kleinen Pool.
4. Diese kleinen Pools bilden eine Staffel, bis der Preis den nächsten größeren Pool oder die nächste obere Widerstandszone erreicht.
5. Der eigentliche Abprallpunkt ist erst dort, wo die nächste starke Zone sitzt, nicht an jedem kleinen Zwischenpool.

Konsequenz:

- Kleine Zwischenpools sind oft nur Zwischenstationen.
- Relevant wird erst der nächste dickere oder klar verschachtelte obere Pool.
- Das Muster ist: **Pool holen, Zwischenpools bauen, weiter hoch, am nächsten großen Pool abprallen**.

## Kurz

| Punkt | Wert |
| --- | --- |
| Timeframe | 4h |
| Oberer Pool | Unterkante = Hoch der Ursprungskerze, Oberkante = Hoch plus halbe Spanne |
| Unterer Pool | Oberkante = Tief der Ursprungskerze, Unterkante = Tief minus halbe Spanne |
| Ein Cluster | alle Pools, deren Lücke unter 3 % liegt |
| Alleinstehend | nächster Pool mindestens 3 % entfernt, Rebound-Ebene. Beispiel: 3,77 % |
| Intensität | Anzahl der Bänder auf demselben Preis, hier maximal 3 |
| Liquidation | Close oder Hoch über der Cluster-Oberkante, hier 29. Apr 08:00 UTC |
| Treppe nach unten | steigender Preis legt untere Pools, der Fall holt sie stufenweise, Ziel ist die alte untere Zone. Beispiel: 23. Mai 04:00, Tief 0,09692 |
| Schwache obere Bänder | einzelne dünne Pools direkt unter dem Anstieg, hier 0,11175 und 0,11280 |
| Großer oberer Pool | verschachtelter Stapel, hier Oberkante 0,12123. Preis 14. Mai 16:00 nur bis 0,11859 |
| Lücke darüber | nächster relevanter Pool bei 0,12665, Abstand 4,48 %. Dafür braucht es ein starkes Delta |
| Sequenz | oberer Pool geholt, Treppe nach unten, keine neuen oberen Cluster, EMAs drehen mit. Ende am alten unteren Pool |
| Untere Treppe | neue untere Pools entstehen stufenweise, werden nacheinander liquidiert, bis die alte tiefe Zone erreicht ist |

## Technische Erkennungsregel

Das Muster soll im Code als **Zustandsfolge** erkannt werden, nicht als einzelne Kerze.

### Inputs

- 5m als Feinstruktur
- 15m und 1h als Bestätigung
- 4h als Hauptstruktur
- Pool-Cluster pro Zeitfenster
- EMA9, EMA20, EMA59, EMA200
- Abstand zwischen benachbarten Pools

### Zustände

1. **Upper-Cluster aktiv**
   - Der Preis läuft in einen relevanten oberen Pool.
   - Mehrere kleine Pools liegen darüber oder darin.
   - Der nächste größere obere Pool liegt deutlich weiter weg.

2. **Zwischenpools entstehen**
   - Der Preis läuft weiter oder pendelt leicht zurück.
   - Dabei entstehen kleine neue Pools.
   - Diese Pools sind erst einmal nur Zwischenstationen.

3. **Stufenbildung**
   - Der Preis arbeitet die kleinen Pools nacheinander ab.
   - Rücksetzer bleiben klein.
   - EMAs laufen mit und stützen die Bewegung.

4. **Treppe nach unten**
   - Der Preis verliert die obere Struktur.
   - Unter dem Preis entstehen neue kleine Pools.
   - Diese werden stufenweise liquidiert.

5. **Erschöpfung**
   - Kein neuer oberer Cluster mehr.
   - Der Preis erreicht die alte tiefe Zone.
   - Dann ist die Sequenz abgeschlossen.

### Übergänge

- `upper_cluster -> zwischenpools`, wenn der erste große obere Pool erreicht wird und der nächste große Pool nicht direkt mitgenommen wird.
- `zwischenpools -> stufenbildung`, wenn kleine neue Pools sichtbar werden und der Preis sie in Serie abarbeitet.
- `stufenbildung -> treppe_nach_unten`, wenn die obere Struktur bricht und die EMAs nach unten drehen.
- `treppe_nach_unten -> erschöpfung`, wenn der letzte alte untere Pool erreicht wird oder die tiefe Zone vollständig abgeholt ist.

### Code-Idee

- Nicht jede Kerze einzeln bewerten.
- Immer prüfen, ob der Preis in einer laufenden Pool-Folge steckt.
- Pools nach Seite, Alter und Cluster-Lücke gruppieren.
- Das Ergebnis als `upper_build`, `stair_down`, `exhausted` oder `neutral` ausgeben.
- Ein echtes Signal nur dann geben, wenn mehrere Timeframes dieselbe Phase bestätigen.
