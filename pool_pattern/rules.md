# Regel

Dieselbe Folge für jede Münze. 4h entscheidet die Phase. 15m und 1h bestätigen sie. 5m zeigt nur die feinen Schritte. Keines der feineren Fenster setzt eine eigene Phase und keines gibt ein Signal.

Die Grundlage steht in `docs/4h-pool-cluster.md`. Die Abstände 3 % und 4,5 % bleiben fest.

## Phase, 4h

Der Schluss einer 4h-Kerze zählt. Eine einzelne Kerze gegen die laufende Phase schaltet nicht um.

- `upper_build`, solange der Preis einen relevanten oberen Pool arbeitet. Kleine Pools auf dem Weg nach oben bleiben in dieser Phase. Sie sind Staffeln, keine eigene Phase.
- `stair_down` beginnt, nachdem der obere Pool geholt ist und der Schluss wieder darunter liegt, solange unter dem Preis noch untere Pools offen sind. Die Treppe wartet nicht, bis diese Pools schon liquidiert sind. Der Moment, in dem der Pool geholt wird, kann auf einer feineren Kerze innerhalb dieser 4h-Kerze liegen. Der 4h-Stempel ist ihr Schluss.
- Die Treppe läuft weiter, solange der Preis die unteren Pools stufenweise holt. Ein dicker oberer Pool über dem Preis ist dabei Widerstand, keine neue Phase. Ein einzelner schwacher Pool unter dem Preis beendet sie nicht.
- Ein weiterer unterer Pool, der noch innerhalb von 3 % unter dem ersten Ziel liegt, gehört zur selben Treppe. Ein Docht in die Oberkante dieser Zone beendet sie nicht.
- `exhausted`, wenn der Schluss am Boden des letzten unteren Pools hält und darunter kein eigener Pool mit 3 % Abstand mehr offen ist.
- Danach beginnt wieder `upper_build`, wenn der Schluss diesen letzten unteren Pool verlässt und in einem oberen Pool liegt.

Ein Stapel ist relevant, wenn mindestens drei Pools darin liegen oder sich Bänder überlappen. Ein einzelner Pool mit mindestens 3 % Abstand zum nächsten bleibt relevant. Ein schmales Band, das nur in der Lücke vor einem schwereren Pool liegt, zählt nicht. Die Intensität ist die Anzahl der Bänder auf demselben Preis.

Eine Lücke von mindestens 4,5 % zum nächsten oberen Pool wird nicht als genommen gezählt. Der Delta-Aufruf dafür kommt später.

## Bestätigung

`confirm_15m.annotate` prüft auf 15m und auf 1h dieselben vier Stunden nach dem 4h-Schluss. Die Phase bleibt stehen.

- `stair_down` ist bestätigt, wenn diese Schlüsse unter dem geholten oberen Pool bleiben. Holt eine laufende Treppe den oberen Pool erst auf einer späteren 4h-Kerze, gilt die Prüfung ab diesem Schluss.
- `upper_build` ist bestätigt, wenn der letzte Schluss über dem verlassenen unteren Pool liegt.
- `exhausted` ist bestätigt, wenn ein Tief die alte untere Zone antippt und der Schluss darüber hält.

5m wird mit derselben Prüfung gelesen, setzt aber nichts.

## Geprüft

DOGE, Juni 2026. Die 4h-Treppe hält ab dem Schluss 22. Juni 16:00, nachdem der obere Pool auf der 15m-Kerze um 14:00 geholt wurde. 5m, 15m und 1h bestätigen die folgenden vier Stunden. Der Anstieg ist am 1. Juli 20:00 bestätigt, über dem letzten unteren Pool. Am 21. Juni 12:00 bestätigen 5m und 15m die Treppe nicht; die 1h bleibt knapp darunter. Die 4h-Phase schaltet davon nicht zurück.
