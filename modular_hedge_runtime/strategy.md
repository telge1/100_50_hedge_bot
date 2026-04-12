# Dynamic Breakeven Strategy

Dieses Dokument beschreibt die fachliche Logik der ersten Strategie in `modular_hedge_runtime` und den aktuellen technischen Stand der Runtime drumherum.

## Ziel

Der Bot haelt gleichzeitig eine Long- und eine Short-Position und baut diese bei fallendem Markt kontrolliert ab. Ziel ist, echte Long-Verluste durch echte Short-Gewinne auszugleichen, sodass der gesamte Basket moeglichst bei `0` oder leicht positiv endet.

Beispiel:

- Long: `100 $`
- Short: `50 $`
- gleicher Entry auf beiden Seiten

Die Strategie arbeitet nicht mit einem starren Grid, sondern mit echten Fill-Preisen und aktuellen Restgroessen vom Exchange.

## Kernidee

Bei einem Rueckgang unter den Trigger reduziert die Strategie zuerst einen Teil der Long-Seite. Erst nachdem dieser Fill wirklich passiert ist, wird aus dem echten Fill-Preis und der echten Fill-Menge der noetige Short-Exit neu berechnet.

Wichtige Regeln:

- nicht mit alten Annahmen weiterrechnen
- nach jedem Fill und Reconcile wieder mit aktuellen Exchange-Daten arbeiten
- die Gegenseite immer dynamisch neu anpassen

## Formel-Logik

Nach einem Long-Reduce-Fill:

- `long_loss = long_fill_qty * (long_entry - long_fill_price)`
- `needed_short_move = long_loss / short_close_qty`
- `short_exit_price = short_entry - needed_short_move - edge`

Dabei gilt:

- `long_fill_qty`: tatsaechlich gefuellte Long-Menge
- `long_entry`: Entry der Long-Position
- `long_fill_price`: echter Fill-Preis der Long-Reduce-Order
- `short_close_qty`: die dazu geplante Short-Reduce-Menge
- `short_entry`: Entry der Short-Position
- `edge`: optionaler Sicherheits- bzw. Profit-Puffer fuer Fees und Slippage

Ziel:

- `Long-Verlust = Short-Gewinn`

## Aktueller Runtime-Ablauf

### Startup

Beim Start passiert aktuell:

1. optionales Laden des persistierten `strategy_state`
2. optionales Laden persistierter `active_orders`
3. Exchange-Checks fuer Hedge-Mode und Leverage
4. Startup-Recovery offener Orders vom Exchange
5. Snapshot-Aufbau aus aktuellen Positionen und Mark-Price
6. `on_start()` der Strategie

### Tick-Verhalten

Die `dynamic_breakeven_strategy`:

1. prueft, ob Long und Short noch offen sind
2. prueft, ob bereits ein offener Long-Reduce oder Short-Compensation-Order existiert
3. berechnet den Trigger
4. sendet bei Trigger einen `reduce_only` Market-Intent fuer die Long-Seite

Standardmaessig:

- Long-Reduce-Fraction: `0.33`
- Short-Reduce-Fraction: `0.33`
- Trigger: `1 %` unter Long-Avg
- Edge-Buffer: `0.05 %`

### Fill-Verhalten

Wenn der Long-Reduce fillt:

1. Long-Verlust wird aus echtem Fill berechnet
2. passender Short-Exit wird neu berechnet
3. es wird eine neue `reduce_only` Limit-Order fuer den Short gesetzt
4. alte offene Short-Compensation-Orders mit gleichem Purpose werden ersetzt

Wenn die Short-Compensation fillt:

1. der Zyklus wird als abgeschlossen markiert
2. die Strategie kann beim naechsten Trigger wieder neu arbeiten

## Wichtige Purposes

Die Runtime arbeitet intern mit `purpose`-Feldern, damit Orders fachlich sauber zugeordnet werden koennen.

Fuer die Dynamic-Breakeven-Strategie:

- `DYN_LONG_REDUCE`
- `DYN_SHORT_COMPENSATE`

Fuer die Beispielstrategie `basket_exit_strategy.py`:

- `BASKET_EXIT_LONG`
- `BASKET_EXIT_SHORT`

## Startup-Recovery

Die Runtime ist inzwischen deutlich gehaertet und kann offene Orders beim Neustart wieder in den lokalen State aufnehmen.

Recovery-Reihenfolge:

1. direkte Zuordnung ueber `orderLinkId`
2. Zuordnung ueber gespeichertes `exchange_order_id -> client_order_id` Mapping
3. Score-Matching gegen persistierte `active_orders`
4. heuristische Klassifizierung unbekannter Orders

### Score-Matching

Wenn kein direktes Mapping da ist, versucht die Runtime eine bekannte lokale Order wiederzufinden. Dabei werden unter anderem verglichen:

- `side`
- `reduce_only`
- `order_type`
- `qty`
- bei Limit-Orders auch `price`

Es wird absichtlich nur gematcht, wenn der Kandidat stark genug und eindeutig ist. Bei mehrdeutigen Kandidaten wird bewusst kein riskantes Mapping gemacht.

### Heuristische Klassifizierung

Wenn keine bekannte lokale Order sicher passt, wird die offene Exchange-Order trotzdem sinnvoll klassifiziert.

Dafuer nutzt die Runtime:

- `positionIdx`
- Exchange-`side`
- `reduceOnly`
- `orderType`

Beispiele:

- Dynamic Breakeven:
  - Short `reduceOnly` Limit -> `DYN_SHORT_COMPENSATE`
  - Long `reduceOnly` -> `DYN_LONG_REDUCE`
- Basket Exit:
  - Long `reduceOnly` -> `BASKET_EXIT_LONG`
  - Short `reduceOnly` -> `BASKET_EXIT_SHORT`

## Reconcile-Verhalten

Die Runtime synchronisiert offene Orders regelmaessig ueber REST:

1. Abgleich mit `fetch_open_orders()`
2. falls dort nicht gefunden: Lookup ueber `fetch_order_history()`
3. ggf. Ableitung fehlender Fills aus `cumExecQty` und `avgPrice`
4. Update lokaler Statuswerte
5. Entfernung terminaler Orders

Dadurch ueberlebt die Runtime auch WebSocket-Luecken oder Neustarts besser.

## Audit-Logging

Alle wichtigen Schritte werden als JSONL-Audit geschrieben.

Bereits enthalten:

- Snapshots
- Strategy-Traces mit Formeln, Inputs und Ergebnissen
- Intent-Erstellung und Order-Submission
- Fill-Verarbeitung
- Startup-Recovery
- Reconcile-Entscheidungen

### Wichtige Recovery-Events

- `startup_order_recovery_matched_existing`
- `startup_order_recovery_match_skipped`
- `startup_order_recovery_classified`
- `startup_order_recovery_attached`
- `startup_order_recovery_completed`

### Wichtige Reconcile-Events

- `reconcile_skipped`
- `reconcile_open_order_miss`
- `reconcile_history_miss`
- `reconcile_history_found`
- `reconcile_fill_inferred`
- `order_reconciled_open`
- `order_reconciled_partial`
- `order_reconciled_terminal`

## Tabelle aus der fruehen Idee

| Zyklus | Geschlossene Long Size | Geschlossene Short Size | Long-Verlust $ | Short-Gewinn $ | PnL pro Zyklus $ | Kumulative PnL $ | Long Size danach | Short Size danach |
| -----: | ---------------------: | ----------------------: | -------------: | -------------: | ---------------: | ---------------: | ---------------: | ----------------: |
|      1 |                15.0000 |                  7.5000 |       0.120000 |       0.138750 |         0.018750 |         0.018750 |          85.0000 |           42.5000 |
|      2 |                12.7500 |                  6.3750 |       0.102000 |       0.117938 |         0.015938 |         0.034688 |          72.2500 |           36.1250 |
|      3 |                10.8375 |                  5.4188 |       0.086700 |       0.100247 |         0.013547 |         0.048234 |          61.4125 |           30.7063 |
|      4 |                 9.2119 |                  4.6059 |       0.073695 |       0.085210 |         0.011515 |         0.059750 |          52.2006 |           26.1003 |
|      5 |                 7.8301 |                  3.9150 |       0.062641 |       0.072429 |         0.009788 |         0.069538 |          44.3705 |           22.1853 |
|      6 |                 6.6556 |                  3.3278 |       0.053245 |       0.061564 |         0.008320 |         0.077858 |          37.7150 |           18.8575 |
|      7 |                 5.6572 |                  2.8286 |       0.045258 |       0.052329 |         0.007072 |         0.084930 |          32.0578 |           16.0289 |
|      8 |                 4.8087 |                  2.4043 |       0.038469 |       0.044480 |         0.006012 |         0.090942 |          27.2491 |           13.6246 |
|      9 |                 4.0874 |                  2.0437 |       0.032699 |       0.037808 |         0.005110 |         0.096052 |          23.1617 |           11.5808 |
|     10 |                 3.4743 |                  1.7371 |       0.027794 |       0.032137 |         0.004343 |         0.100391 |          19.6874 |            9.8437 |



###################################### Strategie #################################################

Start positions 100$ long 50$ short bei 100$ geoffnet

preis fallt um 0,8% vom long_avg price Long close SL order mit 15% reduzieren Fill Preis in json speichern 
Fur Short Positions Preis berechnen nach den Long Fill Preis auf 1.6%+ Puffer (0.25%)
danach wiederholt sich der nachste zyklus 
Exit nach jeden Fill neu berechnen z.b long 1$ Profit und short 1$ verlust = be + 0.5% Profit + Puffer (0.25%)


was ist jetzt wenn wir zum long sl fill auch die halfte von den short schliessen um 7.5% und die restlichen 7.5%
bei 1.6%+ Puffer (0.25%) ich glaube das ist besser so weil wir einmal die ratio heilen und schneller in exit kommen 
konnen oder was sagst du dazu

########################################################################################################################

Nicht:

nur Long -15%

Sondern:

Long -15% bei -0.8%
Short -7.5% sofort am selben Event
Short -7.5% später bei Rebound auf

Start: Long 100 / Short 50
Bei -0.8%:
Long -15%
Short -7.5% sofort
Long-Fill-Preis speichern
Bei long_fill_price + 1.85%:
restliche Short -7.5%
Nach jedem Fill:
Exit neu berechnen auf Basket-Basis

sondern 


Exit nach jeden Fill neu berechnen z.b long 1$ Profit und short 1$ verlust = be + longorder verlust umgerechnet in % + 0.5% Profit + Puffer (0.25%) verstehst du also wenn wir ein dollar verlust gemacht haben dann mussen wir berechnen um wieiviel prozent der preis noch hoch gehen muss nach be um diesen verlust zu decken 

Exit = Basket-BE + Verlust-Rückholung + Zielprofit + Puffer

########################################################################################################################

rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_hedge_runtime.log
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/generic_hedge_runtime_audit.jsonl

./start_fixed_cycle.sh

"target_profit_usdt": 0.002,  =? 0.2$

2025-01-23 12:34:56,789 INFO runtime.fixed_cycle Fixed cycle fill handling started: {"fill": {...}, ...}


########################################################################################################################

Start:
100$ Long
50$ Short
beide bei gleichem Startpreis
Wenn der Preis um 0.8% unter long_avg fällt:
Long-SL / Long-Reduce auslösen
15% Long reduzieren
echten Fill-Preis speichern
Danach:
Short-TP nur für diese Cycle-Stufe berechnen
Basis = Long-Fill-Preis
Ziel = 0.8% + 0.25% Puffer unter dem Long-Fill-Preis
Danach:
Exit nach jedem Fill neu berechnen
und erst dann nächster Cycle




Start: Long 100 / Short 50
Bei -0.8%:
Long -15%
Long-Fill-Preis speichern
Bei long_fill_price + 1.85%:
restliche Short -15%
Nach jedem Fill:
Exit neu berechnen auf Basket-Basis


#####################################################

