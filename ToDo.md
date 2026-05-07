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


6.62 12:23



100 0.15
1000 1.50 = 1000/10= 100
10.000 15   10000/10 =1000
100.000 150




############### Event-Audit / Self-Healing-Controller ##########################

NORMAL RUNNING:
- LONG_TP_EXIT muss da sein
- SHORT_SL_EXIT muss da sein
- CYCLE_X_LONG_ADD oder CYCLE_X_SHORT_TP muss da sein

FINAL EXIT IN PROGRESS:
- keine neuen Cycle-Orders setzen
- keine Initial Entries setzen
- warten bis beide Exit-Legs und PnL bestätigt sind

REFILL:
- REFILL_LONG / REFILL_SHORT prüfen
- keine normalen Cycle-Orders erzwingen

FLAT + PNL NICHT FERTIG:
- keine Initial Entries
- PnL holen
- offene alte Orders bereinigen


Nicht nur reagieren, wenn etwas crasht,
sondern nach jedem Event beweisen:
"Meine Struktur ist gesund."