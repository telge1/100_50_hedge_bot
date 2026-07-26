# Multi-Blocker Forensic Audit

**Decision: `MULTI_BLOCKER_FORENSIC_AUDIT_PASS_WITH_WARNINGS`**

Policy: `shared_be_t1_6pct` (T1 close→next-open, 6% start distance, shared_be)
APT regression: `APT_REGRESSION_PASS`

## Answers

1. Exakt testbare Blocker: **25** (unresolved **2**)
2. Recovery V0 bis 30/60/90/120d: **2/2/2/2**
3. Offen nach 120d (V0): **23**
4. Combined positiv (V0): **2**
5. Summe Cobertura-PnL (V0, inkl. Neut-Fee): **-578.6263672914864**
6. Summe Combined-PnL (V0): **-855.1652490533094**
7. Median/Worst Drawdown (V0): **17.28860273940013** / **35.70476801086996**; Median/Worst Peak Gross: **1718.8124519999997** / **2631.6687628**
8. Größte Drawdown-Risiken (Coin, dd, peak_gross): `[('ADAUSDT', 35.70476801086996, 1754.454604), ('XRPUSDT', 31.65931675433, 2631.6687628), ('SEIUSDT', 29.96646911297585, 2305.1505972), ('AVAXUSDT', 29.52793872624991, 2063.8874769999998), ('DOTUSDT', 23.417795531189892, 1751.1799500000002)]`
9. Same-Candle Add+Exit Fälle (V0): **2**
10. Baseline-Winner die unter V3 Winner bleiben: **0/2**
11. Winner→open/unresolved bei next_bar_exit: **0** `[]`
12. Kipper durch Gap-Varianten: **3** `['APTUSDT|two_early_medium|continuous|0006', 'TIAUSDT|two_early_medium|continuous|0007', 'TIAUSDT|two_early_medium|continuous|0007']`
13. APT reproduziert Einzel-Audit: **True**
14. Invariant fails (V0 sum): **0**; V3: **0**
15. Policy technisch für weitere Forschung freigegeben?: **Ja**

Decision: `MULTI_BLOCKER_FORENSIC_AUDIT_PASS_WITH_WARNINGS`
