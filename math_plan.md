Gesamtplan: OB-basierter Hedge-Recovery-Bot
1. Ausgangspunkt

Der Bot startet grundsätzlich mit einer abgesicherten Position, zum Beispiel:

Long:  100 USDT
Short:  50 USDT
Entry: gleicher Preis

Der Long ist die Hauptposition. Der Short schützt bei einem stärkeren Preisrückgang.

Die bisherige Schwäche des Hedge-Bots war:

feste kleine Grid-Abstände
→ mehrere Reduce-Verluste
→ Verluste werden auf den Exit aufgeschlagen
→ benötigter Exit steigt immer höher
→ Exit kann über relevanten Ask-Walls liegen
→ Preis dreht möglicherweise vorher wieder nach unten

Genau dieses Problem soll die neue OB-basierte Strategie lösen.

2. Grundlage: kausale Orderbook-Grid-Punkte

Vor dem Start analysiert der Bot das Orderbook und bestimmt:

relevante Bid-Zonen unterhalb des aktuellen Preises
relevante Ask-Zonen oberhalb des aktuellen Preises

Die bisherigen Audits zeigen bei DOGE und APT eine auffällig regelmäßige Struktur von ungefähr:

1,2–1,3 % Abstand zwischen den relevanten Grid-Zonen

Wichtig:

Die Punkte wurden kausal vor dem späteren Kontakt erkannt.
Sie wurden nicht nachträglich aus dem Chart gewählt.
DOGE und APT zeigten Fills und deutliche Reaktionen.
BTC war mit OB200 wegen zu geringer sichtbarer Tiefe nicht testbar.
3. Zentrale Strategieidee

Der Bot soll nicht blind bei festen Prozentabständen handeln.

Stattdessen:

Orderbook analysieren
→ relevante Bid- und Ask-Punkte bestimmen
→ vollständige Positionsrechnung durchführen
→ nur mathematisch gültige Orders setzen

Die Mathematik entscheidet, nicht das Gefühl.

4. Hauptregel für den Exit

Für Long-primary gilt immer:

Basket-Exit inklusive
- aller realisierten Verluste
- aller Gebühren
- Slippage
- gewünschtem Nettoprofit

muss unter der relevanten Ask-Zone liegen

Genauer:

required_exit
<=
ask_level - safety_buffer

Alternativ direkt als PnL-Prüfung:

Netto-PnL bei ask_level - buffer
>= gewünschter Nettoprofit

Ein Exit oberhalb der Ask-Zone ist grundsätzlich nicht zulässig.

5. Prüfung muss vor der Order erfolgen

Der Bot darf nicht erst nach einem Fill feststellen, dass die Rechnung nicht mehr passt.

Vor jeder Order wird der vollständige Folgezustand simuliert:

Long-Qty und Long-Avg nach der Aktion
Short-Qty und Short-Avg nach der Aktion
realisierter Long-Verlust
realisierter Short-Gewinn
Entry- und Exit-Gebühren
Slippage
Zielprofit
Basket-PnL an der Ask-Grenze
Risiko bis zum nächsten Bid-Level

Erst danach lautet die Entscheidung:

PASS_WITH_CURRENT_SIZE
PASS_WITH_SIZE_ADJUSTMENT
WAIT_FOR_NEXT_BID
NO_FEASIBLE_SOLUTION
6. Verhalten am ersten Bid-Punkt

Wenn der Preis den ersten unteren Grid-Punkt erreicht, wird nicht automatisch gehandelt.

Zuerst wird geprüft:

Kann der Basket nach der geplanten Aktion
an der zugehörigen Ask-Zone
BE + Fees + Profit erreichen?

Falls ja:

Order darf gesetzt werden

Falls nein:

Positionsgrößen simulieren

Mögliche Varianten:

Long teilweise reduzieren
Long reduzieren und teilweise tiefer zurückkaufen
zusätzlichen Long am tiefen Bid-Level kaufen
Short unverändert lassen
kleinen Short-Add vorbereiten
nichts tun und einen Bid-Punkt tiefer warten
7. Warum ein tieferer Bid-Punkt helfen kann

Wenn die Rechnung am aktuellen Bid-Level nicht aufgeht, kann ein tieferer Punkt günstiger sein:

der bestehende Short hat mehr Gewinn aufgebaut
zusätzlicher Long kann günstiger gekauft werden
der Abstand vom tieferen Bid-Level zur Ask-Zone ist größer
dieselbe Zusatzinvestition erzeugt beim Rücklauf mehr Gewinn
die benötigte Zusatzgröße kann kleiner werden

Beispiel:

Fehlbetrag am Ask-Exit: 0,40 USDT

Bid 1 → Ask-Abstand klein
benötigter Zusatz-Long: zu groß

Bid 2 → Ask-Abstand größer
benötigter Zusatz-Long: akzeptabel

Dann lautet die Entscheidung:

Bid 1 überspringen
→ Bid 2 abwarten
8. Positionsgrößen werden mathematisch gelöst

Wenn zusätzliches Long-Kapital helfen soll, wird die benötigte Menge berechnet.

Vereinfacht:

benötigte Zusatzmenge
=
Fehlbetrag
/
(Ask-Exit - Bid-Kaufpreis)

Mitberücksichtigt werden müssen:

neue Entry-Gebühren
spätere Exit-Gebühren
Slippage
Profitziel
verbleibendes Risiko bis zum nächsten Bid-Punkt

Eine rechnerisch passende Menge ist nur gültig, wenn zusätzlich gilt:

Gesamtkapital <= Recovery-Budget
Gesamtexposure <= Exposure-Limit
Verlust bis nächster Bid <= Risikolimit
Short-Schutz >= Mindestwert
9. Verhalten bei einem Grid-Downshift

Wenn der Markt weiter fällt, können neue Ask-Zonen tiefer entstehen. Gleichzeitig können auch neue tiefere Bid-Zonen erscheinen.

Dann soll der Bot nicht blind an alten, ungefüllten Orders festhalten.

Ablauf:

neue Ask-Struktur deutlich tiefer
+ neue Bid-Struktur ebenfalls tiefer
→ alte ungefüllte Recovery-Orders canceln
→ aktuelle Position reconciliieren
→ neues Grid analysieren
→ nächsten tieferen Bid-Punkt wählen
→ vollständige Rechnung neu durchführen
→ nur gültige Orders neu setzen

Bereits ausgeführte Aktionen bleiben natürlich Bestandteil der Rechnung:

realisierte Verluste
Gebühren
neue Durchschnittspreise
veränderte Mengen

Diese dürfen beim Replan niemals ignoriert werden.

10. Was verändert werden darf

Bei einem neuen Downshift darf der Bot:

ungefüllte Orders canceln
den nächsten Einstieg tiefer verlegen
einen Grid-Punkt überspringen
die Long-Reduce-Menge verkleinern
eine kleine zusätzliche Long-Menge berechnen
den Long netto abbauen
den Short unverändert lassen
unter strengen Bedingungen einen kleinen Short hinzufügen

Er darf nicht:

alte Verluste vergessen
Exit künstlich über die Ask-Zone verschieben
beliebig neues Kapital hinzufügen
unbegrenzt neue Cycles erzeugen
immer größere Positionen aufbauen
einen mathematisch ungültigen Plan trotzdem ausführen
11. Rolle des Longs

Wenn Ask- und Bid-Struktur weiter nach unten rollen, sollte der Long nicht zwingend immer vollständig refilled werden.

Möglich wäre:

Long um 20 % reduzieren
nur 10 % zurückkaufen
→ Long netto um 10 % kleiner

Dadurch wird das Netto-Long-Risiko tatsächlich reduziert.

Das ist robuster als:

Long reduzieren
→ immer vollständig wieder auffüllen
→ Long-Menge bleibt unverändert

Der genaue Anteil muss vom Solver berechnet werden.

12. Rolle des Shorts

Der Short ist in einem anhaltenden Abwärtstrend die wichtigste Schutzposition.

Deshalb:

Der Short sollte bei einem klaren Downshift nicht laufend zur Verlustdeckung verbraucht werden.

Bevorzugte Regel:

Short unverändert lassen

Der Short-Gewinn wächst bei weiterem Fall und verbessert den wirtschaftlichen Basketzustand.

13. Optionaler kleiner Short-Add

Ein kleiner Short-Add kann sinnvoll sein, wenn:

der Markt die Bid-Zone klar bricht
kein schneller Reclaim erfolgt
die gesamte Grid-Struktur tiefer wandert
der aktuelle Preis deutlich unter dem alten Short-Avg liegt
der neue Short-Avg noch genügend Abstand zum aktuellen Preis besitzt
der Basket an der Ask-Zone weiterhin profitabel geschlossen werden kann

Nicht ausreichend ist nur:

aktueller Preis weit unter altem Short-Avg

Entscheidend ist:

neuer Short-Avg nach dem Add

und:

Basket-PnL an der geplanten Ask-Grenze

Ein Short-Add direkt an einer starken Bid-Reaktionszone wäre riskant, weil dort ein Bounce beginnen kann.

Daher:

Long-Reduce eventuell an Bid-Zone

Short-Add erst nach:
Bid-Bruch
+ fehlendem Reclaim
+ bestätigtem Grid-Downshift
14. Recovery-Kapital

Zusätzliches Kapital darf nicht spontan und unbegrenzt nachgeschossen werden.

Vor dem Start wird ein fester Recovery-Pool definiert, zum Beispiel:

Start:
100 USDT Long
50 USDT Short

Recovery-Pool:
maximal 25–50 USDT

Dieser Pool wird in maximale Stufen aufgeteilt.

Beispiel:

Reserve 1: 15 USDT
Reserve 2: 10 USDT
Reserve 3: 10 USDT
Restreserve: 5 USDT

Die wirklichen Werte müssen simuliert werden.

Harter Endzustand:

RESCUE_CAPITAL_EXHAUSTED

Dann darf kein weiteres Kapital hinzugefügt werden.

15. Zwei Gleichungen müssen immer gleichzeitig passen

Eine Strategievariante ist nur gültig, wenn beide Bedingungen erfüllt sind.

Bedingung A: Exit
Netto-PnL an Ask-Level minus Buffer
>= Gebühren + Zielprofit
Bedingung B: weiterer Fall
Worst-Case-Verlust bis zum nächsten Bid-Level
<= verbleibendes Risikobudget

Eine Variante, die nur an der Ask-Zone profitabel wäre, aber beim nächsten Fall zu viel verliert, ist keine gültige Lösung.

16. Zustände des Solvers

Für jeden Grid-Punkt sollte der Rechner genau eine Entscheidung liefern.

PASS_WITH_CURRENT_SIZE

Die bestehende Position reicht aus.

PASS_WITH_SIZE_ADJUSTMENT

Eine begrenzte Anpassung der Positionsgröße macht den Exit gültig.

WAIT_FOR_NEXT_BID

Am aktuellen Punkt wäre die benötigte Anpassung zu groß oder zu riskant. Einen Punkt tiefer neu rechnen.

NO_FEASIBLE_SOLUTION

Keine erlaubte Größenkombination erfüllt Exit- und Risikobedingung.

RECOVERY_BUDGET_EXHAUSTED

Der festgelegte Zusatzkapitalrahmen ist vollständig verbraucht.

NO_PROFITABLE_EXIT_BEFORE_ASK

Der Basket kann vor der aktuellen Ask-Zone nicht mehr mit BE, Gebühren und Profit geschlossen werden.

17. Was nicht garantiert werden kann

Nicht garantiert werden kann:

dass der Markt immer zur Ask-Zone zurückkehrt
dass jede Ask-Zone hält
dass für jeden Preisverlauf eine passende Positionsgröße existiert
dass zusätzliches Kapital jeden Basket retten kann
dass ein tieferer Bid-Punkt immer eine bessere Lösung bringt

Garantiert werden kann:

keine Order ohne vorherige vollständige Berechnung
kein Exit oberhalb der erlaubten Ask-Grenze
kein unbegrenztes Nachkaufen
keine Erhöhung über das festgelegte Risikobudget
kein Verbrauchen des Shorts ohne mathematische Begründung
klare Erkennung, wenn keine sichere Lösung mehr existiert
18. Erste Strategieversion

Die erste testbare Version sollte möglichst einfach bleiben.

Start
100 USDT Long
50 USDT Short
gleicher Entry
Vor dem Start
Bid- und Ask-Grid bestimmen
Recovery-Budget festlegen
Safety-Buffer festlegen
Fees, Slippage und Profitziel definieren
An jedem Bid-Level
1. Basketzustand berechnen
2. PnL an Ask minus Buffer berechnen
3. mögliche Größenanpassungen simulieren
4. Risiko bis zum nächsten Bid berechnen
5. Entscheidung treffen
Bevorzugte Reihenfolge
A. bestehende Position reicht
B. kleiner Zusatz-Long reicht
C. Long teilweise abbauen / teilweise refillen
D. aktuellen Bid überspringen
E. kleiner Short-Add nur bei bestätigtem Breakdown
F. keine Lösung → Recovery stoppen
19. Nächster technischer Schritt

Noch keinen vollständigen Bot bauen.

Zuerst einen isolierten:

OB Hedge Feasibility Solver
Eingaben
Long qty
Long avg
Short qty
Short avg
realisierter PnL
bisherige Gebühren
Bid-Level
nächster tieferer Bid-Level
Ask-Level
Safety-Buffer
Entry-/Exit-Gebühren
Slippage
Profitziel
Recovery-Budget
Exposure-Limit
Mindest-Short-Schutz
Zu testende Aktionen
keine Anpassung
Long reduzieren
Long reduzieren + Teil-Refill
Zusatz-Long
kleiner Short-Add
aktuellen Bid überspringen
Ausgaben
Basket-PnL an Ask-Grenze
erforderlicher Exit
Exit-Abstand zur Ask-Zone
benötigte Zusatzgröße
neuer Long-Avg
neuer Short-Avg
neue Mengen
Risiko bis nächster Bid
verbrauchtes Recovery-Kapital
verbleibendes Recovery-Kapital
Entscheidungsstatus
20. Erster Praxistest

Der Solver sollte zuerst auf den bekannten APT-Punkten laufen:

Root:
0.56755

Bid:
0.560003
0.553402
0.546473
0.539758

Ask:
0.575114
0.582012
0.588852

Startposition:

Long 100 USDT
Short 50 USDT
gleicher Entry bei 0.56755

Zu vergleichen:

Variante A:
Short unverändert, Zusatz-Long erlaubt

Variante B:
Long schrittweise abbauen, Short unverändert

Variante C:
Long abbauen + kleiner Short-Add nach Breakdown

Variante D:
aktuellen Bid überspringen und tiefer neu rechnen

Danach dieselbe Simulation auf DOGE.

Primärentscheidung
BUILD_FEASIBILITY_SOLVER_BEFORE_BOT_INTEGRATION

Die Strategie ist mathematisch umsetzbar. Noch offen ist, wie häufig sie unter realistischen Kapital- und Risikogrenzen tatsächlich eine gültige Lösung findet. Genau das muss der Solver als Nächstes beantworten