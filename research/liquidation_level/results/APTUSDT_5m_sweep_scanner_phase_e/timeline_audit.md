# Phase E Timeline Audit

Momentum candles start at `decision_index + 1` (scanner age 0).
`break_close` is forced to `decision_close` after the age-0 update.
Forward metrics start at `confirming_candle_index + 1` (never include confirmation).

- phase_d_hash: `cf301399bde97d95d81016ba14ca0a52471beaa6514a70d9ee241833bec42a2a`
- leakage_passed: **True**
- timeline_samples: 50

## Sample rows

```
  event_id rule_family  decision_offset  momentum_window confirmation_direction  confirmation_age  confirming_candle_index  decision_index   phase_e_state    cohort
OPT_000269          R3                6                2                   long               0.0                   1607.0            1606  BULL_CONFIRMED confirmed
OPT_002009          R3                6                2                   long               1.0                   7823.0            7821  BULL_CONFIRMED confirmed
OPT_001238          R3                6                3                   long               0.0                   5065.0            5064  BULL_CONFIRMED confirmed
OPT_006709          R2                1                3                   long               0.0                  22820.0           22819  BULL_CONFIRMED confirmed
OPT_009626          R5                6                2                   long               1.0                  33517.0           33515  BULL_CONFIRMED confirmed
OPT_013108          R5                6                2                  short               0.0                  45775.0           45774 SHORT_CONFIRMED confirmed
OPT_004708          R3                6                3                   long               1.0                  17472.0           17470  BULL_CONFIRMED confirmed
OPT_006374          R2                6                2                   long               0.0                  21917.0           21916  BULL_CONFIRMED confirmed
OPT_006976          R2                3                2                  short               0.0                  23322.0           23321 SHORT_CONFIRMED confirmed
OPT_001235          R3                6                2                   long               0.0                   5065.0            5064  BULL_CONFIRMED confirmed
OPT_012272          R2                3                3                   long               0.0                  42587.0           42586  BULL_CONFIRMED confirmed
OPT_005358          R3                6                3                   long               2.0                  18907.0           18904  BULL_CONFIRMED confirmed
OPT_000967          R2                6                3                   long               2.0                   3144.0            3141  BULL_CONFIRMED confirmed
OPT_005311          R2                1                3                   long               0.0                  18734.0           18733  BULL_CONFIRMED confirmed
OPT_009627          R5                6                2                   long               1.0                  33517.0           33515  BULL_CONFIRMED confirmed
OPT_002701          R4                6                3                  short               0.0                  10592.0           10591 SHORT_CONFIRMED confirmed
OPT_001450          R5                6                2                   long               0.0                   5091.0            5090  BULL_CONFIRMED confirmed
OPT_009102          R3                6                3                   long               0.0                  31592.0           31591  BULL_CONFIRMED confirmed
OPT_002139          R2                3                3                   long               0.0                   8472.0            8471  BULL_CONFIRMED confirmed
OPT_010836          R2                6                3                   long               0.0                  37546.0           37545  BULL_CONFIRMED confirmed
```

