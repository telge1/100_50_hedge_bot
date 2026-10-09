# Long im Uptrend: nur Long, wenn 1h oder 4h bullisch ist

Diese Regel gilt, wenn der Regime-Filter einen Uptrend erkennt. Das ist die **normale Long-Regel**.

Sie ist **nicht** die Spike-Regel. Der Spike-Long steht in `docs/long-uptrend-pool-rule.md`.
Hier warten wir vor dem Pool auf OB + Delta und kaufen den Support nach Reclaim oder sauberem Halt. Wir laufen dem Preis nicht hinterher.

## Regime

Bullisch, wenn auf **1h oder 4h** EMA9 und EMA20 beide über EMA59 liegen.

Dann suchen wir **nur noch Longs**. Short-Signale werden ignoriert.

## Was dieser Long ist

Das ist der Start des Moves, nicht der Einstieg mitten im Spike.

An diesem Zeitpunkt liegen die roten Upper-Pools **schon über dem Preis**. Der Preis läuft danach in sie hinein. Sie sind das Ziel. Sie sind kein Grund, den Long auszulassen, und sie sind nicht der Exit aus der Spike-Regel.

Den Start sieht man auf **15m und 1h**: letzte ruhige Stelle, bevor der Ausbruch kommt. Auf 1m und 5m sieht dieselbe Zeit noch wie Basis oder Rauschen aus.

## Wann der Long gesucht wird

Alle Punkte gleichzeitig:

1. Regime ist bullisch auf 1h oder 4h.
2. Der Preis steht an der **Unterkante eines unteren Pools**, der schon vorher sichtbar und ACTIVE war, oder am **EMA200-Band**.
3. Die blauen Pools unter dem Preis werden als Support gehalten, nicht liquidiert.
4. OB und Delta bestätigen den Halt. Der Long kommt bei der **stärksten OB- und Delta-Bestätigung** an dieser Stelle.
5. Über dem Preis existieren bereits rote Upper-Pools. Die sind das Ziel.

## Mikro-Pool unter dem Preis

Manchmal liegt direkt unter dem Preis noch ein **kleiner, riskanter Pool** knapp unter EMA59. Den traden wir nicht blind.

Regel:

1. Sobald der Preis ungefähr **0,3 % vor diesem Pool** steht, starten wir die **OB- und Delta-Analyse**.
2. **Bricht der Preis durch den kleinen Pool**, passiert nichts.
3. **Kommt sofort ein Reclaim zurück über den Pool** und bleibt OB + Delta danach positiv, dann kann der Long dort gesetzt werden.
4. Bleibt der Reclaim schwach oder kippt OB + Delta, warten wir weiter auf den **dicken Pool darunter**.

So ist der kleine Pool nur ein Frühwarnpunkt. Der eigentliche Long entsteht erst, wenn der Markt ihn hält und wieder zurückerobert.

## Stop

Stop immer bei **0,2 % unter der Unterkante** des Pools, an dem der Long steht.

## Trend halten

Solange EMA9, EMA20 und EMA59 über EMA200 bleiben, ist der Uptrend intakt. Auf 1h sind die EMAs in diesem Fall stark aufgefächert. Den Long dann nicht am ersten roten Pool der kleinen Timeframes schließen.

Bricht eine der schnellen EMAs durch EMA200, ist die Halte-Bedingung weg.

## 1h immer mit 4h vergleichen

1h ist die Zwischenstruktur, 4h ist das eigentliche Ziel.

Wenn 1h schon einen lokalen roten Pool oder eine kleine Widerstandszone zeigt, 4h aber noch ein höheres Ziel offen hat, dann wird **nicht** zu früh geschlossen.

Erst wenn auch die 4h-Zone erreicht ist oder der Trend klar kippt, wird der Long beendet.

## Ziel auf 4h

Das Ziel kommt von der **4h**, nicht von 1m oder 1h. Auf 1h ist oft nur ein Pool sichtbar. Auf 4h liegt darüber der zweite.

1. Die aktuelle 4h-Kerze steht direkt unter der Unterkante des **ersten** roten Pools. Der Preis läuft dort sehr wahrscheinlich hinein. Das ist ein Zwischenziel, kein Exit.
2. Erreicht der Preis den **zweiten** roten Pool darüber, ist dort der Ausstieg. An diesem Pool fällt der Preis.

## Exit

Schließen am **zweiten roten 4h-Pool** über dem Entry.

Nicht am nächsten roten Pool auf 1m, 5m oder 1h.

## Abgrenzung zum Spike-Long

| | Regime-Long | Spike-Long |
| --- | --- | --- |
| Zeitpunkt | Start des Moves, vor dem Ausbruch | mitten im laufenden Spike |
| Rote Pools über dem Preis | schon da; erster 4h-Pool wird angelaufen, Exit am zweiten | noch nicht da, der erste neue ist der Exit |
| Erkennung | 15m und 1h | 1m |
| Entry | untere Pool-Kante oder EMA200, stärkstes OB und Delta | 0,2 % über der Oberkante eines alten 1m-Pools |
| Stop | 0,2 % unter der Pool-Unterkante | 0,2 % unter der Pool-Unterkante |

## Kurz

| Punkt | Wert |
| --- | --- |
| Regime | bullisch auf 1h oder 4h |
| Richtung | nur Long |
| Erkennung | 15m und 1h, Basis vor dem Ausbruch |
| Ort | untere Pool-Kante oder EMA200, Pools halten als Support |
| Bestätigung | stärkstes OB und Delta |
| Trend halten | EMA9, EMA20 und EMA59 bleiben über EMA200 |
| Ziel | 4h: erster roter Pool wird angelaufen, Exit am zweiten roten Pool |
| Stop | Pool-Unterkante − 0,2 % |
