## Phase 4 Engine Event Effects

### Fast Engine

| Event | Effect on Engine | Confidence Impact | Exclusions / Conflicts |
|---|---|---|---|
| `volatility_expansion` | raises instability, supports impulse classification | positive only with participation | weakened by `weak_move_low_participation` |
| `thin_orderflow_instability` | raises instability sharply | confidence down | conflicts with `high_participation_breakout` |
| `fresh_long_build_up` | increases long pressure | confidence up | conflicts with `fresh_short_build_up` |
| `fresh_short_build_up` | increases short pressure | confidence up | conflicts with `fresh_long_build_up` |
| `high_participation_breakout` | boosts participation and trend impulse | confidence up | reduced by `dirty_breakout_risk` |
| `weak_move_low_participation` | raises exhaustion, reduces participation | confidence down | cannot coexist as positive breakout confirmation |
| `panic_liquidation_phase` | raises instability and exhaustion | confidence down | if paired with aligned build-up, treat as unstable continuation not clean breakout |
| `squeeze_exhaustion_reversal` | raises exhaustion, supports reversal attempts | confidence conditional | conflicts with fresh same-direction build-up |
| `spread_stress_phase` | raises instability, lowers confidence | confidence down | conflicts with `high_participation_breakout` |
| `dirty_breakout_risk` | lowers clean breakout confidence, raises instability | confidence down | never direct trend confirmation |

### Mid Engine

| Event | Effect on Engine | Confidence Impact | Exclusions / Conflicts |
|---|---|---|---|
| `fresh_long_build_up` | confirms long continuation / reversal long | confidence up | weakened by `spread_stress_phase` |
| `fresh_short_build_up` | confirms short continuation / reversal short | confidence up | weakened by `spread_stress_phase` |
| `high_participation_breakout` | validates move quality | confidence up | weakened by `dirty_breakout_risk` |
| `weak_move_low_participation` | supports exhaustion over continuation | confidence down | never quality confirmation |
| `squeeze_exhaustion_reversal` | supports reversal setup | confidence up if slow is transitioning | conflicts with same-direction build-up |
| `spread_stress_phase` | quality penalty, can downgrade breakout to exhaustion | confidence down | no direct continuation confirmation |
| `panic_liquidation_phase` | supports exhaustion / capitulation context | confidence conditional | if no opposing context, avoid immediate reversal label |

### Slow Engine

| Event / Context | Effect on Engine | Confidence Impact | Exclusions / Conflicts |
|---|---|---|---|
| `fresh_long_build_up` | increases structural long conviction | confidence up | weakened by `squeeze_exhaustion_reversal` |
| `fresh_short_build_up` | increases structural short conviction | confidence up | weakened by `squeeze_exhaustion_reversal` |
| `atr_regime_zscore` high | context only, raises caution | neutral to slight down | not a trigger alone |
| `panic_liquidation_phase` | only extremity / exhaustion context | confidence down | no direct trend trigger |
| `spread_stress_phase` | market quality penalty | confidence down | no direct trend trigger |
| `squeeze_exhaustion_reversal` | exhaustion context, transition support | confidence down | cannot create trend by itself |
