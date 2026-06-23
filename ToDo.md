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





