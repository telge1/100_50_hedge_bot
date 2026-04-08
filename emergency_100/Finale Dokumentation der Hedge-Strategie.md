Finale Dokumentation der Hedge-Strategie
1. Ziel der Strategie

Diese Strategie verwaltet einen gleichzeitigen Long- und Short-Hedge so, dass:

Gewinne auf der starken Seite schrittweise realisiert werden
die schwache Seite nicht unkontrolliert aus dem Ruder läuft
ein zu großer Spread zwischen long_avg und short_avg aktiv geheilt wird
der gesamte Trade als Gesamtkorb geschlossen wird, sobald Break-Even oder Profit erreicht ist

Die Strategie ist nicht darauf ausgelegt, einzelne Seiten isoliert zu optimieren.
Sie bewertet immer die Gesamtstruktur aus:

Long-Position
Short-Position
realisierten Gewinnen/Verlusten
aktuellem offenen PnL beider Seiten
Spread
Ratio
2. Grundprinzip

Die Strategie arbeitet in drei Ebenen:

Ebene A: Normal Flow

Bei echten Marktbewegungen werden auf der Gewinnerseite Teilgewinne gesichert.

Ebene B: Recovery / Failover

Wenn ein erwarteter Pullback ausbleibt, wird die Offside-Seite defensiv reduziert, um die Struktur nicht eskalieren zu lassen.

Ebene C: Spread-Healing

Wenn der Abstand zwischen long_avg und short_avg zu groß wird, wird die schlechtere Seite gezielt verbessert.

3. Feste Parameter
Marktstruktur
Trigger nur bei echten 1%-Bewegungen
Keine Aktionen auf kleine Noise-Bewegungen
Adds
Maximal 3 Adds pro Seite und Zyklus
Jedes Add = 10% der aktuellen Side-Size
Spread-Healing
Aktiv ab:
spread > 1.5%
Short-Healing
Erst erlaubt, wenn:
price >= short_avg * 1.01
Long-Healing
Nur erlaubt, wenn:
price < long_avg
Exit
Gesamtkorb schließen, wenn:
basket_pnl >= 0

Optional:

kleiner Profit-Exit möglich, z. B.:
basket_pnl >= target_profit_usd
4. Definitionen
4.1 Spread
spread_pct = abs(long_avg - short_avg) / long_avg * 100

Der Spread misst den strukturellen Abstand zwischen Long-Avg und Short-Avg.

Wichtig:

Reine Reduktionen ändern den Avg nicht
Reine Reduktionen ändern also den Spread nicht
Nur Adds / Rebuilds / Healing-Aktionen können den Spread verändern
4.2 Ratio
ratio = long_qty / short_qty

Interpretation:

ratio = 1.0 → gleiche Size
ratio > 1.0 → Long ist größer
ratio < 1.0 → Short ist größer

Wichtig:

100:100 Size bedeutet nur Größen-Gleichgewicht
100:100 bedeutet nicht automatisch, dass die Struktur gesund ist
Wenn der Spread noch groß ist, ist kein echter Reset erreicht
4.3 Basket-PnL
realized_pnl_total = realized_long_pnl_total + realized_short_pnl_total

unrealized_long_pnl  = (current_price - long_avg) * long_qty
unrealized_short_pnl = (short_avg - current_price) * short_qty

basket_pnl = realized_pnl_total + unrealized_long_pnl + unrealized_short_pnl

Die Strategie bewertet den Exit immer über den Gesamtkorb.

Nicht relevant ist:

ob Long einzeln im Gewinn ist
ob Short einzeln im Verlust ist

Relevant ist nur:

ob die Summe aus realisierten und offenen Ergebnissen wieder bei 0 oder im Plus liegt
5. Zustände der Strategie
5.1 NORMAL_FLOW

Das ist der Standardmodus, solange kein strukturelles Problem vorliegt.

Regeln
Bei starkem Up-Move:
Gewinnerseite Long schrittweise reduzieren
Bei starkem Down-Move:
Gewinnerseite Short schrittweise reduzieren
Ziel
Gewinne sichern
Struktur nicht sofort mit Adds verkomplizieren
5.2 WAIT_PULLBACK

Nach einer Teilreduktion wird auf eine bestätigte Gegenbewegung gewartet.

Regeln
Keine hektischen Adds
Kein blindes Umschalten
Rebuild nur nach bestätigtem Gegenereignis
Ziel
die zuvor reduzierte Seite günstiger wieder aufbauen
5.3 NO_PULLBACK_FAILOVER

Wenn die erwartete Gegenbewegung ausbleibt und der Markt stattdessen weiter trendet.

Regeln
Nach Long-Reduktionen

Wenn der Preis weiter steigt und kein Pullback kommt:

Short-Seite defensiv reduzieren
Nach Short-Reduktionen

Wenn der Preis weiter fällt und kein Rebound kommt:

Long-Seite defensiv reduzieren
Ziel
Schadensbegrenzung
Offside-Hedge entlasten
nicht auf einen Pullback hoffen, der nicht kommt

Wichtig:

Diese Reduktion ist kein Profit-Move
Sie ist ein Risk-Reduction-Move
5.4 SPREAD_HEALING

Dieser Modus wird aktiviert, wenn die Struktur zu weit auseinanderläuft.

Aktivierung
if spread_pct > 1.5:
    spread_healing = true
5.5 SPREAD_HEAL_LONG

Long-Healing ist nur dann erlaubt, wenn ein Long-Add den long_avg wirklich verbessert.

Bedingung
price < long_avg
Aktion
Long +10% der aktuellen Long-Size
maximal 3 Adds in diesem Healing-Zyklus
Wirkung
long_avg sinkt
Spread kann kleiner werden
Nicht erlaubt
Long-Add oberhalb long_avg
denn das würde long_avg verschlechtern oder nicht sinnvoll verbessern
5.6 SPREAD_HEAL_SHORT

Short-Healing ist nur dann erlaubt, wenn ein Short-Add den short_avg wirklich verbessert.

Bedingung
price >= short_avg * 1.01
Aktion
Short +10% der aktuellen Short-Size
maximal 3 Adds in diesem Healing-Zyklus
Wirkung
short_avg steigt
Spread kann kleiner werden
Nicht erlaubt
Short-Add unterhalb short_avg
denn das würde short_avg weiter senken und den Spread verschlechtern
5.7 WAIT / NO ACTION

Wenn Spread-Healing aktiv ist, aber keiner der sauberen Trigger erfüllt ist:

Regeln
keine Aktion
nur warten
Typische Fälle
Preis steigt leicht, ist aber noch nicht hoch genug für Short-Healing
Preis fällt leicht, aber es gibt keinen sauberen neuen Healing-Schritt
Richtungswechsel ist unklar
Ziel
kein Overtrading
kein ständiges Hin-und-Her zwischen Long- und Short-Healing
5.8 SIZE_RESET_ONLY

Wenn Long- und Short-Qty wieder ähnlich oder gleich sind, aber der Spread noch zu groß ist.

Beispiel
long_qty ≈ short_qty
spread > 1.5%
Bedeutung
Size ist wieder balanciert
Struktur ist aber noch nicht gesund
es darf noch kein neuer Normalzyklus starten
5.9 FULL_RESET_READY

Ein neuer sauberer Zyklus ist erst erlaubt, wenn die Struktur wieder gesund ist.

Praktisch heißt das:

Size wieder balanciert
Spread wieder akzeptabel
oder Basket-Exit ist bereits möglich
6. Trigger-Regeln
6.1 Marktstruktur-Trigger

Es werden nur echte Bewegungen ab 1% gehandelt.

Up-Move
Preis steigt ≥ 1% über das letzte relevante Hoch oder die relevante Strukturreferenz
Down-Move
Preis fällt ≥ 1% unter das letzte relevante Tief oder die relevante Strukturreferenz
6.2 Last Relevant High / Low

Es wird immer nur mit dem letzten bestätigten strukturellen Hoch/Tief gearbeitet.

Nicht gültig sind:

Durchschnitte
beliebige Zwischenwerte
unbestätigte kleine Bounces
6.3 Event-Kette

Eine Folgeaktion darf nur nach gültigem vorherigem Ereignis kommen.

Beispiel:

erst Breakout
dann Referenzhoch gesetzt
dann erst Pullback-Trigger erlaubt

Oder:

erst Down-Move
dann Referenztief gesetzt
dann erst Rebound erlaubt
6.4 Einmalige Trigger

Pro Strukturereignis darf eine Aktion nur einmal feuern.

Erst wenn ein neues Gegenereignis die Referenz aktualisiert, wird der nächste Trigger wieder freigegeben.

6.5 Priorität
Hauptaktion zuerst
nie Add und Close im selben Tick
Guards filtern nur, ob eine Aktion erlaubt ist
Guards sind nicht der primäre Auslöser
7. Add-Regeln
Pro Seite
maximal 3 Adds pro Zyklus
jedes Add = 10% der aktuellen Side-Size
Beispiel

Startgröße 100

Add 1: 100 -> 110
Add 2: 110 -> 121
Add 3: 121 -> 133.1

Also nach 3 Adds:

133.1% der ursprünglichen Größe

Das gilt identisch für Long und Short.

8. Rebuild-Regeln
Nach Long-Reduktionen

Wenn ein sauberer Pullback kommt:

reduzierte Long-Teile können wieder aufgebaut werden
Nach Short-Reduktionen

Wenn ein sauberer Rebound kommt:

reduzierte Short-Teile können wieder aufgebaut werden

Wichtig:

Rebuild nur nach bestätigtem Gegenereignis
kein blindes Wiederaufbauen ohne Struktur
9. Spread-Healing-Logik, endgültig
Wenn spread > 1.5%

Dann gelten nur diese drei Regeln:

Regel 1

Wenn:

price < long_avg

Dann:

Long-Healing erlaubt
Regel 2

Wenn:

price >= short_avg * 1.01

Dann:

Short-Healing erlaubt
Regel 3

Sonst:

Warten
keine Aktion

Das ist die feste Endlogik für Spread-Healing.

10. Exit-Logik
10.1 Grundregel

Alle realisierten Gewinne und Verluste werden während des gesamten Trades gespeichert.

Diese werden laufend mit dem offenen Long- und Short-PnL verrechnet.

Formel
basket_pnl = realized_pnl_total + unrealized_long_pnl + unrealized_short_pnl
10.2 Exit-Bedingung

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Trade vollständig beenden

Optional:

basket_pnl >= target_profit_usd

Dann:

beide Positionen mit kleinem Gewinn schließen
10.3 Bedeutung

Healing ist nicht das Ziel.
Healing ist nur das Mittel, um die Struktur so weit zu verbessern, dass ein späterer Basket-Exit möglich wird.

Die Strategie zielt also nicht auf ewiges Management ab, sondern auf:

Struktur verbessern
Korb wieder exitfähig machen
dann alles schließen
11. Was die Strategie ausdrücklich nicht tut
kein Long-Healing oberhalb long_avg
kein Short-Healing unterhalb short_avg
kein Short-Healing vor 1% über short_avg
kein neuer Zyklus nur wegen 100:100 Size
kein Add und Close im selben Tick
kein Exit auf Basis nur einer einzelnen Seite
kein blindes Umschalten bei kleinen Bounces
12. Beispielhafte Kernlogik in Klartext
A. Normale Bewegung
Markt steigt stark → Long reduzieren
Markt fällt stark → Short reduzieren
B. Danach
auf Pullback / Rebuild warten
C. Wenn Pullback ausbleibt
Offside-Seite defensiv reduzieren
D. Wenn Spread zu groß wird
price < long_avg → Long-Healing
price >= short_avg * 1.01 → Short-Healing
sonst warten
E. Immer parallel
realisierte Gewinne/Verluste speichern
Basket-PnL laufend berechnen
F. Exit
sobald basket_pnl >= 0
beide Positionen schließen
13. Endgültige Kurzfassung
Finale Hedge-Strategie

1. Trigger nur bei echten 1%-Moves
2. Bei Up-Move: Long reduzieren
3. Bei Down-Move: Short reduzieren
4. Danach auf Pullback/Rebuild warten
5. Wenn kein Pullback kommt: Offside-Seite defensiv reduzieren
6. Wenn spread > 1.5%:
   - price < long_avg -> Long-Healing
   - price >= short_avg * 1.01 -> Short-Healing
   - sonst warten
7. Max 3 Adds pro Seite
8. Jedes Add = 10% der aktuellen Side-Size
9. Alle realisierten PnLs speichern
10. Basket-PnL immer live berechnen
11. Wenn basket_pnl >= 0 -> beide Positionen schließen
12. 100:100 allein ist kein echter Reset
13. Neuer Zyklus erst bei gesunder Struktur oder nach Basket-Exit
14. Praktische Bedeutung für den Bot

Der Bot muss fortlaufend verwalten:

long_qty
long_avg
short_qty
short_avg
realized_long_pnl_total
realized_short_pnl_total
spread_pct
ratio
basket_pnl
Anzahl der Long-Heal-Adds
Anzahl der Short-Heal-Adds
aktueller Zustand der State-Machine

Die Bot-Logik soll immer zuerst prüfen:

Ist ein Basket-Exit möglich?
Ist Spread-Healing aktiv?
Gibt es einen gültigen Healing-Trigger?
Gibt es einen gültigen Normal-Flow-Trigger?
Gibt es einen No-Pullback-Failover?
Sonst: warten
15. Schlussbewertung

Diese Strategie ist darauf ausgelegt:

nicht auf perfekte Marktverläufe angewiesen zu sein
auch bei fehlendem Pullback handlungsfähig zu bleiben
eine schlechte Struktur wieder in einen exitfähigen Zustand zu bringen
den Trade auf Gesamtkorb-Basis sauber zu beenden






16. Vorplatzierte Spread-Heal-Orders (Optionaler Modus)
16.1 Ziel dieses Modus

Dieser Modus erweitert die bestehende Spread-Healing-Logik um eine proaktive Order-Platzierung.

Anstatt nur zu reagieren, wenn der Preis bestimmte Bedingungen erfüllt, werden bereits im Voraus zwei Heal-Orders im Markt platziert, sodass:

keine Bewegungen verpasst werden
keine Reaktionsverzögerung entsteht
der Bot immer in beide Richtungen vorbereitet ist
16.2 Aktivierung

Dieser Modus ist optional und wird nur verwendet, wenn:

spread_pct > 1.5%

Zusätzlich:

state in {SPREAD_HEALING, SIZE_RESET_ONLY}
16.3 Grundprinzip

Es werden immer zwei Orders gleichzeitig platziert:

Untere Order (Long-Heal)
Typ: Buy (Limit)
Position: unterhalb von short_avg
Zweck: spread_heal_long
Obere Order (Short-Heal)
Typ: Sell (Limit)
Position: oberhalb von long_avg
Zweck: spread_heal_short

👉 Beide Orders sind gleichzeitig aktiv.

16.4 Order-Positionierung

Die genaue Platzierung erfolgt relativ zu den Averages:

long_heal_price  = short_avg * (1 - heal_offset_pct)
short_heal_price = long_avg  * (1 + heal_offset_pct)

Typischer Wert:

heal_offset_pct = 0.2% – 0.5%

Ziel:

Orders liegen leicht außerhalb der Averages
keine sofortige Ausführung
nur echte Bewegungen triggern
16.5 Ablauf (Zyklus)

Der Mechanismus arbeitet in einem festen Loop:

Schritt 1 – ARMED
beide Heal-Orders sind aktiv
Schritt 2 – FILL
eine Order wird gefillt:
entweder Long-Heal
oder Short-Heal
Schritt 3 – CANCEL
die gegenüberliegende Order wird sofort gecancelt
Schritt 4 – RECALCULATE
neue Werte berechnen:
long_avg
short_avg
spread_pct
ratio
Schritt 5 – REARM
neue Heal-Orders setzen:
wieder oben und unten

Dann zurück zu:

→ ARMED
16.6 Wichtige Regel (kritisch)

Es darf niemals passieren, dass beide Heal-Orders gleichzeitig gefillt werden oder eine alte Gegenorder aktiv bleibt.

Deshalb gilt zwingend:

Nach Fill:
sofort Cancel der Gegenorder
erst danach neue Orders setzen
16.7 Integration in bestehende Healing-Logik

Dieser Modus ersetzt nicht die bestehenden Regeln, sondern ergänzt sie.

Klassische Healing-Regeln bleiben gültig:
price < long_avg           → Long-Healing erlaubt
price >= short_avg * 1.01  → Short-Healing erlaubt
Erweiterung:
Wenn Preplaced-Modus aktiv ist:
entscheidet nicht mehr der aktuelle Tick
sondern welche Order gefillt wird
16.8 Vorteile
1. Keine verpassten Bewegungen

Orders sind bereits im Markt.

2. Symmetrische Logik

Bot ist immer auf beide Richtungen vorbereitet.

3. Weniger Entscheidungslogik

Kein ständiges Umschalten zwischen Long- und Short-Healing.

4. Stabiler Flow

Klare Zyklusstruktur statt reaktiver Einzelentscheidungen.

16.9 Risiken / Grenzen
1. Fake-Fills durch Noise

→ Lösung:

Offset verwenden (z. B. 0.3%)
2. Sehr schnelle Bewegungen

→ beide Orders könnten theoretisch nahe gleichzeitig getriggert werden
→ muss durch Cancel-Logik abgesichert werden

3. Zu aggressive Adds

→ weiterhin begrenzen durch:

max 3 Adds pro Seite
16.10 Zusammenspiel mit State Machine

Dieser Modus arbeitet hauptsächlich in:

SPREAD_HEALING
SIZE_RESET_ONLY

Nicht aktiv in:

NORMAL_FLOW
WAIT_PULLBACK
16.11 Kurzform

Im Spread-Healing-Modus setzt der Bot gleichzeitig eine Long-Heal-Order unterhalb von short_avg und eine Short-Heal-Order oberhalb von long_avg. Sobald eine Order gefillt wird, wird die Gegenorder sofort gecancelt. Danach werden Positionen und Averages neu berechnet und erneut zwei symmetrische Heal-Orders gesetzt. Dieser Zyklus läuft fortlaufend, bis der Spread reduziert oder ein Exit erreicht wird.

⚠️ Wichtiger Hinweis zur Strategie

Dieser Mechanismus ist ein anderer Stil als dein ursprünglicher Flow:

ursprüngliche Strategie = Event-getrieben (reaktiv)
dieser Modus = Order-getrieben (proaktiv)

👉 Du kannst beide kombinieren, aber:

Empfehlung:

Nur aktivieren, wenn:
Spread groß ist
Markt eher seitwärts / pendelnd ist


############################################################################


🔁 Paired Short-Heal → Long-Close Mechanismus

Das ist die wichtigste neue Logik.

Grundidee

👉 Wenn du unten Short aufbaust,
👉 willst du diesen Block oben wieder abbauen

Aber nicht sofort, sondern nur wenn:

der Markt wirklich zurückläuft
Ablauf
Schritt 1 – Short-Heal-Fill
Short wird unten aufgebaut
für jeden Short-Heal-Fill wird eine eigene Long-Close-SL erzeugt
Schritt 2 – Long-Close vorbereiten
nach jedem Short-Fill wird eine neue Long-Close-Stop-Order gesetzt
bereits bestehende Long-Close-Orders bleiben aktiv (kein Replace)

👉 Ergebnis:

mehrere gestaffelte Long-Close-Orders existieren parallel
Schritt 3 – Trigger-Level

Jede Long-Close-Order wird individuell ausgelöst bei:

Short-Entry-Level + Spread + Buffer (~0.2%)

👉 damit:

der Verlust kleiner als der vorher realisierte Short-Gewinn ist
Fees gedeckt sind
auch kleine Rebounds genutzt werden können
6. Dynamische Anpassung (kritisch)
A. Bei neuem Short-Heal-Fill

Dann passiert:

neue zusätzliche Long-Close-SL wird gesetzt
bestehende Long-Close-Orders bleiben unverändert bestehen

👉 keine Aggregation, sondern gestaffelte Orders

B. Bei Long-Close-Fill (sehr wichtig)

Wenn eine Long-SL gefillt wird:

Dann MUSS der Bot:
aktuelle Positionen vom Exchange neu einlesen
long_qty
long_avg
short_qty
short_avg
alle noch offenen zukünftigen Short-Heal-Orders prüfen
alle noch offenen tieferen Short-Orders canceln
neue Short-Orders basierend auf der aktuellen Long-Restgröße berechnen
next_short_qty = current_long_qty * 0.10
neu setzen
👉 Hintergrund

Da sich durch den Long-Close die Long-Restgröße reduziert,
würden bestehende Short-Orders sonst auf einer falschen (zu großen) Basis beruhen.

7. Heal-Block Konzept (angepasst)

Es gibt zwei Ebenen:

1. Operativer Flow (aktiv genutzt)
gestaffelte Short-Heals
gestaffelte Long-Close-Orders
dynamische Anpassung nach jedem Fill
2. Optionaler Tracking-Block (intern)
heal_short_total_qty
heal_short_avg_price

👉 dient nur zur Analyse / Logging,
👉 nicht zwingend zur Ordersteuerung notwendig

Beispiel
Schritt	Aktion	Long-SL aktiv	Bemerkung
Fill 1	Short +10	SL 1	erste gekoppelte Order
Fill 2	Short +10	SL 1 + SL 2	zweite hinzugefügt
Fill 3	Short +10	SL 1 + SL 2 + SL 3	dritte hinzugefügt
Wenn Preis steigt:
SL 1 → wird gefillt
SL 2 → wird ggf. gefillt
SL 3 → bleibt offen oder wird später gefillt
Wenn SL gefillt wird:
Long wird reduziert
danach:
neue Long-Size lesen
nächste Short-Stufe neu berechnen
8. Ziel des Mechanismus
unten → Struktur verbessern (Short Avg erhöhen)
oben → Long schrittweise reduzieren

👉 aber:

nicht alles auf einmal
sondern in gestaffelten Rebound-Schritten
🔥 Kernvorteil dieser Version
kleine Rebounds werden genutzt
Long wird früher reduziert
kein Warten auf großen Move
dynamische Anpassung an reale Positionsgröße
⚠️ Wichtige Regel
Nach jedem Long-Close-Fill müssen alle noch offenen zukünftigen Short-Heal-Orders gecancelt und auf Basis der neuen Long-Restgröße neu gesetzt werden.
⚡ Kurzform

Jeder Short-Heal erzeugt eine eigene Long-Close-Order. Beim Rebound werden diese schrittweise gefillt. Nach jedem Long-Close wird die verbleibende Long-Position neu bewertet und alle zukünftigen Short-Orders entsprechend angepasst.



#########################################################################################

    Reihenfolge der Strategie
    Phase 1 – Aggressiver Down-Heal

    Ziel: schnell Short-Gewinne aufbauen und den ersten starken Schaden abfangen.

    Start mit Hedge:
    Long 100$
    Short 100$
    Wenn der Preis fällt:
    pro 1% Down-Move wird ein Short-Close-Schritt gemacht
    Größe am Anfang: 20% der Long-Size
    Das läuft weiter, bis der ursprüngliche Short vollständig reduziert / geschlossen ist.
    Phase 2 – Long mit Short-Gewinn reduzieren

    Ziel: die gesammelten Short-Gewinne nutzen, um die problematische Long-Seite kleiner zu machen.

    Nachdem der Short vollständig reduziert wurde:
    alle realisierten Short-Gewinne zusammenrechnen
    Mit diesem Gewinn wird der Long teilweise im Verlust geschlossen, um die Long-Size zu reduzieren.

    👉 Ziel:

    Risiko rausnehmen
    Long-Position verkleinern
    Phase 3 – Long-Struktur neu aufbauen

    Ziel: den Long wieder auf sinnvolle Größe bringen und den Avg verbessern.

    Danach wird nicht sofort blind neu gehedged, sondern zuerst der Long wieder aufgebaut.
    Long-Rebuys werden gesetzt, bis die gewünschte Long-Zielgröße wieder erreicht ist
    (in deinem Beispiel wieder 100$ Long-Size).

    👉 Dabei verbessert sich idealerweise auch der long_avg.

    Phase 4 – Short neu aufbauen

    Ziel: nach dem Long-Rebuild den Hedge wiederherstellen.

    Erst nachdem der Long wieder sauber aufgebaut wurde, wird auch der Short wieder aufgebaut.
    Short wird wieder auf die gewünschte Hedge-Größe gesetzt
    (in deinem Beispiel wieder 100$ Short-Size).
    Phase 5 – Feiner Healing-Modus

    Ziel: ab jetzt nicht mehr aggressiv, sondern kontrolliert weiterarbeiten.

    Nach dem Reset auf:
    Long 100$
    Short 100$

    wird nicht mehr mit 20% gearbeitet, sondern mit:

    10% Short-Close-Schritten bezogen auf die aktuelle Long-Size
    Danach läuft der feinere Healing-Zyklus:
    Preis fällt → kleiner Short-Close
    Preis reboundet → passender Long-Close
    Struktur wird schrittweise geheilt
    Kurzform für die Doku
    1. Start mit Long 100$ und Short 100$.
    2. In der ersten Down-Heal-Phase werden bei fallendem Preis aggressive Short-Close-Schritte mit 20% der Long-Size ausgeführt.
    3. Diese Phase läuft, bis der ursprüngliche Short vollständig reduziert ist.
    4. Anschließend werden alle realisierten Short-Gewinne addiert.
    5. Mit diesen Gewinnen wird die Long-Position teilweise im Verlust geschlossen, um die Long-Size zu reduzieren.
    6. Danach wird der Long über Rebuys wieder auf die gewünschte Zielgröße aufgebaut.
    7. Erst nach diesem Long-Rebuild wird auch der Short wieder auf die gewünschte Zielgröße aufgebaut.
    8. Ab diesem Punkt wechselt die Strategie in den feineren Healing-Modus.
    9. Im feineren Modus erfolgen Short-Close-Schritte nur noch mit 10% der aktuellen Long-Size.
    10. Ziel ist es dann, die Struktur kontrolliert weiter zu heilen und den Basket-Exit zu erreichen.




    ########################################################################################



    Master-Dokumentation – Finale Hedge-Strategie
1. Ziel der Strategie

Diese Strategie verwaltet einen gleichzeitigen Long- und Short-Hedge so, dass:

Gewinne auf der starken Seite schrittweise realisiert werden
die schwache Seite nicht unkontrolliert aus dem Ruder läuft
ein zu großer Spread zwischen long_avg und short_avg aktiv geheilt wird
der gesamte Trade als Gesamtkorb geschlossen wird, sobald Break-Even oder Profit erreicht ist

Die Strategie bewertet nicht einzelne Seiten isoliert, sondern immer die Gesamtstruktur aus:

Long-Position
Short-Position
realisierten Gewinnen/Verlusten
aktuellem offenen PnL beider Seiten
Spread
Ratio
Basket-PnL
2. Grundprinzip

Die Strategie arbeitet in drei übergeordneten Ebenen:

Ebene A – Normal Flow

Bei echten Marktbewegungen werden auf der Gewinnerseite Teilgewinne gesichert.

Ebene B – Recovery / Failover

Wenn ein erwarteter Pullback ausbleibt, wird die Offside-Seite defensiv reduziert, damit die Struktur nicht eskaliert.

Ebene C – Spread-Healing

Wenn der Abstand zwischen long_avg und short_avg zu groß wird, wird die schlechtere Seite gezielt verbessert, damit die Struktur wieder exitfähig wird.

3. Feste Parameter
Marktstruktur
Trigger nur bei echten 1%-Bewegungen
Keine Aktionen auf kleine Noise-Bewegungen
Adds
maximal 3 Adds pro Seite und Zyklus
jedes Add = 10% der aktuellen Side-Size
Spread-Healing

Aktiv ab:

spread_pct > 1.5
Short-Healing

Erst erlaubt, wenn:

price >= short_avg * 1.01
Long-Healing

Nur erlaubt, wenn:

price < long_avg
Exit

Gesamtkorb schließen, wenn:

basket_pnl >= 0

Optional kann zusätzlich ein kleiner Profit-Exit verwendet werden, z. B.:

basket_pnl >= target_profit_usd
4. Wichtige Definitionen
4.1 Spread
spread_pct = abs(long_avg - short_avg) / long_avg * 100

Der Spread misst den strukturellen Abstand zwischen long_avg und short_avg.

Wichtig:

reine Reduktionen ändern den Avg nicht
reine Reduktionen ändern also den Spread nicht
nur Adds / Rebuilds / Healing-Aktionen können den Spread wirklich verändern
4.2 Ratio
ratio = long_qty / short_qty

Interpretation:

ratio = 1.0 → gleiche Size
ratio > 1.0 → Long ist größer
ratio < 1.0 → Short ist größer

Wichtig:

100:100 Size bedeutet nur Größen-Gleichgewicht
100:100 bedeutet nicht automatisch, dass die Struktur gesund ist
wenn der Spread noch groß ist, ist kein echter Reset erreicht
4.3 Basket-PnL
realized_pnl_total = realized_long_pnl_total + realized_short_pnl_total

unrealized_long_pnl  = (current_price - long_avg) * long_qty
unrealized_short_pnl = (short_avg - current_price) * short_qty

basket_pnl = realized_pnl_total + unrealized_long_pnl + unrealized_short_pnl

Die Strategie bewertet den Exit immer über den Gesamtkorb.

Nicht relevant ist:

ob Long einzeln im Gewinn ist
ob Short einzeln im Verlust ist

Relevant ist nur:

ob die Summe aus realisierten und offenen Ergebnissen wieder bei 0 oder im Plus liegt
5. Globale Prioritätsreihenfolge der Bot-Logik

Der Bot soll die Logik immer in dieser Reihenfolge prüfen:

1. Basket-Exit möglich?

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Trade vollständig beenden
2. Ist aktives strukturelles Healing nötig?

Wenn:

spread_pct > 1.5

Dann:

Spread-Healing hat Vorrang
normaler Event-Flow tritt in den Hintergrund
3. Wenn kein Spread-Healing nötig ist:

Dann normale Strategie ausführen:

Normal Flow
Pullback / Rebuild
No-Pullback-Failover
6. Zustände der Strategie
6.1 NORMAL_FLOW

Standardmodus, solange kein strukturelles Problem vorliegt.

Regeln:

bei starkem Up-Move → Gewinnerseite Long schrittweise reduzieren
bei starkem Down-Move → Gewinnerseite Short schrittweise reduzieren

Ziel:

Gewinne sichern
Struktur nicht sofort mit Adds verkomplizieren
6.2 WAIT_PULLBACK

Nach einer Teilreduktion wird auf eine bestätigte Gegenbewegung gewartet.

Regeln:

keine hektischen Adds
kein blindes Umschalten
Rebuild nur nach bestätigtem Gegenereignis

Ziel:

die zuvor reduzierte Seite günstiger wieder aufbauen
6.3 NO_PULLBACK_FAILOVER

Wenn die erwartete Gegenbewegung ausbleibt und der Markt stattdessen weiter trendet.

Regeln:

Nach Long-Reduktionen:

wenn der Preis weiter steigt und kein Pullback kommt
Short-Seite defensiv reduzieren

Nach Short-Reduktionen:

wenn der Preis weiter fällt und kein Rebound kommt
Long-Seite defensiv reduzieren

Wichtig:

diese Reduktion ist kein Profit-Move
sie ist ein Risk-Reduction-Move
6.4 SPREAD_HEALING

Wird aktiviert, wenn die Struktur zu weit auseinanderläuft.

Aktivierung:

if spread_pct > 1.5:
    spread_healing = true
6.5 SPREAD_HEAL_LONG

Long-Healing ist nur erlaubt, wenn ein Long-Add den long_avg wirklich verbessert.

Bedingung:

price < long_avg

Aktion:

Long +10% der aktuellen Long-Size
maximal 3 Adds in diesem Healing-Zyklus

Wirkung:

long_avg sinkt
Spread kann kleiner werden

Nicht erlaubt:

Long-Add oberhalb long_avg
6.6 SPREAD_HEAL_SHORT

Short-Healing ist nur erlaubt, wenn ein Short-Add den short_avg wirklich verbessert.

Bedingung:

price >= short_avg * 1.01

Aktion:

Short +10% der aktuellen Short-Size
maximal 3 Adds in diesem Healing-Zyklus

Wirkung:

short_avg steigt
Spread kann kleiner werden

Nicht erlaubt:

Short-Add unterhalb short_avg
6.7 WAIT / NO ACTION

Wenn Spread-Healing aktiv ist, aber keiner der sauberen Trigger erfüllt ist.

Dann gilt:

keine Aktion
nur warten

Typische Fälle:

Preis steigt leicht, ist aber noch nicht hoch genug für Short-Healing
Preis fällt leicht, aber nicht sinnvoll genug für Long-Healing
Richtungswechsel ist unklar

Ziel:

kein Overtrading
kein ständiges Hin-und-Her
6.8 SIZE_RESET_ONLY

Wenn Long- und Short-Qty wieder ähnlich oder gleich sind, aber der Spread noch zu groß ist.

Beispiel:

long_qty ≈ short_qty
spread > 1.5%

Bedeutung:

Size ist wieder balanciert
Struktur ist aber noch nicht gesund
es darf noch kein neuer Normalzyklus starten
6.9 FULL_RESET_READY

Ein neuer sauberer Zyklus ist erst erlaubt, wenn die Struktur wieder gesund ist.

Praktisch heißt das:

Size wieder balanciert
Spread wieder akzeptabel
oder Basket-Exit ist bereits möglich
7. Trigger-Regeln
7.1 Marktstruktur-Trigger

Es werden nur echte Bewegungen ab 1% gehandelt.

Up-Move:

Preis steigt ≥ 1% über das letzte relevante Hoch oder die Strukturreferenz

Down-Move:

Preis fällt ≥ 1% unter das letzte relevante Tief oder die Strukturreferenz
7.2 Last Relevant High / Low

Es wird immer nur mit dem letzten bestätigten strukturellen Hoch/Tief gearbeitet.

Nicht gültig sind:

Durchschnitte
beliebige Zwischenwerte
unbestätigte kleine Bounces
7.3 Event-Kette

Eine Folgeaktion darf nur nach gültigem vorherigem Ereignis kommen.

Beispiel:

erst Breakout
dann Referenzhoch gesetzt
dann erst Pullback-Trigger erlaubt

Oder:

erst Down-Move
dann Referenztief gesetzt
dann erst Rebound erlaubt
7.4 Einmalige Trigger

Pro Strukturereignis darf eine Aktion nur einmal feuern.

Erst wenn ein neues Gegenereignis die Referenz aktualisiert, wird der nächste Trigger wieder freigegeben.

7.5 Priorität
Hauptaktion zuerst
nie Add und Close im selben Tick
Guards filtern nur, ob eine Aktion erlaubt ist
Guards sind nicht der primäre Auslöser
8. Add-Regeln

Pro Seite:

maximal 3 Adds pro Zyklus
jedes Add = 10% der aktuellen Side-Size

Beispiel bei Startgröße 100:

Add 1: 100 -> 110
Add 2: 110 -> 121
Add 3: 121 -> 133.1

Das gilt identisch für Long und Short.

9. Rebuild-Regeln

Nach Long-Reduktionen:

wenn ein sauberer Pullback kommt
reduzierte Long-Teile können wieder aufgebaut werden

Nach Short-Reduktionen:

wenn ein sauberer Rebound kommt
reduzierte Short-Teile können wieder aufgebaut werden

Wichtig:

Rebuild nur nach bestätigtem Gegenereignis
kein blindes Wiederaufbauen ohne Struktur
10. Spread-Healing-Logik – endgültige Kernregel

Wenn:

spread_pct > 1.5

Dann gelten nur diese drei Regeln:

Regel 1

Wenn:

price < long_avg

Dann:

Long-Healing erlaubt
Regel 2

Wenn:

price >= short_avg * 1.01

Dann:

Short-Healing erlaubt
Regel 3

Sonst:

warten
keine Aktion

Das ist die feste Endlogik für Spread-Healing.

11. Exit-Logik
11.1 Grundregel

Alle realisierten Gewinne und Verluste werden während des gesamten Trades gespeichert.

Diese werden laufend mit dem offenen Long- und Short-PnL verrechnet.

basket_pnl = realized_pnl_total + unrealized_long_pnl + unrealized_short_pnl
11.2 Exit-Bedingung

Wenn:

basket_pnl >= 0

Dann:

beide Positionen schließen
Trade vollständig beenden

Optional:

basket_pnl >= target_profit_usd

Dann:

beide Positionen mit kleinem Gewinn schließen
11.3 Bedeutung

Healing ist nicht das Ziel.
Healing ist nur das Mittel, um die Struktur so weit zu verbessern, dass ein späterer Basket-Exit möglich wird.

12. Was die Strategie ausdrücklich nicht tut
kein Long-Healing oberhalb long_avg
kein Short-Healing unterhalb short_avg
kein Short-Healing vor 1% über short_avg
kein neuer Zyklus nur wegen 100:100 Size
kein Add und Close im selben Tick
kein Exit auf Basis nur einer einzelnen Seite
kein blindes Umschalten bei kleinen Bounces
13. Operative Spezialmechanik: Paired Short-Heal → Long-Close

Das ist eine operative Unterlogik innerhalb des Healing-/Feinheilungs-Prozesses.

Grundidee

Wenn unten ein Short-Heal aufgebaut wird, soll dieser Block beim Rebound oben wieder teilweise gegen Long abgebaut werden.

Ablauf
Schritt 1 – Short-Heal-Fill

Short wird aufgebaut.

Für jeden Short-Heal-Fill wird eine eigene Long-Close-Order erzeugt.

Schritt 2 – Long-Close vorbereiten

Nach jedem Short-Fill wird eine neue Long-Close-Stop-Order gesetzt.

Bereits bestehende Long-Close-Orders bleiben aktiv.

Ergebnis:

mehrere gestaffelte Long-Close-Orders können parallel existieren
kein Replace der alten Orders
Schritt 3 – Trigger-Level

Jede Long-Close-Order wird individuell ausgelöst bei:

short_entry_level + spread + buffer

typisch mit kleinem Buffer, z. B. etwa 0.2%.

Ziel:

der Verlust auf dem Long-Close bleibt kleiner als der zuvor realisierte Short-Gewinn
Fees werden gedeckt
auch kleinere Rebounds können genutzt werden
Dynamische Anpassung
A. Bei neuem Short-Heal-Fill

Dann passiert:

neue zusätzliche Long-Close-Order wird gesetzt
bestehende Long-Close-Orders bleiben unverändert bestehen
B. Bei Long-Close-Fill

Wenn eine Long-Close-Order gefillt wird, dann muss der Bot:

aktuelle Positionen vom Exchange neu einlesen
long_qty, long_avg, short_qty, short_avg aktualisieren
alle noch offenen zukünftigen Short-Heal-Orders prüfen
alle noch offenen tieferen Short-Orders canceln
neue Short-Heal-Orders auf Basis der aktuellen Long-Restgröße berechnen

Neue Basis:

next_short_qty = current_long_qty * 0.10

Hintergrund:

Wenn Long kleiner geworden ist, dürfen zukünftige Short-Heals nicht weiter auf der alten, zu großen Basis laufen.

Ziel dieses Mechanismus
unten Struktur verbessern
oben Long schrittweise abbauen
kleine Rebounds aktiv nutzen
Long nicht erst sehr spät reduzieren
zukünftige Orders immer an die reale Restgröße anpassen
14. Spezieller Recovery-Ablauf: Phase 1 bis Phase 5

Dieser Teil ist ein spezifischer Heilungs-/Recovery-Ablauf und nicht die globale Prioritätslogik des gesamten Bots.

Er kommt zum Einsatz, wenn du einen stärkeren strukturellen Schaden systematisch abarbeiten willst.

Phase 1 – Aggressiver Down-Heal

Ziel:

schnell Short-Gewinne aufbauen
den ersten starken Schaden abfangen

Ausgangslage:

Start mit Hedge, z. B. Long 100 / Short 100

Bei fallendem Preis:

pro relevantem Down-Schritt wird ein aggressiver Short-Schritt ausgeführt
Größe am Anfang: 20% der Long-Size

Diese Phase läuft, bis der ursprüngliche Short vollständig reduziert / geschlossen ist.

Phase 2 – Long mit Short-Gewinn reduzieren

Ziel:

gesammelte Short-Gewinne verwenden, um die problematische Long-Seite zu verkleinern

Nachdem der Short vollständig reduziert wurde:

alle realisierten Short-Gewinne zusammenrechnen
mit diesem Gewinn wird Long teilweise im Verlust geschlossen

Ziel:

Risiko rausnehmen
Long-Position verkleinern
Phase 3 – Long-Struktur neu aufbauen

Ziel:

den Long wieder auf sinnvolle Größe bringen
den long_avg verbessern

Danach wird nicht sofort blind neu gehedged, sondern zuerst der Long wieder aufgebaut.

Long-Rebuys werden gesetzt, bis die gewünschte Long-Zielgröße wieder erreicht ist.

Phase 4 – Short neu aufbauen

Ziel:

nach dem Long-Rebuild den Hedge wiederherstellen

Erst nachdem der Long wieder sauber aufgebaut wurde, wird auch der Short wieder aufgebaut.

Short wird wieder auf die gewünschte Hedge-Größe gesetzt.

Phase 5 – Feiner Healing-Modus

Ziel:

ab jetzt nicht mehr aggressiv, sondern kontrolliert weiterarbeiten

Nach Reset auf etwa:

Long 100
Short 100

wird nicht mehr mit 20% gearbeitet, sondern mit feineren Schritten.

Im feineren Modus erfolgen Short-Heal-Schritte nur noch mit:

10% der aktuellen Long-Size

Danach läuft der kontrollierte Healing-Zyklus:

Preis fällt → kleiner Short-Heal
Preis reboundet → passender Long-Close
Struktur wird schrittweise geheilt
Ziel bleibt der Basket-Exit
15. Optionaler Modus: Vorplatzierte Spread-Heal-Orders

Wichtig:
Dieser Abschnitt ist ein optionaler Modus als Konzept.
Er gehört nur in die Doku als Zusatzmechanik und sollte nur als aktiv dokumentiert werden, wenn dieser Pfad im Code bewusst wieder eingeschaltet ist.

Aktuell ist die Kernstrategie auch ohne diesen Modus vollständig definiert.

Ziel

Anstatt nur reaktiv auf den Tick zu warten, können Heal-Orders bereits im Voraus in den Markt gelegt werden.

Dann sind gleichzeitig zwei Heal-Orders aktiv:

untere Order = Long-Heal
obere Order = Short-Heal
Aktivierung

Nur sinnvoll, wenn:

spread_pct > 1.5
state in {SPREAD_HEALING, SIZE_RESET_ONLY}
Grundprinzip

Es werden zwei Orders gleichzeitig platziert:

Untere Order
Typ: Buy Limit
Position: unterhalb einer Referenz
Zweck: spread_heal_long
Obere Order
Typ: Sell Limit
Position: oberhalb einer Referenz
Zweck: spread_heal_short
Zyklus
beide Orders sind armed
eine Order wird gefillt
Gegenorder wird sofort gecancelt
neue Werte berechnen
neue Heal-Orders setzen
zurück zu armed
Kritische Regel

Es darf niemals passieren, dass beide Heal-Orders gleichzeitig aktiv weiterlaufen, nachdem eine Seite bereits gefillt wurde.

Deshalb gilt zwingend:

nach Fill sofort Gegenorder canceln
erst danach neu berechnen
erst danach neu armen
Einordnung

Dieser Modus ersetzt die normale Healing-Logik nicht, sondern ist nur eine alternative operative Ausführung.

Die Kernregeln bleiben trotzdem:

price < long_avg           -> Long-Healing erlaubt
price >= short_avg * 1.01  -> Short-Healing erlaubt
sonst warten
16. Praktische Bot-Verwaltung

Der Bot muss fortlaufend verwalten:

long_qty
long_avg
short_qty
short_avg
realized_long_pnl_total
realized_short_pnl_total
spread_pct
ratio
basket_pnl
Anzahl der Long-Heal-Adds
Anzahl der Short-Heal-Adds
aktueller Zustand der State-Machine
17. Finale Kurzfassung
Globale Reihenfolge
Basket-Exit prüfen
Wenn spread_pct > 1.5 → Spread-Healing hat Vorrang
Sonst Normal Flow
Nach Reduktion auf Pullback / Rebuild warten
Wenn Pullback ausbleibt → Offside-Seite defensiv reduzieren
Im Spread-Healing:
price < long_avg → Long-Heal
price >= short_avg * 1.01 → Short-Heal
sonst warten
Max 3 Adds pro Seite
Jedes Add = 10% der aktuellen Side-Size
Alle realisierten PnLs speichern
Basket-PnL immer live berechnen
Wenn basket_pnl >= 0 → beide Positionen schließen
100:100 allein ist kein echter Reset
Neuer Zyklus erst bei gesunder Struktur oder nach Basket-Exit
18. Abschlussbewertung

Diese Strategie ist darauf ausgelegt:

nicht auf perfekte Marktverläufe angewiesen zu sein
auch bei fehlendem Pullback handlungsfähig zu bleiben
eine schlechte Struktur wieder in einen exitfähigen Zustand zu bringen
den Trade auf Gesamtkorb-Basis sauber zu beenden