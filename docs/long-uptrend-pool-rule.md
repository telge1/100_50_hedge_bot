# Long im Uptrend: Entry an vorbestehendem 1m-Pool

Diese Regel gilt nur für den **echten Spike / Momentum-Pump**. Wenn es nur ein normaler bullischer Trend ist, dann gilt die Regime-Long-Regel in `docs/long-regime-uptrend-rule.md`.

Der 5m-Chart ist nur die Übersicht. Den Einstieg suchen wir auf dem 1m-Chart.

## Wann die Regel gilt

Nur im **starken Long-Spike**, und nur wenn alle fünf Punkte gleichzeitig wahr sind. Fehlt einer, bleibt die Regel aus.

1. Das Regime ist **bullisch**: auf 1h oder 4h liegen EMA9 und EMA20 beide über EMA59.
2. Der Preis läuft im Spike nach oben. Die blauen Pools darunter werden als Support gehalten, nicht liquidiert.
3. Der Pool für den Entry war **schon vor dem Spike** sichtbar und ACTIVE. Zonen, die erst in der Spike-Kerze auftauchen, zählen nicht.
4. Der Preis kommt auf die **Oberkante genau dieses alten Pools** zurück.
5. Über dem Preis liegt zu diesem Zeitpunkt **noch kein roter Upper-Pool**. Der erscheint erst später und ist der Exit.

Das ist kein Long in einen ruhigen Pullback und kein Long in einen Pool, der erst mitten im Spike entstanden ist. In diesem Zustand suchen wir keinen Short.

## Entry

1. Nur ein **unterer Pool, der schon vor dem Impuls sichtbar und ACTIVE war**.
2. Pools, die erst im Spike auftauchen, zählen nicht.
3. Der Preis kommt auf die **Oberkante** dieses Pools zurück und hält sie.
4. Long-Einstieg bei **0,2 % über der Pool-Oberkante**.

## Stop

Stop immer bei **0,2 % unter der Pool-Unterkante**.

## Exit

Schließen, sobald **über dem Preis ein roter Upper-Pool sichtbar wird, der vorher noch nicht da war**.

Zum Entry-Zeitpunkt existieren diese oberen Pools noch nicht. Sie erscheinen erst später im Verlauf des Pumps und sind dann das Ziel bzw. der Exit.

## Kurz

| Punkt | Wert |
| --- | --- |
| Regime | bullisch (1h oder 4h) |
| Timeframe für den Entry | 1m |
| Pool | unterer Pool, schon vor dem Spike sichtbar |
| Entry | Pool-Oberkante + 0,2 % |
| Stop | Pool-Unterkante − 0,2 % |
| Exit | erster neuer roter Pool über dem Preis |
