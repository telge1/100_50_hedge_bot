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



LOW COST PROMPT + Ziel + Scope“