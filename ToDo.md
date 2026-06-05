66% plastic und 33% sand


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

udo journalctl -u coin_scanner.service --since "2026-05-17" --no-pager

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

######################################################################################################################

/home/telgenbuescher/projects/spread_recovery_hedge/modular_hedge_runtime/strategy.md

######################################################################################################################

Short:
SHORT_FIXED_SYMBOL=XRPUSDT bash live_bots/short_hedge_bot/short_bot_1/scripts/start.sh
bash live_bots/short_hedge_bot/short_bot_1/scripts/stop_with_cleanup.sh

Long:
SHORT_FIXED_SYMBOL=XRPUSDT bash live_bots/100_50_hedge_bot/long_bot_1/scripts/start.sh
live_bots/100_50_hedge_bot/long_bot_1/scripts/stop_with_cleanup.sh 

ok check jetzt wieder warum die cycle und exit orders nicht gestetzt wurden 

/home/telgenbuescher/projects/spread_recovery_hedge






Wichtig:
Bitte nichts Neues erfinden.
Erst den bestehenden Referenz-Long-Bot unter /home/telgenbuescher/projects/spread_recovery_hedge analysieren.
Dann die dort vorhandene Lösung exakt gespiegelt für den Short-Bot übernehmen.
Keine neue Architektur, keine neue State-Machine, keine alternativen Regeln.