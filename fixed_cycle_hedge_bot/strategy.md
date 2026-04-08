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

Die folgende Tabelle stammt aus der fruehen Konzeptphase. Sie beschreibt ein Beispiel fuer schrittweisen Abbau und den daraus resultierenden Breakeven-Effekt. Sie ist als Denkmodell nuetzlich, aber die Runtime arbeitet inzwischen fill-getrieben und nicht mehr nach einer statischen Tabelle.

| Zyklus | Kumulative Tiefe unter Avg % | Kumulativer Profit $ | Offener Long $ | Offener Short $ | Fee-Puffer $ (0,055%) | BE-% ueber Entry |
| ------ | ---------------------------: | -------------------: | -------------: | --------------: | --------------------: | ---------------: |
| Start  |                        0,00% |               0,0000 |       100,0000 |         50,0000 |                0,0825 |          0,1650% |
| 1      |                        0,15% |               0,0188 |        75,0000 |         37,5000 |                0,0619 |          0,1150% |
| 2      |                        0,30% |               0,0328 |        56,2500 |         28,1250 |                0,0464 |          0,0483% |
| 3      |                        0,45% |               0,0434 |        42,1875 |         21,0938 |                0,0348 |         -0,0406% |
| 4      |                        0,60% |               0,0513 |        31,6406 |         15,8203 |                0,0261 |         -0,1591% |
| 5      |                        0,75% |               0,0572 |        23,7305 |         11,8652 |                0,0196 |         -0,3171% |
| 6      |                        0,90% |               0,0617 |        17,7979 |          8,8989 |                0,0147 |         -0,5278% |
| 7      |                        1,05% |               0,0650 |        13,3484 |          6,6742 |                0,0110 |         -0,8087% |
| 8      |                        1,20% |               0,0675 |        10,0113 |          5,0056 |                0,0083 |         -1,1833% |
| 9      |                        1,35% |               0,0694 |         7,5085 |          3,7542 |                0,0062 |         -1,6827% |
| 10     |                        1,50% |               0,0708 |         5,6314 |          2,8157 |                0,0046 |         -2,3487% |
