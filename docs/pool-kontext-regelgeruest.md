# Pool-Kontext, Versuch

Dieses Blatt ist ein Versuch, den vorhandenen Pool-Kontext als Lageplan zu benutzen. Es enthält keine Orderblock- oder Delta-Bedingung. Es ist keine belegte Handelsregel und kein Nachweis, dass alte Pools Support oder Resistance sind.

Die Zahlen stammen aus der festen Kohorte von 126 Fällen in `results/pool_scan/context_study_v1/`. Gemessen wurde, was mit Pools passierte, die am Anfang eines bereits festgelegten 8%-Laufs aktiv waren. Das ist ein Kontext unter Überlebenden bis zum Fallbeginn, kein Signal für jeden beliebigen Zeitpunkt.

## Was die Auswertung trägt

- Der Abstand zur Bruchkante hängt in dieser Kohorte stärker mit einem späteren Bruch zusammen als das Alter allein.
- Jung und nah an der Bruchkante: häufig gebrochen bis zum Fallende. Auf 15m im nächsten Abstandsquartil 1593 von 1923 Zuordnungen.
- Alt und weit weg: fast kein Bruch. Auf 15m 3 von 1629 im ältesten und weitesten Quartil.
- Ein durchgehend fallender Bruchanteil über alle vier Altersquartile steht nur auf 15m im zweiten und dritten Abstandsquartil. In den anderen Abstandsbändern ist der Verlauf nicht monoton, oder eine Randzelle ist klein.
- Ein Pool, der nur aus dem Profil oder der Anzeige fällt, ist nicht gebrochen. Solche Pools sind kein Bezug für einen Einstieg.

## Kanten

Oben: die nahe Kante ist die Unterkante (`bottom_price`), die Bruchkante ist die Oberkante (`top_price`).

Unten: die nahe Kante ist die Oberkante (`top_price`), die Bruchkante ist die Unterkante (`bottom_price`).

Der Abstand zur Bruchkante in Prozent des Schlusses der letzten geschlossenen Kerze:

- oben: `(top_price - close) / close * 100`
- unten: `(close - bottom_price) / close * 100`

Das Alter in Kerzen zählt geschlossene Kerzen derselben Zeitebene nach `available_at` bis zu diesem Schluss.

## Rollen der Zeitebenen

- 4h bestimmt die Seite. Ohne eine klare 4h-Seite gibt es keinen Versuch.
- 30m wählt den Bezugspool auf dieser Seite. Er muss nah genug und noch aktiv in der Chart-Auswahl sein.
- 15m wählt nur den Zeitpunkt. Eine 15m-Kerze allein eröffnet keinen Versuch.

## Bänder aus der Kohorte

Das sind die Quartilgrenzen der vorhandenen Auswertung, keine allgemeinen Schwellen.

| Ebene | nah | mittel | weit | jung | alt |
|---|---|---|---|---|---|
| 4h | bis 19,2 % | über 19,2 bis 37,5 % | über 37,5 % | bis 128 Kerzen | über 250 Kerzen |
| 30m | bis 11,9 % | über 11,9 bis 21,5 % | über 21,5 % | bis 417 Kerzen | über 999 Kerzen |

„Nah“ ist das erste und zweite Abstandsquartil. „Weit“ ist das vierte. „Jung“ ist bis zum Altersmedian. „Alt“ ist das vierte Altersquartil.

## Seite auf 4h

Unter den 4h-Pools, die aktiv in der Chart-Auswahl sind und deren Bruchkante nah ist:

- Short-Lage, wenn die nahen aktiven Pools oben sind und mindestens einer davon jung ist.
- Long-Lage, wenn die nahen aktiven Pools unten sind und mindestens einer davon jung ist.
- Keine Lage, wenn beide Seiten einen nahen jungen Pool haben oder keine Seite einen hat.
- Ein alter und weiter Pool bleibt auf der Karte sichtbar. Er wählt die Seite nicht.

## Bezugspool auf 30m

Auf der von der 4h gewählten Seite genau ein Pool:

- aktiv in der Chart-Auswahl
- Bruchkante nah
- noch nicht gebrochen, `break_known_at` liegt nicht vor dem Zeitpunkt

Wenn mehrere 30m-Pools das erfüllen, ist der Bezug der mit dem kleinsten Abstand zur Bruchkante. Wenn keiner das erfüllt, kein Versuch. Ein Pool am Profilrand oder nur wegen der Anzeige-Obergrenze verschwunden ist kein Ersatz.

## Zeitpunkt auf 15m

Nur eine geschlossene 15m-Kerze zählt.

- Short: der 15m-Schluss liegt über der Oberkante des 30m-Bezugspools.
- Long: der 15m-Schluss liegt unter der Unterkante des 30m-Bezugspools.

Keine offene Kerze, kein nachträglich bestätigtes Extremum.

## Stop

Der Stop hängt an der Bruchkante des 30m-Bezugspools.

- Short: 0,2 % über der Oberkante. Preis der Kante mal 1,002.
- Long: 0,2 % unter der Unterkante. Preis der Kante mal 0,998.

Der Stop rückt nicht nach, weil ein Pool aus der Anzeige fällt. Er gilt, bis der Bezugspool durch diese Kante plus 0,2 % erledigt ist oder die Lage neu gelesen wird.

## Kein Versuch

- 4h und 30m zeigen nicht dieselbe Seite.
- Der nächste relevante Pool ist alt und weit.
- Der Kandidat ist nur nicht mehr sichtbar, ohne bekannten Bruch.
- Die 15m-Kerze hat die Bruchkante des 30m-Bezugs noch nicht geschlossen überschritten.
- Die Lage steht auf beiden Seiten gleichzeitig.

## Wie der Versuch gelesen wird

Ein Short in diesem Blatt ist der Versuch, die obere nahe Front mitzunehmen, nachdem der 15m-Schluss über ihrer Bruchkante liegt. Ein Long ist dasselbe nach unten.

Ein alter weiter Pool ist Orientierung auf der Karte. Er ist in dieser Kohorte selten die Stelle, an der bis zum Fallende der Bruch stattfand. Ihn nur deshalb als Umkehrpunkt zu handeln, trägt die Auswertung nicht.

## Treppen-Checkliste

Eine Down- oder Up-Treppe liegt erst vor, wenn nach dem Bruch eines wichtigen Pools mehrere kleine Folge-Pools in dieselbe Richtung entstehen.

### Down-Treppe

1. Ein wichtiger oberer Pool wird gebrochen.
2. Danach entstehen unter dem Preis neue kleine Pools.
3. Die neuen Pools liegen stufenartig tiefer.
4. Die Gegenseite bleibt leer oder deutlich schwächer.
5. Der Preis respektiert die alten oberen Kanten eher als dass er sie sofort sauber zurückerobert.

### Up-Treppe

1. Ein wichtiger unterer Pool wird gebrochen.
2. Danach entstehen über dem Preis neue kleine Pools.
3. Die neuen Pools liegen stufenartig höher.
4. Die Gegenseite bleibt leer oder deutlich schwächer.
5. Der Preis respektiert die alten unteren Kanten eher als dass er sie sofort sauber zurückerobert.

### Kein Treppenbild

Kein klares Treppenbild liegt vor, wenn:

- nur ein einzelner Pool gebrochen wird und danach nichts Folgehaftes entsteht
- die neuen Pools auf beiden Seiten gleichzeitig entstehen
- der Markt direkt in eine breite Seitwärtsphase kippt
- die Gegenseite sofort wieder neue, gleich starke Pools bildet

### Praktische Lesereihenfolge

1. 4h: Welche Seite ist strukturell dominant?
2. 30m: Welcher Pool ist der nächste relevante Bezugspool auf dieser Seite?
3. 15m: Hat die letzte geschlossene Kerze die Bezugskante schon bestätigt?
4. Wenn ja, dann erst die Treppe weiter lesen.
5. Wenn nein, ist es nur ein möglicher Vorläufer, noch keine Treppe.

## Kurzliste

1. 4h bestimmt die Seite.
2. 30m wählt den nächsten aktiven Bezugspool mit naher Bruchkante.
3. 15m bestätigt nur den Zeitpunkt.
4. Short erst nach Bruch der oberen Bezugskante, Long erst nach Bruch der unteren Bezugskante.
5. Stop immer 0,2 % hinter der Bruchkante.
6. Ein einzelner Bruch ist noch keine Treppe.
7. Eine grosse Luecke bis zum naechsten Pool ist selbst ein wichtiger Kontextfaktor und kann den TP oder das Halten der Position deutlich beeinflussen.
