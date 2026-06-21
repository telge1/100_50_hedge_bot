📘 Strategie Abschnitt 1 — Start & Preplaced-Heal-Zyklus (Emergency-Bot)
1. Startbedingungen des Emergency-Bots

Der Emergency-Bot wird nur aktiviert, wenn mindestens eine der folgenden Bedingungen erfüllt ist:

Markt ist stark volatil
oder der bestehende Hedge hat bereits einen zu großen Spread

Nach Aktivierung wird sofort ein Hedge mit gleicher Größe aufgebaut:

long_qty  = X
short_qty = X
ratio = 1.0

Zusätzlich gilt:

spread_pct > 1.5%

Dieser Zustand ist der definierte Startpunkt des Bots.

2. Initiale Prüfung

Nach dem Start liest der Bot den aktuellen Zustand vollständig vom Exchange:

long_qty
long_avg
short_qty
short_avg
realized_long_pnl_total
realized_short_pnl_total
spread_pct
ratio
basket_pnl

Danach erfolgt die erste Prioritätsprüfung.

2.1 Basket-Exit

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Bot-Zyklus beenden

Wenn nicht, wird fortgefahren.

3. Aktivierung des Spread-Healing-Modus

Wenn:

spread_pct > 1.5%

Dann:

Spread-Healing wird aktiviert
Normal Flow wird vollständig pausiert
Bot arbeitet ausschließlich im Healing-Modus

Zustand:

state = SPREAD_HEALING oder SIZE_RESET_ONLY
4. Preplaced-Heal-Logik (Standard im Emergency-Bot)

Im Spread-Healing-Modus arbeitet der Bot nicht reaktiv, sondern proaktiv mit vorplatzierten Orders.

4.1 Grundprinzip

Es werden immer zwei Orders gleichzeitig gesetzt:

Untere Order (Long-Heal)
Typ: Buy Limit
Zweck: Verbesserung von long_avg
Position: unterhalb von short_avg
Obere Order (Short-Heal)
Typ: Sell Limit
Zweck: Verbesserung von short_avg
Position: oberhalb von long_avg

Beide Orders sind gleichzeitig aktiv.

5. Preisberechnung der Heal-Orders

Die Platzierung erfolgt relativ zu den aktuellen Averages:

long_heal_price  = short_avg * (1 - heal_offset_pct)
short_heal_price = long_avg  * (1 + heal_offset_pct)

Typischer Wert:

heal_offset_pct = 0.2% – 0.5%

Ziel:

keine sofortige Ausführung
nur echte Marktbewegungen triggern
klare Trennung von Struktur und Noise
6. Ordergröße (verbindlich)

Für jede Heal-Order gilt:

order_size = 20% der aktuellen Side-Size

Also:

Long-Heal: 0.20 * long_qty
Short-Heal: 0.20 * short_qty

Maximal:

max 3 Adds pro Seite pro Zyklus

Wichtiger Hinweis:

Die Erhöhung von 10% auf 20% ist bewusst gewählt, weil 10%-Adds bei einem bereits deutlich geöffneten Spread strukturell zu wenig Wirkung haben. 20%-Adds verbessern den jeweiligen Average deutlich schneller.

7. Heal-Zyklus (zentrale Logik)

Der Bot arbeitet in einem festen Loop:

Zustand: ARMED
beide Heal-Orders sind aktiv
Bot wartet auf Fill
8. Verhalten bei Fill (kritische Kernregel)

Sobald eine der beiden Heal-Orders gefillt wird:

Schritt 1 — Fill erkennen
Order wird als gefillt erkannt
genaue Fill-Daten werden übernommen
Schritt 2 — Gegenorder sofort canceln

Die nicht gefillte Heal-Order wird sofort entfernt.

Wichtig:

keine Verzögerung
kein Warten auf nächsten Tick
keine parallelen alten Orders erlaubt
Schritt 3 — Struktur neu einlesen

Der Bot liest den aktuellen Zustand vollständig neu vom Exchange:

long_qty
long_avg
short_qty
short_avg
Schritt 4 — Werte neu berechnen
spread_pct
ratio
basket_pnl
Schritt 5 — Exit prüfen

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Zyklus beenden
Schritt 6 — Healing weiter prüfen

Wenn:

spread_pct > 1.5%

Dann:

Healing bleibt aktiv
neue Heal-Orders werden gesetzt

Wenn nicht:

kein automatisches Ende der Logik
es wird geprüft, ob Reset / Gegenzug / Folgemodus nötig ist
Schritt 7 — REARM

Neue Orders werden auf Basis der aktuellen Averages gesetzt:

long_heal_price  = neuer_short_avg * (1 - heal_offset_pct)
short_heal_price = neuer_long_avg  * (1 + heal_offset_pct)

Dann zurück zu:

ARMED
9. Zentrale Sicherheitsregel

Es darf niemals passieren, dass:

alte Heal-Orders nach einem Fill aktiv bleiben
Heal-Orders auf veralteten Averages basieren
beide Seiten gleichzeitig ohne Revalidierung weiterlaufen
10. Ziel dieses Moduls

Der Preplaced-Heal-Zyklus dient dazu:

den Spread aktiv zu reduzieren
beide Averages strukturell zu verbessern
den Hedge stabil zu halten
den Trade wieder exitfähig zu machen
11. Kurzfassung

Wenn spread > 1.5%:

beide Heal-Orders setzen (oben Short, unten Long)
auf Fill warten
nach Fill:
Gegenorder canceln
Struktur neu laden
Werte neu berechnen
wenn weiterhin nötig:
neue Heal-Orders setzen
wiederholen
Exit sobald basket_pnl >= 0
Kernregel 2 — Reset nach erstem einseitigem Heal-Fill

Wenn eine Heal-Order gefillt wird und danach spread_pct <= 1.5%, aber ratio noch nicht wieder 1:1 ist, dann wird trotzdem noch eine neu berechnete Gegenorder gesetzt oder ein passender Folgemodus aktiviert, damit die andere Seite strukturell nachziehen kann.

Erst wenn sowohl:

der Spread wieder gesund
als auch die Size wieder strukturell akzeptabel

ist, gilt der Reset als vollständig abgeschlossen.

Kernregel 3 — Verhalten nach oberem Short-Heal-Fill

Wenn eine obere Short-Heal-Order gefillt wurde, gilt:

die Short-Position wurde vergrößert
short_avg wurde verbessert
der Spread wurde reduziert
die Ratio ist nicht mehr 1:1 (Short > Long)

Der Bot setzt danach eine neue untere Long-Heal-Gegenorder und erwartet zunächst einen Rücklauf.

Kernregel 4 — Oberer Heal-Block mit maximal 3 Short-Adds

Wenn der Preis nach einem oberen Short-Heal-Fill nicht zurückläuft, sondern weiter steigt, darf der Bot oben weitere Short-Heals setzen.

Diese obere Heal-Logik ist jedoch streng begrenzt:

Short-Add bei erstem Trigger
Short-Add bei weiterem +1%
Short-Add bei nochmals weiterem +1%

Danach gilt:

max_upper_short_heals = 3

Ein 4. oberer Short-Add ist nicht erlaubt.

Hintergrund

Mit bis zu 3 oberen Short-Adds à 20% kann der Spread sehr stark reduziert werden. Gleichzeitig wird die Struktur deutlich short-lastig. Deshalb ist nach dem 3. oberen Short-Add zwingend ein neuer Folgemodus nötig.

Kernregel 5 — Zustand nach starkem oberen Heal-Block

Nach einem starken oberen Heal-Block mit bis zu 3 Short-Adds kann der Zustand typischerweise so aussehen:

Spread deutlich reduziert oder fast vollständig geheilt
short_avg stark verbessert
short_qty deutlich größer als long_qty

Das heißt:

Spread ist nicht mehr das Hauptproblem
die Struktur ist jetzt aber stark short-lastig

Ab diesem Punkt darf der Bot nicht weiter stumpf oben Shorts adden.

Kernregel 6 — Wechsel in den Deleveraging-Modus bei weiter steigendem Preis

Wenn nach dem oberen Heal-Block der Preis weiter steigt, dann wechselt der Bot nicht in weitere aggressive Adds, sondern in einen kontrollierten Verkleinerungsmodus.

Dieser Modus dient dazu:

beide Seiten kontrolliert zu verkleinern
Risiko bei weiter steigendem Preis zu reduzieren
ohne direkten Nettoverlust zu arbeiten
Kernregel 7 — Ablauf des Deleveraging-Modus

Pro Zyklus gilt:

Schritt A

Der Bot schließt zunächst:

10% der aktuellen Short-Size
Schritt B

Wenn der Preis danach weitere

+0.45%

steigt, schließt der Bot:

dieselbe absolute Menge der Long-Position

Wichtig:

es wird nicht prozentual dieselbe Long-Quote geschlossen
sondern exakt dieselbe absolute Menge
dadurch wird die Struktur kontrolliert verkleinert
Kernregel 8 — Ziel des Deleveraging-Modus

Dieser Modus dient nicht primär der Ratio-Heilung.

Er dient dazu:

beide Positionen bei weiter steigendem Preis zu verkleinern
den Hedge kontrolliert abzubauen
die Struktur nicht durch weitere aggressive Adds zu verschlechtern
dabei möglichst ohne Nettoverlust zu arbeiten

Wichtige Eigenschaft:

der Spread bleibt bei reinen Reduktionen praktisch gleich
die Averages ändern sich durch reine Reduktionen nicht
der Modus ist daher ein Risikoreduktionsmodus, kein klassischer Healing-Modus
Kernregel 9 — Obergrenze für den Deleveraging-Modus

Der Deleveraging-Modus darf maximal:

4 Zyklen

ausgeführt werden.

Also:

max_deleveraging_cycles = 4

Danach gilt zwingend:

Stopp des Modus
vollständige Neubewertung der Struktur
neuer Entscheidungsmodus notwendig
Hintergrund

Ab mehr als 4 Zyklen würde der Long zu stark verbraucht werden. Dadurch würde der Bot in eine ungesunde Reststruktur kippen.

Kernregel 10 — Harte Neubewertung nach Ende des Deleveraging-Modus

Spätestens nach Zyklus 4 muss der Bot den gesamten Zustand vollständig neu einlesen:

long_qty
long_avg
short_qty
short_avg
spread_pct
ratio
basket_pnl

Danach wird:

ein neuer Referenzpunkt gesetzt
der alte Entscheidungszustand verworfen
ein neuer Modus gewählt

Ab diesem Punkt darf der Bot nicht einfach mit derselben Logik weiterlaufen.


📘 Strategie Abschnitt 1 — Start & Preplaced-Heal-Zyklus (Emergency-Bot)
1. Startbedingungen des Emergency-Bots

Der Emergency-Bot wird nur aktiviert, wenn mindestens eine der folgenden Bedingungen erfüllt ist:

Markt ist stark volatil
oder der bestehende Hedge hat bereits einen zu großen Spread

Nach Aktivierung wird sofort ein Hedge mit gleicher Größe aufgebaut:

long_qty  = X
short_qty = X
ratio = 1.0

Zusätzlich gilt:

spread_pct > 1.5%

Dieser Zustand ist der definierte Startpunkt des Bots.

2. Initiale Prüfung

Nach dem Start liest der Bot den aktuellen Zustand vollständig vom Exchange:

long_qty
long_avg
short_qty
short_avg
realized_long_pnl_total
realized_short_pnl_total
spread_pct
ratio
basket_pnl

Danach erfolgt die erste Prioritätsprüfung.

2.1 Basket-Exit

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Bot-Zyklus beenden

Wenn nicht, wird fortgefahren.

3. Aktivierung des Spread-Healing-Modus

Wenn:

spread_pct > 1.5%

Dann:

Spread-Healing wird aktiviert
Normal Flow wird vollständig pausiert
Bot arbeitet ausschließlich im Healing-Modus

Zustand:

state = SPREAD_HEALING oder SIZE_RESET_ONLY
4. Preplaced-Heal-Logik (Standard im Emergency-Bot)

Im Spread-Healing-Modus arbeitet der Bot nicht reaktiv, sondern proaktiv mit vorplatzierten Orders.

4.1 Grundprinzip

Es werden immer zwei Orders gleichzeitig gesetzt:

Untere Order (Long-Heal)
Typ: Buy Limit
Zweck: Verbesserung von long_avg
Position: unterhalb von short_avg
Obere Order (Short-Heal)
Typ: Sell Limit
Zweck: Verbesserung von short_avg
Position: oberhalb von long_avg

Beide Orders sind gleichzeitig aktiv.

5. Preisberechnung der Heal-Orders

Die Platzierung erfolgt relativ zu den aktuellen Averages:

long_heal_price  = short_avg * (1 - heal_offset_pct)
short_heal_price = long_avg  * (1 + heal_offset_pct)

Typischer Wert:

heal_offset_pct = 0.2% – 0.5%

Ziel:

keine sofortige Ausführung
nur echte Marktbewegungen triggern
klare Trennung von Struktur und Noise
6. Ordergröße (verbindlich)

Für jede Heal-Order gilt:

order_size = 20% der aktuellen Side-Size

Also:

Long-Heal: 0.20 * long_qty
Short-Heal: 0.20 * short_qty

Maximal:

max 3 Adds pro Seite pro Zyklus

Wichtiger Hinweis:

Die Erhöhung von 10% auf 20% ist bewusst gewählt, weil 10%-Adds bei einem bereits deutlich geöffneten Spread strukturell zu wenig Wirkung haben. 20%-Adds verbessern den jeweiligen Average deutlich schneller.

7. Heal-Zyklus (zentrale Logik)

Der Bot arbeitet in einem festen Loop:

Zustand: ARMED
beide Heal-Orders sind aktiv
Bot wartet auf Fill
8. Verhalten bei Fill (kritische Kernregel)

Sobald eine der beiden Heal-Orders gefillt wird:

Schritt 1 — Fill erkennen
Order wird als gefillt erkannt
genaue Fill-Daten werden übernommen
Schritt 2 — Gegenorder sofort canceln

Die nicht gefillte Heal-Order wird sofort entfernt.

Wichtig:

keine Verzögerung
kein Warten auf nächsten Tick
keine parallelen alten Orders erlaubt
Schritt 3 — Struktur neu einlesen

Der Bot liest den aktuellen Zustand vollständig neu vom Exchange:

long_qty
long_avg
short_qty
short_avg
Schritt 4 — Werte neu berechnen
spread_pct
ratio
basket_pnl
Schritt 5 — Exit prüfen

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Zyklus beenden
Schritt 6 — Healing weiter prüfen

Wenn:

spread_pct > 1.5%

Dann:

Healing bleibt aktiv
neue Heal-Orders werden gesetzt

Wenn nicht:

kein automatisches Ende der Logik
es wird geprüft, ob Reset / Gegenzug / Folgemodus nötig ist
Schritt 7 — REARM

Neue Orders werden auf Basis der aktuellen Averages gesetzt:

long_heal_price  = neuer_short_avg * (1 - heal_offset_pct)
short_heal_price = neuer_long_avg  * (1 + heal_offset_pct)

Dann zurück zu:

ARMED
9. Zentrale Sicherheitsregel

Es darf niemals passieren, dass:

alte Heal-Orders nach einem Fill aktiv bleiben
Heal-Orders auf veralteten Averages basieren
beide Seiten gleichzeitig ohne Revalidierung weiterlaufen
10. Ziel dieses Moduls

Der Preplaced-Heal-Zyklus dient dazu:

den Spread aktiv zu reduzieren
beide Averages strukturell zu verbessern
den Hedge stabil zu halten
den Trade wieder exitfähig zu machen
11. Kurzfassung

Wenn spread > 1.5%:

beide Heal-Orders setzen (oben Short, unten Long)
auf Fill warten
nach Fill:
Gegenorder canceln
Struktur neu laden
Werte neu berechnen
wenn weiterhin nötig:
neue Heal-Orders setzen
wiederholen
Exit sobald basket_pnl >= 0
Kernregel 2 — Reset nach erstem einseitigem Heal-Fill

Wenn eine Heal-Order gefillt wird und danach spread_pct <= 1.5%, aber ratio noch nicht wieder 1:1 ist, dann wird trotzdem noch eine neu berechnete Gegenorder gesetzt oder ein passender Folgemodus aktiviert, damit die andere Seite strukturell nachziehen kann.

Erst wenn sowohl:

der Spread wieder gesund
als auch die Size wieder strukturell akzeptabel

ist, gilt der Reset als vollständig abgeschlossen.

Kernregel 3 — Verhalten nach oberem Short-Heal-Fill

Wenn eine obere Short-Heal-Order gefillt wurde, gilt:

die Short-Position wurde vergrößert
short_avg wurde verbessert
der Spread wurde reduziert
die Ratio ist nicht mehr 1:1 (Short > Long)

Der Bot setzt danach eine neue untere Long-Heal-Gegenorder und erwartet zunächst einen Rücklauf.

Kernregel 4 — Oberer Heal-Block mit maximal 3 Short-Adds

Wenn der Preis nach einem oberen Short-Heal-Fill nicht zurückläuft, sondern weiter steigt, darf der Bot oben weitere Short-Heals setzen.

Diese obere Heal-Logik ist jedoch streng begrenzt:

Short-Add bei erstem Trigger
Short-Add bei weiterem +1%
Short-Add bei nochmals weiterem +1%

Danach gilt:

max_upper_short_heals = 3

Ein 4. oberer Short-Add ist nicht erlaubt.

Hintergrund

Mit bis zu 3 oberen Short-Adds à 20% kann der Spread sehr stark reduziert werden. Gleichzeitig wird die Struktur deutlich short-lastig. Deshalb ist nach dem 3. oberen Short-Add zwingend ein neuer Folgemodus nötig.

Kernregel 5 — Zustand nach starkem oberen Heal-Block

Nach einem starken oberen Heal-Block mit bis zu 3 Short-Adds kann der Zustand typischerweise so aussehen:

Spread deutlich reduziert oder fast vollständig geheilt
short_avg stark verbessert
short_qty deutlich größer als long_qty

Das heißt:

Spread ist nicht mehr das Hauptproblem
die Struktur ist jetzt aber stark short-lastig

Ab diesem Punkt darf der Bot nicht weiter stumpf oben Shorts adden.

Kernregel 6 — Wechsel in den Deleveraging-Modus bei weiter steigendem Preis

Wenn nach dem oberen Heal-Block der Preis weiter steigt, dann wechselt der Bot nicht in weitere aggressive Adds, sondern in einen kontrollierten Verkleinerungsmodus.

Dieser Modus dient dazu:

beide Seiten kontrolliert zu verkleinern
Risiko bei weiter steigendem Preis zu reduzieren
ohne direkten Nettoverlust zu arbeiten
Kernregel 7 — Ablauf des Deleveraging-Modus

Pro Zyklus gilt:

Schritt A

Der Bot schließt zunächst:

10% der aktuellen Short-Size
Schritt B

Wenn der Preis danach weitere

+0.45%

steigt, schließt der Bot:

dieselbe absolute Menge der Long-Position

Wichtig:

es wird nicht prozentual dieselbe Long-Quote geschlossen
sondern exakt dieselbe absolute Menge
dadurch wird die Struktur kontrolliert verkleinert
Kernregel 8 — Ziel des Deleveraging-Modus

Dieser Modus dient nicht primär der Ratio-Heilung.

Er dient dazu:

beide Positionen bei weiter steigendem Preis zu verkleinern
den Hedge kontrolliert abzubauen
die Struktur nicht durch weitere aggressive Adds zu verschlechtern
dabei möglichst ohne Nettoverlust zu arbeiten

Wichtige Eigenschaft:

der Spread bleibt bei reinen Reduktionen praktisch gleich
die Averages ändern sich durch reine Reduktionen nicht
der Modus ist daher ein Risikoreduktionsmodus, kein klassischer Healing-Modus
Kernregel 9 — Obergrenze für den Deleveraging-Modus

Der Deleveraging-Modus darf maximal:

4 Zyklen

ausgeführt werden.

Also:

max_deleveraging_cycles = 4

Danach gilt zwingend:

Stopp des Modus
vollständige Neubewertung der Struktur
neuer Entscheidungsmodus notwendig
Hintergrund

Ab mehr als 4 Zyklen würde der Long zu stark verbraucht werden. Dadurch würde der Bot in eine ungesunde Reststruktur kippen.

Kernregel 10 — Harte Neubewertung nach Ende des Deleveraging-Modus

Spätestens nach Zyklus 4 muss der Bot den gesamten Zustand vollständig neu einlesen:

long_qty
long_avg
short_qty
short_avg
spread_pct
ratio
basket_pnl

Danach wird:

ein neuer Referenzpunkt gesetzt
der alte Entscheidungszustand verworfen
ein neuer Modus gewählt

Ab diesem Punkt darf der Bot nicht einfach mit derselben Logik weiterlaufen.



Erweiterung: Preplaced Basket-Exit nach jeder Positionsänderung

Die Strategie arbeitet nicht nur mit Deleveraging-Zyklen, sondern berechnet nach jedem Fill sofort den neuen möglichen Gesamtkorb-Exit.

Deleveraging-Regel:
- 10% Short-Close
- +0.45% höher gleiche Long-Menge schließen
- maximal 4 Deleveraging-Zyklen

Pflicht nach jedem Fill:
- long_qty, long_avg, short_qty, short_avg neu vom Exchange lesen
- realized_long_pnl_total und realized_short_pnl_total aktualisieren
- neuen Basket-Break-Even berechnen
- sofort neuen Exit-Level auf Break-Even + 0.2% festlegen
- passende Reduce-Only Exit-Orders direkt vorplatzieren

Ziel:
Wenn der Preis nur kurz ansteigt, danach aber wieder zurückfällt, soll der Bot den vorbereiteten Basket-Exit bereits im Markt haben und nicht erst verspätet reagieren.

Long-lastige Struktur:
- vorbereitete Long-Close-SL
- vorbereitete Short-TP
- beide auf den Basket-Exit abgestimmt

Short-lastige Struktur:
- vorbereitete Short-Close-SL
- vorbereitete Long-TP
- beide auf den Basket-Exit abgestimmt

Grundprinzip:
Nach jeder Positionsänderung wird der Exit neu berechnet und neu vorbereitet.
Die Strategie ist damit jederzeit exit-ready.




📘 Kernregel 11 — Rebalance nach Zyklus 4 (Min-Order-Logik)

Nach Abschluss des Deleveraging-Modus (max. 4 Zyklen) erfolgt eine harte Neubewertung der Struktur.

Ab diesem Zeitpunkt gilt:

der aktuelle Zustand wird als neuer Referenzpunkt verwendet
der Deleveraging-Modus ist beendet
keine weiteren automatischen Reduktionszyklen sind erlaubt

Wenn der Preis danach weiter steigt, wird kein klassischer prozentualer Rebuy mehr verwendet, sondern eine technisch valide Mindest-Order-Logik.

📘 Kernregel 12 — Long-Rebuy mit Mindest-Order (7$)

Wenn nach Zyklus 4 der Preis vom neuen Referenzpreis aus um weitere +1.0% steigt, wird ein Long-Rebuy ausgelöst.

Berechnung
rebuy_qty_raw = long_qty * rebuy_pct
rebuy_notional = rebuy_qty_raw * current_price

Dann gilt:

wenn rebuy_notional >= 7$:
    rebuy_qty = rebuy_qty_raw
sonst:
    rebuy_qty = 7$ / current_price
📘 Kernregel 13 — Charakter des Rebuys

Dieser Rebuy ist:

kein Healing-Add
kein Deleveraging-Zyklus
sondern ein defensiver Rebalance-Schritt

Ziel:

die stark short-lastige Struktur leicht stabilisieren
ohne den Long-Average unnötig nach oben zu ziehen
ohne aggressives Nachkaufen
📘 Kernregel 14 — Begrenzung des Rebuys

Nach dem ersten Rebuy gilt:

max_rebuy_after_deleveraging = 1

Das bedeutet:

kein mehrfaches Nachkaufen
keine Rebuy-Ketten
danach immer:

👉 Neubewertung + Exit-Logik

📘 Kernregel 15 — Pflicht nach Rebuy

Nach jedem Rebuy muss der Bot sofort:

long_qty, long_avg neu berechnen
short_qty, short_avg einlesen
spread_pct neu berechnen
ratio neu berechnen
basket_pnl neu berechnen

Zusätzlich:

neuer Exit-Level = Break-Even + 0.2%

Und:

passende Reduce-Only Orders setzen
Hedge jederzeit exit-ready halten
📘 Kernregel 16 — Sicherheitslogik (kritisch)

Der Rebuy darf nur ausgeführt werden, wenn:

ratio < 0.55

Und:

spread_pct <= 1.0%

Wenn diese Bedingungen nicht erfüllt sind:

👉 kein Rebuy
👉 stattdessen Exit- oder Warte-Logik

🔧 WICHTIGE KORREKTUR IN DEINER DOKU

Du hast aktuell noch:

10% der aktuellen Short-Size schließen

👉 Das muss geändert werden zu:

5% der aktuellen Short-Size schließen

Sonst passt der ganze Flow nicht mehr zu euren neuen Berechnungen.

📘 OPTIONAL (empfohlen, aber nicht zwingend)
Dynamische Rebuy-Logik
wenn long_qty * rebuy_pct * price < 7$:
    nutze 7$ Mindestorder
sonst:
    nutze rebuy_pct


hier nach mussen wir festlegen das wenn der preis fallt ab wann wir dann wieder reagieren 


##################################################################


Hier ist die saubere, konsistente Tabelle von Start → oberer Heal-Block → Deleveraging → Zyklus 4 → +1% Rebuy, ohne Vermischung der Logik.

Ich halte strikt ein:

oberer Heal-Block → +1% Schritte
Deleveraging → +0.45% Schritte
danach neuer Modus → +1% → Long-Rebuy
📊 Vollständige Durchlauf-Tabelle
Phase 0 — Start (nach oberem Heal-Block)
Preis        = 103.0301
Long Qty     = 1.000000
Long Avg     = 100.0000
Short Qty    = 1.728000
Short Avg    = 99.7425
Spread       = 0.2575%
Ratio        = 0.5787
Phase 1 — Deleveraging
Zyklus 1
Preis        = 103.0301 * 1.0045 = 103.4947

Long Qty     = 0.827200
Short Qty    = 1.555200
Long Avg     = 100.0000
Short Avg    = 99.7425

Spread       = 0.2575%
Ratio        = 0.5319
Zyklus 2
Preis        = 103.4947 * 1.0045 = 103.9600

Long Qty     = 0.671680
Short Qty    = 1.399680

Spread       = 0.2575%
Ratio        = 0.4799
Zyklus 3
Preis        = 103.9600 * 1.0045 = 104.4268

Long Qty     = 0.531712
Short Qty    = 1.259712

Spread       = 0.2575%
Ratio        = 0.4221
Zyklus 4 (MAX erreicht)
Preis        = 104.4268 * 1.0045 = 104.8972

Long Qty     = 0.405741
Short Qty    = 1.133741

Long Avg     = 100.0000
Short Avg    = 99.7425

Spread       = 0.2575%
Ratio        = 0.3579

👉 Hier STOP (Kernregel 9 erfüllt)

Phase 2 — Neuer Modus (nach Neubewertung)
+1% Bewegung nach Zyklus 4
Preis        = 104.8972 * 1.01 = 105.9462
Long-Rebuy (10%)
Rebuy Qty    = 0.405741 * 0.10 = 0.040574
Neue Struktur nach Rebuy
Long Qty     = 0.446315
Short Qty    = 1.133741

Long Avg     = 100.5406
Short Avg    = 99.7425

Spread       = 0.8001%
Ratio        = 0.3937
📊 Kompakt als Gesamtübersicht
Step        Price     Long      Short     Spread   Ratio
--------------------------------------------------------
Start       103.0301  1.000000  1.728000  0.2575%  0.5787

Cycle 1     103.4947  0.827200  1.555200  0.2575%  0.5319
Cycle 2     103.9600  0.671680  1.399680  0.2575%  0.4799
Cycle 3     104.4268  0.531712  1.259712  0.2575%  0.4221
Cycle 4     104.8972  0.405741  1.133741  0.2575%  0.3579

+1% Move    105.9462

Rebuy       —         0.446315  1.133741  0.8001%  0.3937
🧠 Wichtige Erkenntnis (sehr wichtig für Strategie)
1. Deleveraging ist sauber
reduziert beide Seiten
verändert keine Averages
hält Spread konstant
reduziert Risiko

👉 genau wie geplant

2. Rebuy verändert die Struktur stark

Nach dem Rebuy:

Long steigt wieder
Short bleibt gleich
Spread springt hoch (0.25% → 0.80%)
Ratio verbessert sich leicht (0.35 → 0.39)

👉 das ist kein Healing, sondern ein Rebalance-Schritt



########################### noch ncht verifiziert #######################################################

Kernregel 17 — Rücklauf-Referenz nach Rebuy oder Neubewertung

Sobald nach Zyklus 4 entweder

eine harte Neubewertung abgeschlossen wurde
oder ein Long-Rebuy ausgeführt wurde

gilt der aktuelle Preis als neuer Rücklauf-Referenzpunkt.

recovery_reference_price = current_price

Ab diesem Punkt wird nicht mehr nach oben reagiert, sondern der Bot prüft aktiv, ob ein Rücklauf begonnen hat.

📘 Kernregel 18 — Erster Rücklauf-Trigger

Wenn der Preis nach dem gesetzten Referenzpunkt um mindestens

recovery_pullback_trigger_pct = 0.5%

fällt, dann wird der Rücklauf als echt genug gewertet, um wieder aktiv zu reagieren.

if current_price <= recovery_reference_price * (1 - 0.005):
    state = RECOVERY_PULLBACK_ACTIVE

Ziel:

nicht auf jeden kleinen Tick reagieren
aber auch nicht zu spät werden
einen echten beginnenden Rücklauf erkennen
📘 Kernregel 19 — Verhalten beim bestätigten Rücklauf

Wenn der Rücklauf-Trigger erreicht wurde, dann muss der Bot sofort den Exit-Pfad priorisieren.

Reihenfolge:

Schritt 1

Zustand vollständig neu vom Exchange lesen:

long_qty
long_avg
short_qty
short_avg
realized_long_pnl_total
realized_short_pnl_total
spread_pct
ratio
basket_pnl
Schritt 2

Neuen Basket-Exit prüfen

Wenn:

basket_pnl >= 0

Dann:

beide Positionen sofort schließen
Bot-Zyklus beenden
Schritt 3

Wenn Basket noch nicht exitfähig ist

Dann:

vorbereitete Reduce-Only Exit-Orders auf Basis von
Break-Even + 0.2%
aktualisieren
Hedge exit-ready halten
weiteren Rücklauf abwarten
📘 Kernregel 20 — Kein neuer Up-Mode während aktivem Rücklauf

Sobald ein bestätigter Rücklauf erkannt wurde, dürfen

keine neuen oberen Short-Heals
keine neuen Deleveraging-Zyklen
keine neuen Rebuy-Ketten

mehr gestartet werden.

Der Bot ist dann in einem reinen Exit-/Recovery-Modus.

Ziel:

nicht gleichzeitig in zwei Richtungen arbeiten
keinen Richtungswechsel mitten im Exit-Prozess erzeugen
📘 Kernregel 21 — Zweiter Rücklauf-Level für aggressiveren Exit

Wenn der Preis nach Aktivierung des Rücklauf-Modus weiter fällt und insgesamt

1.0% unter recovery_reference_price

liegt, dann wird der Exit aggressiver priorisiert.

Dann gilt:

Exit-Orders enger nachziehen
keine weitere Strukturverbesserung mehr versuchen
Fokus nur noch auf Gesamt-Schließung
if current_price <= recovery_reference_price * (1 - 0.01):
    exit_mode = AGGRESSIVE_EXIT
📘 Kernregel 22 — Rücklauf ungültig, wenn neues Hoch entsteht

Wenn nach gesetztem Referenzpunkt zunächst ein kleiner Rücklauf beginnt, der Preis danach aber wieder über den Referenzpunkt steigt, dann gilt der Rücklauf als nicht bestätigt.

Dann:

Rücklauf-Status zurücksetzen
neuen Referenzpunkt setzen
normale Entscheidungslogik erneut anwenden
if current_price > recovery_reference_price:
    recovery_pullback_state = RESET
    recovery_reference_price = current_price
📘 Kernregel 23 — Ziel des Rücklauf-Moduls

Dieses Modul dient dazu:

nach einer überdehnten Aufwärtsphase nicht planlos weiter zu reagieren
Rückläufe früh genug zu erkennen
den Hedge beim ersten sinnvollen Rücklauf wieder Richtung Exit zu führen
Gewinne nicht wieder durch Untätigkeit zu verlieren



Zwei wichtige Korrekturen in deiner aktuellen Doku

Diese Stellen sind noch veraltet und sollten ersetzt werden:

10% der aktuellen Short-Size schließen

muss zu

5% der aktuellen Short-Size schließen

werden.

Und:

Long-Rebuy (10%)

sollte nicht mehr als fester Wert in der Tabelle stehen, sondern als:

Long-Rebuy nach Min-Order-Logik (mindestens 7$ Notional)

weil ihr das inzwischen so entschieden habt.