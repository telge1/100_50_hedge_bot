Initial Entry
   Long + Short werden eröffnet. Beispiel 100$ Long 50$ Short


1. Preis fällt gegen den Long-Bot

2. Cycle 1 wird ausgelöst

3. Long-Seite wird reduziert
   → Long wird mit Verlust teilweise geschlossen

4. Dieser realisierte Long-Verlust wird gespeichert/berechnet

5. Danach wird eine Short-TP-Order gesetzt
   → Ziel: Short-Gewinn soll genau diesen Long-Verlust + Zielprofit decken

6. Wenn Short-TP gefüllt wird:
   → Verlust ist ausgeglichen
   → Cycle ist sauber abgeschlossen

7. Danach läuft der nächste Cycle ähnlich weiter

8. Nach Cycle 2 kommt der Refill:
   Die reduzierte Long-Seite wird im tieferen Preisbereich wieder aufgefüllt, sodass der Long-Average sinkt.
   Gleichzeitig wird auch die Short-Seite passend aufgefüllt, damit Long und Short wieder möglichst denselben Average-Price-Bereich haben und kein Spread zwischen den beiden Hedge-Seiten entsteht.
   Danach arbeitet der Bot mit einer neu ausgerichteten Hedge-Struktur weiter.

Also vereinfacht:

Fallender Preis im Long-Bot:

Long Reduce mit Verlust
→ Short TP gleicht Verlust aus
→ Cycle abgeschlossen
→ nächster Cycle
→ nach Cycle 2 Refill

Der wichtige Punkt ist:

Ihr kauft nicht blind Long nach,
sondern reduziert zuerst Long-Verlust kontrolliert
und benutzt die profitable Short-Seite,
um diesen Verlust zurückzuholen.



####################################################################################################

🧠 Strategie (einfach erklärt)
Start
Öffne nur Long
Beispiel: Long bei 100
Preis fällt −2%
Bei ~98 wird Short eröffnet
Jetzt hast du Hedge:
Long oben
Short unten
Spread berechnen
Abstand zwischen Long und Short in %
Beispiel:
Long 100
Short 98
→ Spread = 2%
Rebuy-Logik
Rebuy-Abstand = Spread / 3 (oder Band-Logik)
Beispiel:
2% / 3 = ~0.66%
Rebuys passieren unter der Short-Position
Nächster Long-Rebuy:
unter 98 (z. B. 97.3)

👉 Wichtig:

Rebuys orientieren sich immer an der Short-Seite
nicht am Long
📉 Beispiel komplett
Long: 100
Preis fällt → 98 → Short öffnet

Jetzt:

Spread = 2%
Rebuy-Distanz = 0.66%

👉 Rebuy-Level:

98 → 97.3 → 96.6 → ...
🎯 Ziel
Long-Ø nach unten ziehen
Spread kontrollieren
später bei Rebound:
Short TP
Long im besseren Durchschnitt schließen



💰 Profit-Logik (einfach erklärt)
Rebound kommt (Preis steigt)
Short geht ins Minus
Long geht in den Profit
Break-Even Punkt
Beispiel:
Long: +1$
Short: −1$
👉 Ergebnis = 0$ (Break-Even)
Ab hier beginnt echter Profit

Wenn Preis weiter steigt:

Long macht mehr Gewinn
Short verliert weiter, aber:

👉 Du nutzt den Vorteil, dass Long größer ist / besserer Durchschnitt

📈 Beispiel

Nach Rebuys:

Long: 120 Coins @ 98
Short: 50 Coins @ 98

Preis steigt auf 99:

Long Gewinn ≈ +120$
Short Verlust ≈ −50$

👉 Netto:
+70$ Profit

🎯 Ziel der Strategie
Erst:
👉 Break-Even erreichen (Long gewinnt = Short verliert)
Danach:
👉 Long überwiegt → echter Profit




################################# 2% Spread Beispiel ######################

Start: Long 1000$ @ 100
bei 98 wird Short 500$ @ 98 eröffnet
Start-Spread = 2.00%
Rebuy-Abstand = Spread / 3 = 0.666%
Size-Multiplier = 10%
also jeder Rebuy = 10% der aktuellen Long-Size
nach jedem Long-Rebuy wird Short wieder auf 50% der Long-Size ergänzt

Formel für Spread:

Spread % = (Long-Avg - Short-Avg) / Long-Avg * 100
Rebuy-Level

Ausgehend von Short @ 98 mit Step 0.666%:

Rebuy 1: 97.3467
Rebuy 2: 96.6977
Rebuy 3: 96.0530
Rebuy 4: 95.4127
Rebuy 5: 94.7766


Tabelle bis 5 Rebuys
Schritt	Preis	Size-Multiplier	Long	Short	Spread
Nach Short-Open	98.0000	–	1000.00 @ 100.0000	500.00 @ 98.0000	2.0000%
Nach Rebuy 1	97.3467	10%	1100.00 @ 99.7588	500.00 @ 98.0000	1.7630%
Nach Short-Add 1	97.3467	10%	1100.00 @ 99.7588	550.00 @ 97.9406	1.8226%
Nach Rebuy 2	96.6977	10%	1210.00 @ 99.4805	550.00 @ 97.9406	1.5479%
Nach Short-Add 2	96.6977	10%	1210.00 @ 99.4805	605.00 @ 97.8276	1.6615%
Nach Rebuy 3	96.0530	10%	1331.00 @ 99.1689	605.00 @ 97.8276	1.3525%
Nach Short-Add 3	96.0530	10%	1331.00 @ 99.1689	665.50 @ 97.6663	1.5152%
Nach Rebuy 4	95.4127	10%	1464.10 @ 98.8274	665.50 @ 97.6663	1.1749%
Nach Short-Add 4	95.4127	10%	1464.10 @ 98.8274	732.05 @ 97.4614	1.3822%
Nach Rebuy 5	94.7766	10%	1610.51 @ 98.4592	732.05 @ 97.4614	1.0134%
Nach Short-Add 5	94.7766	10%	1610.51 @ 98.4592	805.26 @ 97.2173	1.2613%



Was man daran sieht

Ganz wichtig:

Nach jedem Long-Rebuy sinkt der Spread
Nach jedem Short-Add steigt der Spread wieder leicht
aber dafür wird die Ratio wieder sauber auf 2:1 gebracht

Also genau:

Long-Rebuy heilt den Spread
Short-Add heilt die Ratio
Rebuy-Sizes in Dollar bei 10%

Nur damit man es direkt sieht:

Rebuy 1 = 100.00$
Rebuy 2 = 110.00$
Rebuy 3 = 121.00$
Rebuy 4 = 133.10$
Rebuy 5 = 146.41$

Short-Adds dazu:

Add 1 = 50.00$
Add 2 = 55.00$
Add 3 = 60.50$
Add 4 = 66.55$
Add 5 = 73.21$
Kurzfazit

Mit konstantem 10%-Multiplier wächst die Struktur so:

Long: 1000 → 1610.51
Short: 500 → 805.26
Ratio bleibt sauber bei 2:1



########################### Finale bot strategie #################################

    So arbeitet der Bot jetzt Schritt für Schritt:

    Long ist offen, dann kommt der Short dazu
    Sobald die Short-Position gefüllt ist und der Hedge komplett ist, kennt der Bot beide Durchschnittspreise: long_avg und short_avg.

    Er setzt sofort die Exit-Absicherung
    Aus diesen beiden Positionen berechnet er den gemeinsamen Break-Even.
    Danach setzt er direkt die Profit-/Exit-Orders auf dieses Ziel plus Aufschlag, also ungefähr BE + 1%.

    Danach setzt er sofort die nächste Long-Rebuy-Limit
    Jetzt wird der Spread zwischen Short und Long gemessen.
    Daraus berechnet er den nächsten Rebuy-Abstand, also z. B. Spread / 3 oder je nach Band /4 oder /5.
    Von der Short-Seite nach unten wird dann die neue Long-Limit gesetzt.
    Die Größe startet bei 10% und wird je nach Spread größer.

    Wenn diese Long-Rebuy gefüllt wird
    Dann zieht der Bot sofort den Short nach, damit das Verhältnis wieder stimmt.
    Ziel: Short soll wieder ungefähr 50% der Long-Position sein.

    Wenn dieser Short-Add gefüllt wird
    Dann berechnet der Bot alles neu mit den neuen Durchschnittspreisen:

    neuer Break-Even
    neuer Profit-Exit
    neuer nächster Long-Rebuy unter dem aktuellen Short
    Dieser Zyklus wiederholt sich immer weiter
    Also immer:

    Hedge komplett
    Exit setzen
    Rebuy Long setzen
    Long fill
    Short ausgleichen
    neue Exit-Orders
    nächste Rebuy Long
    Kurz gesagt:
    Der Bot arbeitet jetzt immer von der Short-Seite aus.
    Sobald der Hedge steht, setzt er erst Exit, dann Rebuy.
    Nach jedem Long-Fill setzt er erst den Short-Ausgleich, dann startet der nächste Zyklus wieder neu.





    ##############################################################################################

    ok und ich habe jetzt einen zweiten bot erstellt hier die logik dafur

So arbeitet der Bot jetzt Schritt für Schritt:

Long ist offen, dann kommt der Short dazu
Sobald die Short-Position gefüllt ist und der Hedge komplett ist, kennt der Bot beide Durchschnittspreise: long_avg und short_avg.

Er setzt sofort die Exit-Absicherung
Aus diesen beiden Positionen berechnet er den gemeinsamen Break-Even.
Danach setzt er direkt die Profit-/Exit-Orders auf dieses Ziel plus Aufschlag, also ungefähr BE + 1%.

Danach setzt er sofort die nächste Long-Rebuy-Limit
Jetzt wird der Spread zwischen Short und Long gemessen.
Daraus berechnet er den nächsten Rebuy-Abstand, also z. B. Spread / 3 oder je nach Band /4 oder /5.
Von der Short-Seite nach unten wird dann die neue Long-Limit gesetzt.
Die Größe startet bei 10% und wird je nach Spread größer.

Wenn diese Long-Rebuy gefüllt wird
Dann zieht der Bot sofort den Short nach, damit das Verhältnis wieder stimmt.
Ziel: Short soll wieder ungefähr 50% der Long-Position sein.

Wenn dieser Short-Add gefüllt wird
Dann berechnet der Bot alles neu mit den neuen Durchschnittspreisen:

neuer Break-Even
neuer Profit-Exit
neuer nächster Long-Rebuy unter dem aktuellen Short
Dieser Zyklus wiederholt sich immer weiter
Also immer:

Hedge komplett
Exit setzen
Rebuy Long setzen
Long fill
Short ausgleichen
neue Exit-Orders
nächste Rebuy Long
Kurz gesagt:
Der Bot arbeitet jetzt immer von der Short-Seite aus.
Sobald der Hedge steht, setzt er erst Exit, dann Rebuy.
Nach jedem Long-Fill setzt er erst den Short-Ausgleich, dann startet der nächste Zyklus wieder neu.

was ich jetzt vor habe ist sobald wir mit der burn strategie einen spread von 2% erreichen das wir dann den zweiten bot hier starten die idee ist einen zyklus durch zu fahren das sind 2 burns mit jeweils 0.6% burns und dann rebuy danach ist dann ca unser spread bei 0.8% dann nach den 1 burn starten wir dann sofort den zweiten bot um den spread wieder zu reduzieren und wenn die position size zu gross wird das mussen wir dann checken bis wie weit wir die positions size aufblahen konnen um den spread zu reduzieren hier ein kurzes reales beispiel aus den bot 

Zyklus	Zeitpunkt (Short-Add)	Long-Notional (USDT)	Spread nach Short-Add
Start (vor Zyklus 1)	—	ca. 79,9 $	1,33 %
1	15:06:39	ca. 99,9 $	1,1144 %
2	15:36:51	ca. 124,9 $	0,8941 %
3	15:36:58	ca. 156,1 $	0,7207 %
4	15:37:04	ca. 195,1 $	0,6434 %
5	15:37:06	ca. 230,0 $	0,5136 %
6	15:37:10	ca. 264,9 $	0,4362 %
7	15:37:14	ca. 299,8 $	0,3824 %
8	15:37:17	ca. 334,8 $	0,3348 %

wir hatten mit 1,33% spread begonnen hier kannst du sehen das wir bei 3 zyklen schon fast die halfte an spread reduzieren aber die size wird mehr als doppelt so gross die short size bleibt immer bei 50% 

ok sage mir mal was du davon halst und wie wir am besten die beiden bots kombinieren konnen 

weil vielleicht sollten wir dann wieder  nach den spread auf die halfte reduzieren wieder den burn bot starten bis wir irgendwann den rebound bekommen und wir konnen die order in be oder in profit schliessen 


oder hast du noch eine bessere idee wie wir die beiden startegien nutzen konnen