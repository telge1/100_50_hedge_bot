# APT Winner Forensic Order Audit (T1 / 6%)

**Decision: `APT_WINNER_FORENSIC_AUDIT_PASS_WITH_WARNINGS`**

Multi-blocker release allowed: **True**

## Answers

1. Start causal (T1): **True** (00:00 open dist=0.0522; trigger close dist=0.0818; fill `2026-01-19T00:05:00+00:00` @ `1.6447`)
2. Neutralization exact/qty-neutral: **True** (short_avg→`1.791289264225859`, fee=`0.08934405128000003`)
3. Qty/tick/fees: see add/BE/cashflow audits; configured add≈118.546
4. Order events: **25** (implicit-trigger model; no OMS cancel/replace stream)
5. Fill events: **26**
6. Cancelled/replaced OMS orders: **0** (not modeled)
7. Overlay adds correct qty: **16/16**
8. Shared-BE rounds audited: **7** (PASS=7)
9. Same-candle multi-event bars: **9**
10. Same-candle causality: PASS=8 WARNING=1 FAIL=0
11. Reference reset: see `reference_reset_audit.csv` / overlay_rounds
12. Full exit: state=`RECOVERED` reason=`recovered_profit`
13. Cashflow/fee reconciliation: see `cashflow_reconciliation.csv`
14. Pure overlay PnL (A): **46.149957799999854**
15. Cobertura total exit econ (B): **21.858019294808667**
16. Prior TEM loss covered by Cobertura B? **B alone does not include TEM**; combined D=B+C=`9.957886192741164` (covers prior loss net-positive: **True**)
17. Combined incl. TEM (D): **9.957886192741164** quality=`PASS_WITH_UNRESOLVED_PRIOR_FEES`
18. Fee uncertainty: `FEE_RECONSTRUCTION_UNRESOLVED`; neut fee outside engine=`0.08934405128000003`
19. Hard invariant failures: **0** soft=**0**; same-candle WARNING bars=1
20. Use as multi-blocker basis?: **True**

Decision: `APT_WINNER_FORENSIC_AUDIT_PASS_WITH_WARNINGS`

