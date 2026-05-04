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


export TEST_SYMBOL=XVGUSDT
export TEST_CATEGORY=linear
python fixed_cycle_hedge_bot/test_trailing_long_sl.py


ok und der nachste schritt ist  wenn der preis dann um 0.5% fallt ist der aktivierungs preis und dann berechnen wir den stop price also ACTIVATION_DROP_PCT + stop order also in unseren fall ist das die 0.3% die wir dann auf den aktivierungs preis addieren mussen und da setzt wir jetzt mit der halfte der pos size eine stopLoss order also wir hangen sozusagen an unserer position die stopLoss order an also nicht als eine eigenstandige close limit order das ist wichtig weil wenn diese order gefullt wurde dann bekommen wir uber die rest api der position sehen wir dann sofort das die stopLoss order nicht mehr existiert das ist viel sauberer so und wenn der current price um 0.2% weiter fallt dann ist das unser neuer trigger aktivierungs preis wo wir dann wieder den neuen stopLoss price berechnen also +0.3% vom aktivierungs preis und immer so weiter bis der preis zuruck kommt und die order gefullt wurde hast du das verstanden

ok dann pass das so in den code an und gebe mir dann den kompletten code wieder zuruck als copy and paste 

Damit wird https://dash.immotel.de/dashboard/50_100_hedgebot auf http://127.0.0.1:3000/ weitergeleitet, so wie das Haupt-Dashboard auch.
Bitte starte danach nginx neu (z. B. sudo systemctl restart nginx oder den entsprechenden Service), damit der neue Link aktiv ist. Sag Bescheid, wenn ich dir noch beim Dashboard-Content oder beim Deployment-Skript helfen soll.


sudo systemctl restart dashboard.service

exit fallback market
reset state 



8.36 10:34 1/05



############################## Funding Rate long add profit short tp min 0.8% #####################################

Füge eine minimale Distanz-Guard für Short-TP-Follow-Up ein, ohne die Exit-Logik zu beeinflussen.

Ziel:
Verhindern, dass CYCLE_X_SHORT_REDUCE (Short-TP nach Long-Reduce) zu nah am aktuellen Preis gesetzt wird und dadurch sofort invalid/fallback wird.

WICHTIG:
Diese Änderung darf NUR für Short-TP-Follow-Up gelten.
Exit Orders (LONG_TP_EXIT, SHORT_SL_EXIT) dürfen NICHT verändert werden.

1. Config erweitern:

fixed_cycle_config.json:
"short_tp_min_distance_pct_after_long_reduce": 0.008

Dataclass:
short_tp_min_distance_pct_after_long_reduce: float = 0.008

2. Code-Anpassung:

Datei:
fixed_cycle_hedge_bot/fixed_cycle_strategy.py

Funktion:
_build_short_tp_follow_up

Direkt nach Berechnung von trigger_price_raw:

Füge ein:

current_price = snapshot.current_price

min_distance_pct = config.short_tp_min_distance_pct_after_long_reduce
safe_trigger_price = current_price * (1 - min_distance_pct)

original_trigger_price = trigger_price_raw

if trigger_price_raw is not None:
    trigger_price_raw = min(trigger_price_raw, safe_trigger_price)

Danach normal weiter mit Normalisierung (tick_size).

3. Logging hinzufügen:

Event:
"short_tp_min_distance_guard_applied"

Felder:
- current_price
- original_trigger_price
- safe_trigger_price
- final_trigger_price
- min_distance_pct
- cycle_index
- short_qty

4. WICHTIG:

NICHT ändern:
- _build_exit_intents
- LONG_TP_EXIT
- SHORT_SL_EXIT
- break-even / exit calculation

5. Test:

python -m py_compile fixed_cycle_hedge_bot/fixed_cycle_strategy.py

6. Erwartetes Verhalten:

- Kein sofortiger short_tp_invalid mehr
- Kein dauerhafter trailing_short_reduce Loop
- Stabilere Cycles auch bei Slippage / kleinen Losses

100 0.15
1000 1.50 = 1000/10= 100
10.000 15   10000/10 =1000
100.000 150

8.09