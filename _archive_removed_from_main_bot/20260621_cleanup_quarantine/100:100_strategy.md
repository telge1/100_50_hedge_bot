Start
Long 100
Short 100
Wenn du denkst, Preis fällt weiter
Short +20
dann:
Long 100
Short 120
Wenn der Preis dann doch steigt
Long +20
dann:
Long 120
Short 120

Jetzt seid ihr wieder glatt, aber etwas größer.

Dann das gleiche wieder
Wenn du wieder denkst, Preis fällt
wieder Short +20
dann:
Long 120
Short 140
Wenn Preis dann wieder steigt
wieder Long +20
dann:
Long 140
Short 140
Wichtiger Punkt

Die 20 $ bleiben immer gleich.

Also:

nicht 20 % von 120
nicht 20 % von 140
immer einfach nur 20 $

Dadurch wächst der Hedge:

gleichmäßig
langsamer
kontrollierter
Kurzform zum Speichern

Ping-Pong-Hedge mit fixer Add-Size

Start mit 100 Long / 100 Short
Add-Größe ist immer 20 $
20 $ = 20 % der ursprünglichen Start-Size
Wenn Fall erwartet wird: Short +20
Wenn es doch steigt: Long +20
Dann sind beide Seiten wieder gleich groß
Danach wieder dasselbe spiegelverkehrt
Die Add-Größe bleibt immer fix bei 20 $
Sie wird nicht auf die neue Gesamtgröße neu berechnet
Beispiel
100 / 100
100 / 120
120 / 120
120 / 140
140 / 140
Vorteil
einfacher
kontrollierter
kein exponentielles Wachstum
nur lineares Wachstum
Nachteil
Hedge wird trotzdem mit der Zeit größer
deshalb braucht man später noch Regeln zum Rückbau
Noch kürzer

Regel:
Immer nur mit 20 $ festen Adds arbeiten, basierend auf der Startgröße 100 $.

Nicht:

20 % von der aktuellen Größe

Sondern:

immer einfach +20 $ pro Schritt.



##################################################
100 : 100 bridge to 100 : 50

Idee:

Wenn wir im 100 : 100 Hedge eingefroren sind, wechseln wir nicht direkt hart zurück
auf 100 : 50.

Wir gehen in kleinen festen Schritten zurück.

Ziel:

100 : 100
-> 100 : 80
-> 100 : 60
-> 100 : 50

Wichtig:

Wir bauen immer nur den Short ab.
Den Long lassen wir stehen.

Warum:

Wenn der Markt nach dem Freeze wieder hoch kommt oder sich beruhigt,
dann wollen wir die zu starke Short-Last langsam abbauen
und wieder in unsere normale Struktur zurückkehren.

##################################################
Bridge-Regeln

1. Solange der Markt weiter fällt:
- nichts umbauen
- 100 : 100 halten
- kein Wechsel auf 100 : 80

2. Erst wenn der Markt sichtbar stabiler wird oder reboundet:
- ersten Block Short abbauen
- z. B. 20 $
- dann:
  100 : 100 -> 100 : 80

3. Wenn der Markt weiter stabil bleibt oder weiter hoch zieht:
- nächsten Block Short abbauen
- wieder 20 $
- dann:
  100 : 80 -> 100 : 60

4. Wenn der Markt weiter sauber bleibt:
- letzten kleinen Block abbauen
- dann:
  100 : 60 -> 100 : 50

5. Wenn der Markt unterwegs wieder kippt:
- sofort stoppen
- auf der aktuellen Bridge-Stufe bleiben
- also z. B. bei 100 : 80 oder 100 : 60 einfach halten
- keinen weiteren Short abbauen

##################################################
Einfach erklärt

100 : 100 ist unser Notfall-Hedge.

Wenn der Markt danach wieder ruhiger wird,
dann bauen wir die extra Short-Seite Stück für Stück ab,
bis wir wieder bei 100 : 50 angekommen sind.

Wir machen das nicht auf einmal,
sondern immer nur in 20-$-Schritten.

##################################################
Übergabe zurück an unsere normalen Bots

Sobald wir ungefähr bei 100 : 60 oder 100 : 50 angekommen sind
und der Markt nicht mehr chaotisch ist,
kann wieder unsere normale Burn-/Repair-Logik übernehmen.

Also:

100 : 100 = Emergency / Freeze
100 : 80 = Bridge
100 : 60 = fast wieder normal
100 : 50 = zurück in Standardstruktur


####################### simpel strategie #######################

Erweiterte Tabelle
Phase	Aktion	Preis	Long Size	Long Avg	Short Size	Short Avg	Spread	Ratio L:S
Start	Initial	100 / 97	1000.00	100.0000	1000.00	97.0000	3.00%	1.00
R1	Long +100	96.03	1100.00	99.6391	1000.00	97.0000	2.65%	1.10
R2	Long +100	95.07	1200.00	99.2583	1000.00	97.0000	2.28%	1.20
R3	Long +150	93.88	1350.00	98.6607	1000.00	97.0000	1.68%	1.35
S1	Short +100	92.94	1350.00	98.6607	1100.00	96.6310	2.06%	1.23
S2	Short +150	92.94	1350.00	98.6607	1250.00	96.1882	2.51%	1.08
L4	Long +100	92.01	1450.00	98.1972	1250.00	96.1882	2.05%	1.16
PB1	25% Short-Close + Long Burn	91.0899	1225.83	98.1972	937.50	96.1882	2.05%	1.31
PB2	25% Short-Close + Long Burn	90.1790	1050.18	98.1972	703.13	96.1882	2.05%	1.49
PB3-lite	15% Short-Close + Long Burn	89.2772	968.47	98.1972	597.66	96.1882	2.05%	1.62
PB4-lite	15% Short-Close + Long Burn	88.3844	897.18	98.1972	508.01	96.1882	2.05%	1.77
PB5-mini	10% Short-Close + Long Burn	87.5006	855.92	98.1972	457.21	96.1882	2.05%	1.87
PB6-mini	10% Short-Close + Long Burn	86.6256	818.14	98.1972	411.49	96.1882	2.05%	1.99
PB7-mini	10% Short-Close + Long Burn	85.7593	783.64	98.1972	370.34	96.1882	2.05%	2.12
PB8-micro	5% Short-Close + Long Burn	84.9017	767.88	98.1972	351.82	96.1882	2.05%	2.18
PB9-micro	5% Short-Close + Long Burn	84.0527	752.87	98.1972	334.23	96.1882	2.05%	2.25
PB10-micro	5% Short-Close + Long Burn	83.2122	738.63	98.1972	317.52	96.1882	2.05%	2.33
+40 Schritte 55.6666 552.36 115.33 4.79




1,5% spread

Phase	Aktion	Preis	Long Size	Long Avg	Short Size	Short Avg	Spread	Ratio L:S
Start	Initial	100 / 98.5	1000.00	100.0000	1000.00	98.5000	1.50%	1.00
R1	Long +100	97.5150	1100.00	99.7741	1000.00	98.5000	1.28%	1.10
R2	Long +100	96.5399	1200.00	99.5046	1000.00	98.5000	1.01%	1.20
R3	Long +150	95.3331	1350.00	99.0411	1000.00	98.5000	0.55%	1.35
S1	Short +100	94.3798	1350.00	99.0411	1100.00	98.1254	0.92%	1.23
S2	Short +150	94.3798	1350.00	99.0411	1250.00	97.6760	1.38%	1.08
L4	Long +100	93.4360	1450.00	98.6545	1250.00	97.6760	0.99%	1.16
PB1	25% Short-Close + Long Burn	92.5016	1187.20	98.6545	937.50	97.6760	0.99%	1.27
PB2	25% Short-Close + Long Burn	91.5766	985.23	98.6545	703.13	97.6760	0.99%	1.40
PB3-lite	15% Short-Close + Long Burn	90.6608	892.67	98.6545	597.66	97.6760	0.99%	1.49
PB4-lite	15% Short-Close + Long Burn	89.7542	812.88	98.6545	508.01	97.6760	0.99%	1.60
PB5-mini	10% Short-Close + Long Burn	88.8567	767.15	98.6545	457.21	97.6760	0.99%	1.68
PB6-mini	10% Short-Close + Long Burn	87.9681	725.62	98.6545	411.49	97.6760	0.99%	1.76
PB7-mini	10% Short-Close + Long Burn	87.0884	687.95	98.6545	370.34	97.6760	0.99%	1.86
PB8-micro	5% Short-Close + Long Burn	86.2175	670.89	98.6545	351.82	97.6760	0.99%	1.91
PB9-micro	5% Short-Close + Long Burn	85.3554	654.59	98.6545	334.23	97.6760	0.99%	1.96
PB10-micro	5% Short-Close + Long Burn	84.5018	639.04	98.6545	317.52	97.6760	0.99%	2.01





Setup
Long Start: 1000 @ 100
Short Start: 1000 @ 98
Start-Spread: 2%
Tabelle (2% Startspread)
Phase	Aktion	Preis	Long Size	Long Avg	Short Size	Short Avg	Spread	Ratio L:S
Start	Initial	100 / 98	1000.00	100.0000	1000.00	98.0000	2.00%	1.00
R1	Long +100	97.0200	1100.00	99.7291	1000.00	98.0000	1.73%	1.10
R2	Long +100	96.0498	1200.00	99.4225	1000.00	98.0000	1.43%	1.20

R3	Long +150	94.8492	1350.00	98.9143	1000.00	98.0000	0.92%	1.35


S1	Short +100	93.9007	1350.00	98.9143	1100.00	97.6273	1.30%	1.23
S2	Short +150	93.9007	1350.00	98.9143	1250.00	97.1801	1.75%	1.08
L4	Long +100	92.9617	1450.00	98.5038	1250.00	97.1801	1.34%	1.16
PB1	25% Short-Close + Long Burn	92.0321	1201.42	98.5038	937.50	97.1801	1.34%	1.28
PB2	25% Short-Close + Long Burn	91.1117	1009.01	98.5038	703.12	97.1801	1.34%	1.44
PB3-lite	15% Short-Close + Long Burn	90.2006	920.35	98.5038	597.66	97.1801	1.34%	1.54
PB4-lite	15% Short-Close + Long Burn	89.2986	843.60	98.5038	508.01	97.1801	1.34%	1.66
PB5-mini	10% Short-Close + Long Burn	88.4056	799.46	98.5038	457.21	97.1801	1.34%	1.75
PB6-mini	10% Short-Close + Long Burn	87.5216	759.25	98.5038	411.49	97.1801	1.34%	1.85
PB7-mini	10% Short-Close + Long Burn	86.6464	722.69	98.5038	370.34	97.1801	1.34%	1.95
PB8-micro	5% Short-Close + Long Burn	85.7799	706.10	98.5038	351.82	97.1801	1.34%	2.01
PB9-micro	5% Short-Close + Long Burn	84.9221	690.22	98.5038	334.23	97.1801	1.34%	2.07
PB10-micro	5% Short-Close + Long Burn	84.0729	675.04	98.5038	317.52	97.1801	1.34%	2.13





Phase	Aktion	Preis	Long Size	Long Avg	Short Size	Short Avg	Spread	Ratio
Start	Initial	100 / 98.5	1000	100.0000	1000	98.5000	1.52%	1.00
R1	Long +100	97.5150	1100	99.7741	1000	98.5000	1.28%	1.10
R2	Long +100	96.5399	1200	99.5046	1000	98.5000	1.01%	1.20
R3	Long +150	95.3331	1350	99.0411	1000	98.5000	0.55%	1.35
S1	Short +100	94.3798	1350	99.0411	1100	98.1254	0.92%	1.23
S2	Short +150	94.3798	1350	99.0411	1250	97.6760	1.38%	1.08
L4	Long +100	93.4360	1450	98.6545	1250	97.6760	0.99%	1.16