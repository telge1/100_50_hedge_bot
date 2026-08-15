# APT Multi-Start Netto-BE Validation

**Decision: `APT_MULTISTART_PASS_WITH_WARNINGS`**

Primary objective: reach true net break-even robustly, early, with bounded overlay exposure and capital — not max profit.

## Setup

- Starts eligible: **154**
- Policies: shared_be, individual_tp_2p00, individual_tp_scaled
- Net-BE threshold: target=0.0 + safety_buffer=0.25 (= +0.25 USDT net)
- Seeding: relative notional-invariant (reference start uses absolute audit seed)

## Answers

1. **Starts tested:** 154 eligible starts × 3 policies = 462 runs

2/3. **individual_tp_scaled** recovered_rate=0.857 (132/154), unresolved_rate=0.143
2/3. **individual_tp_2p00** recovered_rate=0.805 (124/154), unresolved_rate=0.195
2/3. **shared_be** recovered_rate=0.130 (20/154), unresolved_rate=0.870

3. **Lowest unresolved rate:** `individual_tp_scaled` (0.14285714285714285)
4. **Smallest worst drawdown:** `shared_be` (60.302001302345076)
5. **Lowest p90 overlay:** `individual_tp_2p00` (940.834188)
6. **Lowest p90 capital:** `shared_be` (2159.0573225000003)
7. **Fastest BE (median/p90):** `shared_be` median=11.01388888888889 p90=34.4138888888889

8. **Unresolved market paths:** see `market_path_groups.csv` (drop buckets typically dominate).

9. **Safety/invariant:** total safety_violation runs = 0; see `safety_violations.csv`.

10. **Clearest robust policy (lexicographic):** `individual_tp_scaled`

11. **Multi-Coin readiness:** review `worst_cases.csv` / unresolved before multi-coin; if decision is PASS or PASS_WITH_WARNINGS, single-APT robustness is sufficient to proceed to a careful multi-coin pilot.

## Policy summary

| policy | recovered_rate | unresolved_rate | safety | median_days | p90_ov | p90_cap | worst_dd |
|---|---|---|---|---|---|---|---|
| individual_tp_scaled | 0.8571428571428571 | 0.14285714285714285 | 0 | 13.052083333333332 | 993.24946124 | 2281.81904888 | 223.56501262012512 |
| individual_tp_2p00 | 0.8051948051948052 | 0.19480519480519481 | 0 | 17.98263888888889 | 940.834188 | 2269.81788192 | 257.4815757362998 |
| shared_be | 0.12987012987012986 | 0.8701298701298701 | 0 | 11.01388888888889 | 961.12504016 | 2159.0573225000003 | 60.302001302345076 |

## Decision

`APT_MULTISTART_PASS_WITH_WARNINGS`
