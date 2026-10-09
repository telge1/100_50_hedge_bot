# Pool-State-Machine

Das Muster ist eine Abfolge, kein einzelner Treffer. Der Scanner liest zuerst den Kontext eines Coins, die Zustandsmaschine hält danach die aktuelle Phase.

## Phasen

1. `upper_build`
   Der Preis verlässt eine Basis und arbeitet sich durch einen oberen Pool oder einen oberen Stapel. Kleine Zwischenpools sind nur Staffel, keine eigene Phase.

2. `stair_down`
   Der obere Aufbau bricht. Unter dem Preis entstehen neue kleine Pools, die der Fall stufenweise abholt. EMA9, EMA20 und EMA59 drehen mit nach unten.

3. `exhausted`
   Die untere Treppe ist gelaufen. Das Tief hält, unter dem Preis entsteht keine neue Stufe mehr, und der Markt kann wieder in eine obere Struktur drehen.

4. `neutral`
   Keine der drei Phasen ist eindeutig.

## Was zählt

- Ein Cluster sind alle Pools mit einer Lücke unter 3 %.
- Ein alleinstehender Pool hat zum nächsten Pool mindestens 3 % Abstand.
- Dünne einzelne Bänder direkt unter dem Anstieg sind Zwischenstationen.
- Der relevante obere Pool ist der verschachtelte Stapel, nicht jeder Zwischenpool.
- Eine Lücke von rund 4,5 % zum nächsten oberen Pool braucht ein starkes Delta.

## Zeitfenster

- 4h ist die Hauptstruktur.
- 1h bestätigt den Übergang.
- 30m und 5m zeigen die feinen Stufen und die echte Wende.

## Halten

Eine Phase bleibt stehen, bis die Folge wirklich wechselt. Eine einzelne Kerze gegen die Phase reicht nicht.

- `upper_build` beginnt auf der Kerze, die von unter EMA200 die schnellen Bänder wirklich räumt. Ein Rücklauf, der nur einen Zwischenpool antippt, reicht nicht.
- `stair_down` beginnt, wenn dieser Aufbau die Basis verliert, EMA9 und EMA20 verloren sind, die Bänder nach unten drehen, der Abstand zum Hoch mindestens 6 % ist, und mindestens zwei untere Stufen liquidiert werden.
- `exhausted` beginnt, wenn das Tief die untere Zone erreicht und darauf hält. Unter dem Tief liegt kein eigener unterer Pool mit mindestens 3 % Abstand. Ein Pool, der zur selben Zone wie das Tief gehört, ist dieser Support und hält die Treppe nicht offen. Offen bleibt sie nur, wenn nach dem Tief noch ein relevanter unterer Pool mit mindestens 3 % Abstand über dem Tief liegt. Dünne Bänder zählen nicht. EMA9 steht dabei über EMA20, und zwei Kerzen schließen über EMA9 und EMA20.

## Verifizierte Fenster

- 23. Mai bis 6. Juni 08:00: Treppe, das Tief liegt in der Kerze 6. Juni 04:00 (Schluss 08:00, Tief 0,07757). Die Wende ist am 8. Juni 04:00 bestätigt.
- 24. Juni bis 1. Juli: tiefer Flush, Tief am 30. Juni, danach Exhaustion und Rücklauf.
- 19. August bis 25. August: sauberer Ausbruch in `upper_build`.
- 19. August bis 18. September: `upper_build` bleibt stehen. Der September-Rücklauf ist keine neue Treppe.

## Prüfung in Rotation

Drei Monate setzen die Phase auf. Der folgende Monat wird geprüft. Die Schwellen bleiben stehen. Kerzen nach dem Prüfende werden nicht geladen.

Ein Wechsel ist auffällig, wenn die Phase nur ein oder zwei 4h-Kerzen hält, die Treppe auf dem Tief dieser Phase beginnt, oder der Aufbau auf dem Hoch dieser Phase beginnt. Stichproben gelten diesen auffälligen Stempeln.

## Lernen pro Coin

Die Regel bleibt gleich. Die Schwellen kommen aus der Historie des jeweiligen Coins: Poolabstand, Clusterbreite, Rücksetzertiefe, EMA-Nachlauf. DOGE bekommt ein anderes Profil als ein ruhigerer Coin.
