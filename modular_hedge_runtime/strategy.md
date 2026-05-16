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

./bot_control.sh hard-reset

rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_hedge_runtime.log && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/generic_hedge_runtime_audit.jsonl && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_calc_audit.log && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_runner.stdout.log

./start_fixed_cycle.sh

####################################### Start/Stop Bot ##########################################################

/home/telgenbuescher/projects/spread_recovery_hedge/scripts/restart_fixed_cycle.sh
/home/telgenbuescher/projects/spread_recovery_hedge/scripts/stop_fixed_cycle.sh

####################################### Multiple Start/Stop Bot #################################################

./live_bots/100_50_hedge_bot/shared_scripts/start_long_bot.sh long_bot_1
./live_bots/100_50_hedge_bot/shared_scripts/stop_with_cleanup.sh long_bot_1

./live_bots/100_50_hedge_bot/shared_scripts/create_bot_env.sh long_bot_3 --with-wrappers

####################################### Main Bot ################################################################

/home/telgenbuescher/projects/spread_recovery_hedge/fixed_cycle_hedge_bot/fixed_cycle_strategy.py

####################################### Dashboard ###############################################################

sudo systemctl daemon-reload
sudo systemctl enable --now dashboard
sudo systemctl restart dashboard

sudo systemctl status dashboard

####################################### Add Bot ###################################################################

 live_bots/100_50_hedge_bot/shared_scripts/create_bot_env.sh long_bot_5 --with-wrappers

 ./live_bots/100_50_hedge_bot/shared_scripts/create_bot_env.sh long_bot_6 --with-wrappers --register-dashboard


[block_marker] bot_restart timestamp=... symbol=<Symbol>

####################################### Watchdog ##################################################################

python live_bots/100_50_hedge_bot/shared_scripts/safety_order_watchdog.py --dry-run --once
python3 live_bots/100_50_hedge_bot/watchdog/safety_order_watchdog.py --loop --interval 10

cd /home/telgenbuescher/projects/spread_recovery_hedge
PYTHONPATH=. python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py --loop --interval 10

####################################### Wallet Captcher ###########################################################

python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py \
  --capture-start-wallet \
  --bot-name long_bot_1

################################## Coin Scanner ###################################################################

sudo systemctl stop coin_scanner.timer

sudo systemctl stop coin_scanner.service

sudo systemctl daemon-reload
sudo systemctl start coin_scanner.service
sudo systemctl start coin_scanner.timer

###################################################################################################################

python analyze_hedge_logs.py --mode blocks

python3 /home/telgenbuescher/projects/spread_recovery_hedge/fixed_cycle_hedge_bot/tools/coin_scanner.py


python fixed_cycle_hedge_bot/tools/simulator.py \
  --mode single \
  --start-price 100 \
  --drop-pct 6 \
  --step-pct 0.1 \
  --long-add-grid 0.5,0.6,0.7

  --long-add-distance-pct 0.8


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


Start mit 100 / 50
LONG ADD bei 0.5%
Nach 2 vollständigen Zyklen resetten
Dann wieder neu 100 / 50 aufbauen

Start: Long 100 / Short 50
Bei -0.8%:
Long -15%
Long-Fill-Preis speichern
Bei long_fill_price + 1.85%:
restliche Short -15%
Nach jedem Fill:
Exit neu berechnen auf Basket-Basis


################################## exit order kalkulation ###################

🔥 FINAL EXIT-LOGIK (DEIN SYSTEM)
🧠 GRUNDPRINZIP
Wir rechnen IMMER:

Long-Gewinn
- Short-Verlust
+ bereits realisierte Gewinne/Verluste
= Zielprofit
📊 1️⃣ Ziel definieren
profit_basis_usdt = long_qty * long_avg

target_profit_usdt =
profit_basis_usdt * tp_profit_target_pct / 100

buffer_usdt =
profit_basis_usdt * tp_buffer_pct / 100

👉 Beispiel:

Long = 100$
tp_profit_target_pct = 0.55%

target_profit_usdt = 0.55$
🔁 2️⃣ Realisierte PnL tracken (WICHTIG)
realized_cycle_net =
Summe aller bisherigen Cycle-Ergebnisse

👉 Beispiele:

❌ Nur Long Add Verlust
Long Add = -0.20$

realized_cycle_net = -0.20
✅ Danach Short TP Gewinn
Short TP = +0.30$

realized_cycle_net = -0.20 + 0.30 = +0.10

👉 Jetzt hast du +0.10$ Guthaben

🎯 3️⃣ Was muss der Exit noch holen?
required_profit_usdt =
target_profit_usdt
+ buffer_usdt
- realized_cycle_net
🔥 Beispiele
Fall A – Verlust vorhanden
realized_cycle_net = -0.20

required_profit_usdt =
0.55 - (-0.20)
= 0.75$

👉 Exit muss höher → Verlust zurückholen

Fall B – Gewinn vorhanden
realized_cycle_net = +0.10

required_profit_usdt =
0.55 - 0.10
= 0.45$

👉 Exit kann tiefer → Gewinn schon teilweise erreicht

🧮 4️⃣ FINAL EXIT PREIS
exit_price =
(
(long_avg * long_qty)
- (short_avg * short_qty)
+ required_profit_usdt
)
/
(long_qty - short_qty)
🧠 INTUITION (WICHTIG!)
Du hast ein Konto (Cycle-PnL Konto):

- Verlust → Schulden → Exit höher
- Gewinn → Guthaben → Exit tiefer
🔁 5️⃣ UPDATE-REGEL

👉 Nach JEDEM Fill:

Long Add ❗
Short TP ❗
→ realized_cycle_net neu berechnen
→ required_profit_usdt neu berechnen
→ exit_price neu berechnen
→ Exit Orders neu setzen
🚨 WICHTIGSTE REGEL
Du darfst NIEMALS nur pending_loss betrachten

Du brauchst IMMER:
realized_cycle_net = Verluste + Gewinne
🔑 MERKSATZ (ABSOLUT FINAL)
Exit = aktueller Hedge-Zustand
     + (Zielprofit - bereits verdienter Gewinn + Verlust) / Netto-Position

oder einfacher:

Was fehlt noch bis Ziel → das muss der Exit holen
✅ DEIN BEISPIEL (GENAU RICHTIG)
Long Add = -0.20
Short TP = +0.30

→ realized_cycle_net = +0.10

→ required_profit_usdt = 0.55 - 0.10 = 0.45

→ Exit wird tiefer gesetzt ✅