66% plastic und 33% sand

rosmarin, thymian, petersilie, oregano

python run_psrh.py --config path/to/your/config.yaml



/home/telgenbuescher/projects/burn_reentry_simple


nach 3 burns a 0,6%
haben wir die long size von 100$ auf 60$ reduziert mit einen spread von 2%
danach reduzieren wir den spread auf die halfte und wir 
erhalten danach wieder eine long size von 112.30$ und short von 56.06$ bei einen spread von 0.98% 



Logik:
spread/3
base_multiplier = 0.25
base_step = 0.0025
max_rebuy_usdt = 20
short_add = final_only
Ergebnis:
final_long ≈ 112.30$
final_short ≈ 56.06$
final_ratio ≈ 0.4992
final_spread ≈ 0.9894%


Hier kommt die korrekte Tabelle mit Spread/Long/Short jeweils nach dem Short-Add (bzw. nach dem finalen Reset auf Ratio 0.5) – Start war Long 60$, Short 30$, Spread 2%, Repair mit final_only (alles läuft erst, dann Short-Add):

Zyklus	Long-Notional	Short-Notional	Spread	Short/Long-Ratio	Short-Add-Notional
1	73.40 $	29.80 $	1.4660 %	0.4060	0.00 $ (noch kein Short-Add)
2	92.25 $	46.04 $	1.2450 %	0.4991	16.19 $
3	112.14 $	56.03 $	1.0267 %	0.4997	10.03 $
4	132.11 $	65.97 $	0.8731 %	0.4994	9.94 $
5	152.08 $	76.00 $	0.7598 %	0.4998	10.03 $




###########################################################

~/projects/burn_reentry_simple/liquidation_strategy 


python -m strategy.market_regime.cli analyze-live-state-quality --limit 2000
python -m strategy.market_regime.cli review-live-state-quality --limit 2000
python -m strategy.market_regime.cli analyze-live-state-quality --symbol BTCUSDT --limit 1000
python -m strategy.market_regime.cli analyze-live-state-quality --symbol ETHUSDT --limit 1000


#######################################################


Heal / Rebuild → eigene Seite



Paired Short-Heal → Long-Close Mechanismus sauber in diese Phase-Logik integrieren
aber so viel wie ich weiss haben wir das doch schon gemacht oder 


WICHTIG:
Ich will einen echten Patch-Diff sehen, so dass Cursor mir die Änderungen zum Accept/Keep-all anbietet.


👉 dynamische TP-Optimierung (z. B. abhängig von Volatilität / Spread)
oder
👉 Exit früher triggern bei schnellen Rebounds




########################################################################################################################

./bot_control.sh hard-reset

rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_hedge_runtime.log && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/generic_hedge_runtime_audit.jsonl && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_calc_audit.log && \
rm /home/telgenbuescher/projects/spread_recovery_hedge/logs/fixed_cycle_runner.stdout.log

./start_fixed_cycle.sh

################################## Coin Scanner ###################################################################

sudo systemctl stop coin_scanner.timer

sudo systemctl stop coin_scanner.service

sudo systemctl daemon-reload
sudo systemctl start coin_scanner.service
sudo systemctl start coin_scanner.timer

sudo journalctl -u coin_scanner.service --since "2026-05-17" --no-pager


sudo systemctl status coin_scanner.service --no-pager
sudo systemctl status coin_scanner.timer --no-pager

Uberschreiben:
sudo chown telgenbuescher:telgenbuescher logs/best_coin.json
chmod 644 logs/best_coin.json


Symbol‑Reservation für die Short‑Gruppe zurücksetzen
rm -f live_bots/short_hedge_bot/state/active_bot_symbols.json
printf '{}\n' > live_bots/short_hedge_bot/state/active_bot_symbols.json


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

short api ausgabe
https://dash.immotel.de/profit-verlauf_2?bot_side=short&profile=bot_1&page=0&page_size=500

long api ausgabe
https://dash.immotel.de/api/dashboard/profit-trades?profile=bot_1&bot_side=long&page=0&page_size=500

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

live_bots/100_50_hedge_bot/shared_scripts/start_hedge_guard_watchers.sh
live_bots/100_50_hedge_bot/shared_scripts/stop_hedge_guard_watchers.sh

cd ~/projects/spread_recovery_hedge

python3 live_bots/100_50_hedge_bot/watchdog/wallet_refill_watchdog.py \
  --loop \
  --interval 30 \
  --enable-transfer \
  --no-transfer-dry-run \
  --transfer-config-file config/config.yaml \
  --transfer-coin USDT \
  --min-transfer-amount 1 \
  --transfer-cooldown-seconds 600 \
  --rebaseline-on-start \
  --reset-transfer-cooldown-on-start

########################################### Reserve coin #############################################################

cat live_bots/100_50_hedge_bot/state/active_bot_symbols.json | jq .

alte reserve coins loschen 
rm -f live_bots/100_50_hedge_bot/long_bot_1/run/reserved_best_coin.json
rm -f live_bots/short_hedge_bot/short_bot_1/run/reserved_best_coin.json

rm -f live_bots/100_50_hedge_bot/long_bot_1/run/fixed_cycle_config.runtime.json
rm -f live_bots/short_hedge_bot/short_bot_1/run/fixed_cycle_config.runtime.json

rm -f live_bots/state/pair_symbol_bot_1.json

######################################################################################################################

/home/telgenbuescher/projects/spread_recovery_hedge/modular_hedge_runtime/strategy.md

######################################################################################################################

############################ Time-Distance-Refill nur: ###############################

CYCLE_2_SHORT_REDUCE filled
+ CYCLE_2_LONG_REDUCE offen
+ Trade älter als X Minuten
+ Preis bei 50% zwischen Avg und CYCLE_2_LONG_REDUCE

Danach:
bestehender Refill-Mode
+ nächsten Cycle-Refill einmal skippen

############################################# Neue teil tp order regel ###############

Für jede normale Cycle-Followup-TP:

1. Berechne normale TP-Qty.
2. Prüfe, ob qty in 3 gültige Orders teilbar ist.
3. Wenn ja → baue 3 Teil-TPs.
4. Wenn nein, prüfe, ob qty in 2 gültige Orders teilbar ist.
5. Wenn ja → baue 2 Teil-TPs.
6. Wenn nein → baue 1 normale TP-Order.

jede Teilorder >= min_order_qty
jede Teilorder * trigger_price >= min_notional

###########################################################################################

Refill lief aus Sicht dieses Logs sauber durch.
Cycle nach Refill läuft korrekt weiter.
Kein Refill hängt offen.
Kein doppelter Rebuild/keine doppelte Refill-Order sichtbar.
Aber: Closed-PnL-Retry für alte Reduce-Orders muss noch gefixt oder begrenzt werden, sonst bleibt der Log dauerhaft voll mit Retry-Versuchen.





############################################## Backtester ################################################################

cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev || exit 1

PYTHONPATH=. python3 -m research.backtests.run_original_hedge_backtest \
  --symbol APTUSDT \
  --direction long \
  --limit 20000 \
  --continuous-reentry \
  --fill-model conservative \
  --config-source live \
  --pnl-coverage-audit

PYTHONPATH=. python3 -m research.backtests.run_original_hedge_backtest \
  --symbol APTUSDT \
  --direction long \
  --limit 20000 \
  --continuous-reentry \
  --config-source live \
  --debug \
  --print-config-diagnostics



Rebound Recovery Reload

Trigger:

Wenn active_order = CYCLE_N_SHORT_REDUCE
und Preis steigt wieder bis ca. 0.75% unter Long Avg / Exit Avg,
dann cancel CYCLE_N_SHORT_REDUCE,
führe Recovery Reload aus,
z.B. +100 Long / +50 Short oder ratio-adjusted +109.454 / +50,
danach Exit Orders neu berechnen.

Wichtig:

nur 1x pro Trade
nur ab erstem Refill oder ab Cycle 3+
nur wenn Mindestorder erfüllt
nur wenn Wallet-Transfer im Backtest sauber simuliert wird
danach Debt / offenen Verlust in Exit neu einrechnen



Aktuell passiert ungefähr:

Cycle wird tiefer
Long-Add/Reduce wird relativ groß
realized_pnl wird stärker negativ
Short-Reduce muss mehr Gewinn holen
Short-Reduce-Preis liegt weiter weg
Trade hängt

Deine Idee dreht das um:

Cycle wird tiefer
Long-Reduce/Add wird kleiner
realized_pnl-Verlust wird kleiner
Short-Reduce muss weniger Gewinn holen
Short-Reduce-Preis liegt näher am Markt
Trade kann eher schließen



## Idee: Rebound Recovery Reload nahe Avg

Wenn ein Trade nach mehreren Cycles tief gelaufen ist und aktuell in einer weit entfernten `CYCLE_N_SHORT_REDUCE` hängt, soll der Bot nicht starr auf diesen tiefen Short-Reduce warten.

Stattdessen prüfen wir bei einem Rebound:

* aktive Order ist `CYCLE_N_SHORT_REDUCE`
* Preis steigt wieder bis ca. `0.75%` unter den aktuellen Avg-/Exit-Bereich
* Trade hatte bereits mindestens einen Refill
* Position ist durch vorherige Long-/Short-Reduces kleiner oder ratio-schief geworden

Dann wird die offene Short-Reduce-Order gecancelt und ein **Recovery Reload** gesetzt.

Ziel:

* Position wieder vergrößern
* Long/Short-Ratio möglichst nahe an der Zielstruktur halten, z.B. `2:1`
* Long-Avg und Short-Avg neu berechnen
* offene Recovery-Verluste/Debt auf die neuen Exit-Orders umlegen
* dadurch Exit-Orders deutlich näher an den neuen Avg bringen

Beispiel aus aktuellem Trade:

Vor Reload:

```text
Long qty:  28.366
Short qty: 18.910
```

Recovery Reload nahe Avg:

```text
+100 Long qty
+50 Short qty
```

oder ratio-sicher:

```text
+109.454 Long qty
+50 Short qty
```

Danach wird alles neu berechnet:

```text
neuer Long avg
neuer Short avg
neue Ratio
neue Exit-Orders inklusive offenem Recovery-Druck
```

Wichtig: Diese Logik soll nur Backtest-only getestet werden, mit Guardrails:

* maximal 1 Rebound Recovery Reload pro Trade
* nur nahe Avg, z.B. `0.75%` darunter
* nur nach mindestens einem normalen Refill
* Mindestorder beachten
* offene Cycle-Order vorher canceln
* danach Exits sauber neu setzen
* PnL/Debt transparent im Backtest ausweisen


########################################################

Variante B: progressiv weniger decken

Das wäre eher dein Vorschlag:

Cycle 3: 80% decken, 20% Debt
Cycle 4: 75% decken, 25% Debt
Cycle 5: 70% decken, 30% Debt
Cycle 6: 65% decken, 35% Debt


| Cycle | Cover Ratio | Required Now | neuer SR Trigger | Abstand vom Long-Add-Fill | neuer Debt | Debt total |
| ----: | ----------: | -----------: | ---------------: | ------------------------: | ---------: | ---------: |
|     3 |         80% |       0.5564 |           1.8651 |                     1.82% |     0.1353 |     0.1353 |
|     4 |         75% |       0.8736 |           1.7687 |                     3.47% |     0.2862 |     0.4216 |
|     5 |         70% |       1.0804 |           1.6471 |                     2.98% |     0.4566 |     0.8782 |
|     6 |         65% |       1.5732 |           1.4857 |                     5.06% |     0.8390 |     1.7172 |



####################################### TRade Stuck ##############################################

| Reihenfolge | Zeit             | Cycle | Order                | Seite | Fill / Order Preis | Bedeutung                 |
| ----------: | ---------------- | ----: | -------------------- | ----- | -----------------: | ------------------------- |
|           1 | 2026-01-05 18:55 | Start | INITIAL_LONG_ENTRY   | Long  |         **1.9830** | Start Long                |
|           2 | 2026-01-05 18:55 | Start | INITIAL_SHORT_ENTRY  | Short |         **1.9830** | Start Short               |
|           3 | 2026-01-05 19:05 |    C1 | CYCLE_1_LONG_ADD     | Long  |         **1.9731** | Long Reduce / Cycle 1     |
|           4 | 2026-01-05 19:50 |    C1 | CYCLE_1_SHORT_REDUCE | Short |         **1.9585** | Short deckt C1            |
|           5 | 2026-01-05 20:50 |    C2 | CYCLE_2_LONG_ADD     | Long  |         **1.9487** | Long Reduce / Cycle 2     |
|           6 | 2026-01-06 16:05 |    C2 | CYCLE_2_SHORT_REDUCE | Short |         **1.9092** | Short deckt C2            |
|           7 | 2026-01-06 16:40 |    C3 | CYCLE_3_LONG_ADD     | Long  |         **1.8997** | Long Reduce / Cycle 3     |
|           8 | 2026-01-08 06:05 |    C3 | CYCLE_3_SHORT_REDUCE | Short |         **1.8415** | Short deckt C3            |
|           9 | 2026-01-08 06:15 |    C4 | CYCLE_4_LONG_ADD     | Long  |         **1.8323** | Long Reduce / Cycle 4     |
|          10 | 2026-01-19 00:00 |    C4 | CYCLE_4_SHORT_REDUCE | Short |         **1.7061** | Short deckt C4            |
|          11 | 2026-01-19 00:05 |    C5 | CYCLE_5_LONG_ADD     | Long  |         **1.6976** | Long Reduce / Cycle 5     |
|          12 | 2026-01-20 08:10 |    C5 | CYCLE_5_SHORT_REDUCE | Short |         **1.5727** | Short deckt C5            |
|          13 | 2026-01-20 08:35 |    C6 | CYCLE_6_LONG_ADD     | Long  |         **1.5648** | Long Reduce / Cycle 6     |
|          14 | offen / stuck    |    C6 | CYCLE_6_SHORT_REDUCE | Short |         **1.3064** | **nicht gefüllt / stuck** |


| Zeitpunkt                 | Auslöser                      | Long Qty danach | Short Qty danach | Neuer Long Avg | Neuer Short Avg |
| ------------------------- | ----------------------------- | --------------: | ---------------: | -------------: | --------------: |
| Start                     | Initial Entry                 |          50.428 |           25.214 |     **1.9830** |      **1.9830** |
| Nach erstem Refill-Block  | nach C2 Short Reduce / Refill |          50.427 |           25.214 | **1.95338234** |  **1.95347019** |
| Nach zweitem Refill-Block | nach C4 Short Reduce / Refill |          50.427 |           25.213 | **1.81833879** |  **1.81846513** |
| Stuck Zustand             | nach C6 Long Reduce           |          28.366 |           18.910 | **1.81833879** |  **1.81846513** |





| Linie                           |               Preis |
| ------------------------------- | ------------------: |
| Start Avg                       |          **1.9830** |
| Avg nach Refill 1               |          **1.9534** |
| Avg nach Refill 2               | **1.8183 / 1.8185** |
| C1 Long Fill                    |          **1.9731** |
| C1 Short Fill                   |          **1.9585** |
| C2 Long Fill                    |          **1.9487** |
| C2 Short Fill                   |          **1.9092** |
| C3 Long Fill                    |          **1.8997** |
| C3 Short Fill                   |          **1.8415** |
| C4 Long Fill                    |          **1.8323** |
| C4 Short Fill                   |          **1.7061** |
| C5 Long Fill                    |          **1.6976** |
| C5 Short Fill                   |          **1.5727** |
| C6 Long Fill                    |          **1.5648** |
| C6 Short Reduce Trigger / Stuck |          **1.3064** |



| Level                 |  Preis | Abstand zu 1.81833879 |
| --------------------- | -----: | --------------------: |
| C4 Short Reduce       | 1.7061 |            **-6.17%** |
| C5 Long Add           | 1.6976 |            **-6.64%** |
| C5 Short Reduce       | 1.5727 |           **-13.51%** |
| C6 Long Add           | 1.5648 |           **-13.94%** |
| C6 Short Reduce Stuck | 1.3064 |           **-28.15%** |


ab der CYCLE_2_LONG_ADD order  fur short tp auf 1% reduzieren verlust umlegen 

und vor avg price bei unter 0.75% recovery reload 

Relief Cap = 1.55%


===== NUR CYCLE FILL PREISE FÜR CHART =====
1.231000  # CYCLE_1_LONG_ADD long qty=20.206000
1.231000  # CYCLE_1_LONG_ADD long qty=20.206000
1.221900  # CYCLE_1_SHORT_REDUCE short qty=10.057000
1.221900  # CYCLE_1_SHORT_REDUCE short qty=10.057000
1.215800  # CYCLE_2_LONG_ADD long qty=15.155000
1.215800  # CYCLE_2_LONG_ADD long qty=15.155000
1.191100  # CYCLE_2_SHORT_REDUCE short qty=7.589000
1.191100  # CYCLE_2_SHORT_REDUCE short qty=7.589000
1.185100  # CYCLE_3_LONG_ADD long qty=20.206000
1.185100  # CYCLE_3_LONG_ADD long qty=20.206000
1.149400  # CYCLE_3_SHORT_REDUCE short qty=10.095000
1.149400  # CYCLE_3_SHORT_REDUCE short qty=10.095000
1.143700  # CYCLE_4_LONG_ADD long qty=15.155000
1.143700  # CYCLE_4_LONG_ADD long qty=15.155000
1.098000  # CYCLE_4_SHORT_REDUCE short qty=7.579000
1.098000  # CYCLE_4_SHORT_REDUCE short qty=7.579000
1.092500  # CYCLE_5_LONG_ADD long qty=20.206000
1.092500  # CYCLE_5_LONG_ADD long qty=20.206000
1.048800  # CYCLE_5_SHORT_REDUCE short qty=10.096000
1.048800  # CYCLE_5_SHORT_REDUCE short qty=10.096000

===== NUR REFILL FILLS + AVG DANACH =====

===== LETZTER REKONSTRUIERTER AVG / POSITION =====
long_qty:  262.683000
long_avg:  1.192712
short_qty: 0.000000
short_avg: 0.000000

===== FINAL ACTIVE EXIT ORDERS =====
LONG_TP_EXIT @ 1.248700 qty=60.621000 status=NEW
SHORT_SL_EXIT @ 1.248700 qty=30.316000 status=NEW

===== LETZTE EXIT INTENTS MIT CARRY LOSS =====
2026-02-01T16:45:00+00:00 | LONG_TP_EXIT @ 1.247500 qty=80.827000 carry=0.0 exit_adj_pct=
2026-02-01T16:45:00+00:00 | SHORT_SL_EXIT @ 1.247500 qty=40.413000 carry=0.0 exit_adj_pct=
2026-02-01T17:20:00+00:00 | LONG_TP_EXIT @ 1.247500 qty=80.827000 carry=0.0 exit_adj_pct=
2026-02-01T17:20:00+00:00 | SHORT_SL_EXIT @ 1.247500 qty=40.413000 carry=0.0 exit_adj_pct=
2026-02-01T23:05:00+00:00 | LONG_TP_EXIT @ 1.247500 qty=60.621000 carry=0.0 exit_adj_pct=
2026-02-01T23:05:00+00:00 | SHORT_SL_EXIT @ 1.247500 qty=30.356000 carry=0.0 exit_adj_pct=
2026-02-01T23:10:00+00:00 | LONG_TP_EXIT @ 1.247500 qty=60.621000 carry=0.0 exit_adj_pct=
2026-02-01T23:10:00+00:00 | SHORT_SL_EXIT @ 1.247500 qty=30.356000 carry=0.0 exit_adj_pct=
2026-02-05T05:20:00+00:00 | LONG_TP_EXIT @ 1.228100 qty=80.827000 carry=0.0 exit_adj_pct=
2026-02-05T05:20:00+00:00 | SHORT_SL_EXIT @ 1.228100 qty=40.412000 carry=0.0 exit_adj_pct=
2026-02-05T10:05:00+00:00 | LONG_TP_EXIT @ 1.228100 qty=80.827000 carry=0.0 exit_adj_pct=
2026-02-05T10:05:00+00:00 | SHORT_SL_EXIT @ 1.228100 qty=40.412000 carry=0.0 exit_adj_pct=
2026-02-05T11:20:00+00:00 | LONG_TP_EXIT @ 1.228100 qty=60.621000 carry=0.0 exit_adj_pct=
2026-02-05T11:20:00+00:00 | SHORT_SL_EXIT @ 1.228100 qty=30.317000 carry=0.0 exit_adj_pct=
2026-02-05T11:25:00+00:00 | LONG_TP_EXIT @ 1.228100 qty=60.621000 carry=0.0 exit_adj_pct=
2026-02-05T11:25:00+00:00 | SHORT_SL_EXIT @ 1.228100 qty=30.317000 carry=0.0 exit_adj_pct=
2026-02-05T15:20:00+00:00 | LONG_TP_EXIT @ 1.184200 qty=80.827000 carry=0.24064840799999831 exit_adj_pct=1.3436029097132924
2026-02-05T15:20:00+00:00 | SHORT_SL_EXIT @ 1.184200 qty=40.412000 carry=0.24064840799999831 exit_adj_pct=1.3436029097132924
2026-02-05T15:25:00+00:00 | LONG_TP_EXIT @ 1.184200 qty=80.827000 carry=0.24064840799999831 exit_adj_pct=1.3436029097132924
2026-02-05T15:25:00+00:00 | SHORT_SL_EXIT @ 1.184200 qty=40.412000 carry=0.24064840799999831 exit_adj_pct=1.3436029097132924
2026-02-05T20:20:00+00:00 | LONG_TP_EXIT @ 1.248700 qty=60.621000 carry=0.5950180079999974 exit_adj_pct=6.863500213949493
2026-02-05T20:20:00+00:00 | SHORT_SL_EXIT @ 1.248700 qty=30.316000 carry=0.5950180079999974 exit_adj_pct=6.863500213949493
(base) telgenbuescher@server-telgenbuescher:~/projects/spread_recovery_hedge_short_dev$ 