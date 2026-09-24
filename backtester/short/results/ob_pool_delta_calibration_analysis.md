n# OB + Public Trade + Pool Calibration Analysis

## Goal

For each long signal, measure how far price actually traveled, which upper liquidity pools were crossed, and at which pool the move slowed, bounced, or reversed. The goal is to calibrate pool selection together with OB / public-trade delta, not separately.

## What we want to learn

1. Which upper pools are only micro-noise and get eaten quickly.
2. Which upper pools act as real reaction points.
3. How far strong positive delta typically carries price beyond the first pool.
4. Whether the strongest pool above price is usually the one price reaches before stalling.
5. How OB and public-trade delta behave at:
   - entry
   - first pool approach
   - first meaningful pool touch
   - maximum excursion
   - reversal / rejection point

## Core analysis unit

One row per signal.

Recommended key fields:

- `signal_ts`
- `symbol`
- `direction`
- `entry_price`
- `max_price_before_reversal`
- `max_excursion_pct`
- `confirm_delta`
- `followthrough_delta`
- `entry_ob_ratio`
- `max_ob_ratio`
- `first_upper_pool_bottom`
- `first_upper_pool_top`
- `first_upper_pool_strength`
- `second_upper_pool_bottom`
- `second_upper_pool_top`
- `second_upper_pool_strength`
- `highest_reached_pool_bottom`
- `highest_reached_pool_top`
- `highest_reached_pool_strength`
- `first_bounce_pool`
- `rejection_pool`
- `tp_mode_if_simulated`

## Pool ladder to record

For each signal, list all upper pools above entry in price order.

For every pool, record:

- `bottom`
- `top`
- `distance_from_entry_pct`
- `pool_height_bps`
- `strength`
- `status`
- `crossed_by_price` yes/no
- `acted_as_reaction_point` yes/no

## Event timeline per signal

### 1. Entry snapshot

Record the state at signal time:

- entry price
- confirm delta
- followthrough delta
- OB ratio
- all upper pools above entry

### 2. Approach phase

For each candle or tick window after entry:

- price high
- delta in the same window
- OB ratio in the same window
- closest pool reached so far

This phase tells us whether the market is simply drifting into weak pools or building enough flow to continue into stronger pools.

### 3. First pool touch

When price first reaches the nearest upper pool, record:

- delta at touch
- OB ratio at touch
- whether price cleanly pierced the pool
- whether it immediately continued to the next pool
- whether it rejected back into the prior range

### 4. Strongest-pool test

Repeat the same logic for the strongest meaningful upper pool above entry:

- was it reached?
- if yes, did price pause there?
- if no, which lower pool stopped the move first?

## Classification for each pool

Label each pool with one of these outcomes:

- `ignored` - price never came close
- `passed_through` - price crossed with little reaction
- `brief_pause` - small hesitation, then continuation
- `reaction_point` - visible bounce / slowdown
- `reversal_point` - move failed there

## Cluster mass and reachability

For the next stage of analysis, pools are not only judged by distance from entry.
The more important signal is the **mass of the cluster** and how price reacts when it
enters that zone.

For every cluster, record:

- number of pools in the cluster
- sum of `strength`
- average `strength`
- cluster width in price / bps
- strongest single pool inside the cluster
- whether the price only passed through, paused, or actually reversed there

For the cluster where price turns back down, also record:

- cluster size at the reversal point
- total cluster strength at reversal
- average pool strength at reversal
- whether the reversal happened at the first touch or after a brief overshoot

### Reachability rule to test

We also want to measure whether a cluster becomes realistically reachable only when
delta is strong enough.

For each cluster, test:

- distance from entry
- cluster mass
- delta at the time of approach
- OB ratio at the time of approach
- whether the cluster was reached at all

This should answer the question:

- which clusters are only reachable with very strong delta
- which clusters are easy to reach even with moderate delta
- which clusters are basically unreachable and should not be TP targets

### Working hypothesis

- near weak clusters are mostly noise
- distant clusters are only valid TP targets if they have enough mass
- a large cluster can still be the correct target even if it is farther away
- if delta is not strong enough, a far cluster should be treated as unlikely

## Calibration questions

For the full signal set, answer:

1. At which pool-strength threshold does a pool stop being noise?
2. At which cluster-mass threshold does a cluster become a real TP candidate?
3. How often does strong positive delta carry price from the first pool into the second meaningful cluster?
4. When price breaks the first pool cleanly, which cluster most often becomes the actual reaction point?
5. Do the strongest clusters above entry line up better with reversals than the nearest clusters?
6. Does OB confirmation improve the odds that price reaches the strongest upper cluster before stalling?

## Aggregations to produce

### Per signal

- max excursion
- highest reached pool
- reaction pool
- delta at entry
- delta at first pool
- delta at reversal

### Per pool band

Group pools by distance from entry:

- `0.0% - 0.5%`
- `0.5% - 1.0%`
- `1.0% - 1.5%`
- `1.5% - 2.5%`
- `2.5%+`

For each band, compute:

- average strength
- hit rate
- rejection rate
- average delta at touch
- average max excursion after touch

### Per strength bucket

Group pools by strength percentiles or fixed ranges:

- weak
- medium
- strong
- strongest

For each bucket, compute:

- how often price reaches them
- how often price rejects there
- how often price blows through them

## Output format

For every signal, produce a compact summary table with:

- signal timestamp
- entry
- max price
- max excursion
- strongest pool reached
- first pool touched
- rejection pool
- delta at touch
- OB ratio at touch

Then produce a cross-signal summary:

- average max excursion
- strongest-pool hit rate
- first-pool pass-through rate
- rejection rate by pool band
- relationship between delta strength and pool reach

## Constraints

- Use only data known at the signal time or before each candle/tick being evaluated.
- Do not use future pools or future flow to score the signal.
- Keep pool order causal: entry -> first upper pool -> next meaningful upper pool -> stronger clusters above.

## Locked signal set (long only)

Symbol: `DOGEUSDT`

| # | signal_ts (UTC) | entry_price |
|---|-----------------|-------------|
| 1 | 2026-09-07 08:45 | 0.08965 |
| 2 | 2026-09-07 19:35 | 0.09024 |
| 3 | 2026-09-08 20:25 | 0.08990 |
| 4 | 2026-09-11 02:15 | 0.08353 |
| 5 | 2026-09-13 14:25 | 0.08363 |
| 6 | 2026-09-14 02:15 | 0.08361 |
| 7 | 2026-09-15 17:50 | 0.08226 |
| 8 | 2026-09-15 23:30 | 0.08041 |
| 9 | 2026-09-16 18:15 | 0.07956 |
| 10 | 2026-09-17 23:20 | 0.08167 |

## Reference case first

Signal `#1` `2026-09-07 08:45`:

- `entry_price = 0.08965`
- known local high near `0.09187`
- use this as the first walkthrough for pool reach + delta timeline
- then apply the same structure to signals `#2`–`#10`

## Raw probe dump

Machine-readable per-signal pool path:
`results/ob_pool_delta_calibration_signal_probe.json`

## Probe result — Signal #1 only

`DOGEUSDT` · `2026-09-07 08:45` · Entry `0.08965` · timeframe pools: **1m ACTIVE upper**

### Entry snapshot

| Field | Value |
|-------|-------|
| tier | `tier2_strong` |
| confirm_delta | `+364,413` |
| followthrough_delta | `+497,538` |
| entry OB bid/ask (5bps) | `4.50` (sehr bid-heavy) |
| max high | `0.091880` @ `13:20` |
| max excursion | `+2.487%` |

### Upper pool ladder at entry (13 ACTIVE)

| # | bottom–top | dist% | strength | first touch | path-to-max label |
|---|------------|-------|----------|-------------|-------------------|
| 1 | 0.08967–0.08978 | 0.02 | 6.12 | 08:45 | passed_through_after_pause |
| 2 | 0.08969–0.08972 | 0.05 | 0.13 | 08:45 | passed_through_after_pause |
| 3 | 0.08997–0.09006 | 0.36 | 3.91 | 09:20 | passed_through_after_pause |
| 4 | 0.08998–0.09003 | 0.37 | 1.26 | 09:20 | passed_through_after_pause |
| 5 | 0.09002–0.09004 | 0.41 | 0.11 | 09:20 | passed_through_after_pause |
| 6 | 0.09004–0.09011 | 0.44 | 1.63 | 09:20 | passed_through_after_pause |
| 7 | 0.09012–0.09025 | 0.52 | 3.14 | 12:15 | passed_through_after_pause |
| 8 | 0.09039–0.09049 | 0.83 | 3.39 | 12:20 | passed_through_after_pause |
| 9 | 0.09057–0.09068 | 1.03 | 1.59 | 12:35 | passed_through_after_pause |
| 10 | 0.09058–0.09067 | 1.04 | 0.96 | 12:35 | passed_through_after_pause |
| 11 | 0.09113–0.09128 | 1.65 | 3.47 | 12:45 | passed_through_after_pause |
| 12 | **0.09125–0.09145** | **1.79** | **6.14** | **12:50** | passed_through_after_pause |
| 13 | 0.09134–0.09155 | 1.89 | 5.32 | 12:50 | passed_through_after_pause |

### Delta / OB timeline

| Moment | d10 (notional) | OB ratio | price note |
|--------|----------------|----------|------------|
| entry bar close | +251k | 4.50 | still below first meaningful stack |
| 09:20 early pool touch | +363k | 1.10 | pierces ~0.0900 band, then consolidates |
| 12:15–12:35 mid ladder | +81k → +349k → +211k | ~1.0–2.3 | resume leg through mid pools |
| 12:45–12:50 strong cluster | **+1.94M → +2.19M** | 1.09 → **2.84** | hits strongest pools |
| 13:20 max high | +231k | 1.90 | high `0.09188`, then fades |

### Probe takeaways

1. **All 13 upper pools were eventually reached** — none stayed `ignored` on the way to the high.
2. **Near / mid pools (< ~1.0%) were not the exit** — price paused/consolidated, then ate them on the later leg (`passed_through_after_pause`).
3. **Strongest pool at entry was `0.09125–0.09145` (str 6.14)** — aligned with the chart Ziel zone; price tagged it with extreme positive delta (~+2.2M).
4. **True exhaustion was slightly above that cluster** (`0.09188`, ~+2.5%), just beyond pool tops `0.09145–0.09155` — so the strong cluster was the last magnet, not a hard wall at the exact edge.
5. **Delta pattern**: strong at entry → still strong at first stack (09:20) → **explodes** into the strong cluster → **collapses** at the high. That matches “carry to strong pool, then stop”.
6. **Implication for TP calibration (hypothesis only):** with `tier2_strong` + strong OB, weak intermediates should be skipped; TP candidate = strongest upper cluster (~+1.8%), not first micro pool (~+0.02–0.4%).

## Cross-signal comparison (all 10 longs)

Method: same as #1 — 1m ACTIVE upper pools at entry, walk 5m highs for 12h, classify each pool vs path to max high.

### Summary table

| # | signal_ts | tier | confirmΔ | OB | max% | pools reached | strongest ≥0.8% | hit ≥0.8% | near&lt;0.6% passed |
|---|-----------|------|----------|----|------|---------------|-----------------|-----------|---------------------|
| 1 | 09-07 08:45 | tier2_strong | +364k | 4.50 | **+2.49** | 13/13 | +1.78% str6.14 | **Y** | 7/7 |
| 2 | 09-07 19:35 | tier1_valid | +161k | 1.16 | +1.66 | 10/11 | +1.82% str3.55 | **N** | 7/7 |
| 3 | 09-08 20:25 | tier2_strong | +1424k | 2.14 | +1.48 | 13/16 | +1.12% str10.0 | **Y** | — |
| 4 | 09-11 02:15 | tier1_valid | +165k | 3.07 | **+5.59** | 31/31 | +0.93% str5.93 | **Y** | 8/8 |
| 5 | 09-13 14:25 | tier2_strong | +557k | 1.45 | +1.16 | 12/36 | +0.81% str3.57 | **Y** | 5/5 |
| 6 | 09-14 02:15 | tier1_valid | +198k | 1.03 | +1.42 | 20/30 | +1.04% str6.92 | **Y** | 5/5 |
| 7 | 09-15 17:50 | tier2_strong | +654k | **0.98** | **+0.18** | **0/40** | +1.36% str1.78 | **N** | 0/3 |
| 8 | 09-15 23:30 | tier1_valid | +214k | **0.80** | **+0.36** | 4/45 | +2.49% str4.04 | **N** | 0/5 |
| 9 | 09-16 18:15 | tier2_strong | +623k | 1.69 | +2.20 | 13/17 | +1.29% str3.84 | **Y** | — |
| 10 | 09-17 23:20 | tier1_valid | +260k | 1.97 | **+6.43** | 13/13 | +1.09% str~0 | **Y** | 10/10 |

### Aggregates

- mean max excursion: **+2.30%**
- meaningful strongest pool (≥0.8% above entry) hit rate: **7/10**
- when near pools (&lt;0.6%) exist and the trade works: almost always **passed** (often 100%)
- absolute “strongest anywhere” hit rate 8/10 is misleading — often that pool sits at +0.1–0.5% and is trivial

### Working moves vs failed moves

**Working (max ≳ +1%):** #1–#6, #9, #10

- Near/weak pools get eaten (`passed_through` / `passed_through_after_pause`)
- Price usually reaches a meaningful stronger pool around **+0.8% to +1.8%**
- Sometimes overshoots far beyond (#4 +5.6%, #10 +6.4%) — strongest entry pool is then only a checkpoint, not the final high

**Failed / dead (#7, #8)**

| | #7 | #8 |
|--|----|----|
| confirmΔ | +654k (strong!) | +214k |
| OB | **0.98** flat | **0.80** ask-heavy |
| max | +0.18% | +0.36% |
| pools | **0 reached** of 40 | only 4 micros, then die |
| lesson | strong public delta alone ≠ carry; OB not supportive | never got to the real strong pool at +2.5% |

### Comparison takeaways

1. **Signal #1 is not unique:** when the move works, weak intermediate pools are noise and get skipped by price.
2. **Best TP magnet proxy so far:** strongest ACTIVE upper pool with **≥ ~0.8% distance**, not the nearest pool and not “strongest regardless of distance”.
3. **Hit rate for that magnet:** 7/10; the 3 misses are #2 (stopped ~+1.66% before +1.82% pool), #7 and #8 (failed trades).
4. **OB filter matters more than expected:** both failures had OB ≤ ~1.0 despite positive confirm delta. Working trades often had OB ≳ 1.1–4.5 at entry.
5. **Confirm delta alone does not predict reach:** #7 had +654k and still went nowhere; #4/#2 had “only” ~+160k but still reached meaningful pools / big excursion.
6. **Overshoot is common:** price often tags the strong pool and pushes a bit further (#1 +0.48% beyond strong top) or much further (#4/#10). Rejection is “around / above” the strong cluster, not always exactly at the lower edge.

### Hypotheses to test next (still calibration, not live rules yet)

1. TP candidate = strongest pool with `dist ≥ 0.8%` **only if** entry OB ≳ 1.05 (or similar).
2. If OB ≤ 1.0 → do **not** project to far strong pools; keep first-pool / tight exit bias.
3. Near pools `&lt; 0.6%` should not be primary TP when flow+OB are supportive.
4. Need a second target rule for “trend continuation beyond strong pool” (#4, #10) vs “stop at strong pool” (#1, #3, #5).

### Next analysis steps

1. Split working vs failed more cleanly with OB×delta grid.
2. Measure for each working trade: distance from strongest≥0.8% pool to actual max (overshoot distribution).
3. Decide whether 1m strength or 5m ladder is the better TP map (same 10 signals, side-by-side).
4. Only then draft concrete TP mode thresholds.

## Cluster-mass analysis code (implemented)

Modules:

- `ob_microstructure_breakout_bot/exit_backtest/cluster_mass.py`
- `ob_microstructure_breakout_bot/exit_backtest/run_cluster_mass_analysis.py`
- tests: `ob_microstructure_breakout_bot/exit_backtest/tests/test_cluster_mass.py`

Run:

```bash
python -m ob_microstructure_breakout_bot.exit_backtest.run_cluster_mass_analysis
```

Output:

- `results/ob_pool_cluster_mass_reachability.json`

What the code measures per signal:

- 1m ACTIVE upper pools at entry → clustered by gap (`0.10%`)
- per cluster: `n_pools`, `strength_sum`, `strength_avg`, `width_pct`, `density_per_pct`
- labels: `zwischenstation` / `gebremst` / `reversal_point` / `reaction_near_high` / `ignored`
- reversal-cluster mass at the turn
- reachability matrix: `f(mass, distance, delta, OB)`

### First run snapshot (10 longs)

- strongest-by-mass cluster hit rate: **80%**
- mean reversal cluster: **~7.9 pools**, strength_sum **~8.5**, width **~0.50%**
- near band `0–0.8%`: high hit rate (easy)
- far band `1.5–2.5%` with strong delta: many still missed when OB weak (`unrealistisch_trotz_delta_ob_schwach`)
- Signal #1 reversal cluster = strong upper mass (**3 pools, strength_sum ~14.9** at ~+1.65%) — matches chart Ziel zone

## Full-history phase plan

This is the next step after the probe run: apply the same logic over the full
historical DOGE signal set, not just the 10 locked examples.

### Phase A — build the signal universe

Goal:

- collect all valid long signals from the calibrated history window
- keep only the signal timestamps where the entry logic would have fired
- preserve the entry-side metadata:
  - entry price
  - confirm delta
  - followthrough delta
  - OB ratio at entry
  - signal tier / label

Output:

- one row per long signal
- no cluster logic yet

### Phase B — attach pool ladders

For every signal:

- load the causal 1m ACTIVE upper pools at signal time
- group nearby pools into clusters
- compute per cluster:
  - pool count
  - sum of strength
  - average strength
  - width in price / bps
  - density per percent
  - strongest pool in the cluster

This phase answers:

- which clusters exist above the entry
- how large they are
- where the entry sits relative to them

### Phase C — walk the price path

For every signal:

- walk forward from entry until max excursion / first clear pullback
- for each cluster:
  - record whether it was reached
  - record the first touch time
  - record delta and OB at touch
  - classify it as:
    - `leicht_erreichbar`
    - `reachable_moderate`
    - `reachable_with_strong_delta`
    - `praktisch_nicht_erreichbar`
    - `unrealistisch_trotz_delta_ob_schwach`

This phase answers:

- which clusters the market really walked into
- which clusters acted as reaction / reversal points
- which clusters only became reachable when delta and OB were strong enough

### Phase D — compare working vs failed signals

Split the full set into:

- working moves
- early failures / dead moves
- overshoot moves beyond the strong cluster

Then compare:

- cluster mass at the reversal point
- distance to the cluster
- delta at entry
- delta at first touch
- OB at entry and at touch

### Phase E — derive exit rules

Only after the full-history comparison should we write the actual TP rule:

- near weak clusters = noise
- far clusters = only valid if cluster mass is high enough
- far clusters plus weak OB = usually not a good TP target
- strong OB + strong delta + heavy cluster = TP target candidate

### What not to do

- do not hand-pick only the best-looking examples
- do not make distance the main TP criterion
- do not ignore cluster mass
- do not use future clusters beyond the signal time

### Expected output files

- signal-level JSON for all analyzed longs
- cross-signal summary JSON
- short markdown note with the final TP hypotheses
- later: code implementation based on the accepted rules

## Full-history run (executed)

### Correct trigger (EMA59 touch) — preferred

Runner:

```bash
python -m ob_microstructure_breakout_bot.exit_backtest.run_ema59_touch_cluster_calibration
```

This matches the DOGE OB calibration trigger:

- start at **EMA59 touch** (`touch_ts` / `bar_ts`)
- measure max excursion **above EMA59** (long / from_below) or **below EMA59** (short / from_above)
- attach causal 1m pool clusters on that path
- compare reachability vs |confirm Δ| + OB + cluster mass

Outputs:

- `results/ob_pool_ema59_touch_cluster_calibration.json`
- `results/ob_pool_ema59_touch_cluster_calibration.md`

First run (DOGE window, first-in-cluster only):

- **171** touches (89 long / 82 short)
- long mean max vs EMA59 **~1.90%** (p75 ~2.37%), strongest-cluster hit **~79%**
- short mean max vs EMA59 **~1.67%** (p75 ~2.22%), strongest-cluster hit **~65%**
- near clusters `<0.8%` usually reachable; far `1.5–2.5%` need strong |Δ| + mass/OB

### Earlier decision_ts-based run (superseded for trigger)

Runner:

```bash
python -m ob_microstructure_breakout_bot.exit_backtest.run_full_history_cluster_calibration
```

Outputs:

- `results/ob_pool_cluster_full_history.json`
- `results/ob_pool_cluster_full_history.md`

Universe in DOGE coverage window (`2026-09-05T17Z` → `2026-09-19T11Z`):

- scanner long breakouts (legacy ∪ calibrated): **15**
- Phase-A strong longs: **10**
- Phase-B long fakeouts: **46**
- total analyzed: **71**

Note: that run started at `decision_ts`, not at EMA59 touch. Prefer the EMA59-touch runner above for calibration samples.

### First full-history takeaways

- Near clusters (`<0.8%`) are usually reachable → noise / checkpoint, not primary TP
- Far clusters (`1.5–2.5%`) often stay unreachable even with confirmΔ ≥ 200k unless OB + mass support
- Failed-early moves tend to have weaker entry OB than working moves
- Working reversals sit in heavier clusters → TP = heaviest meaningful cluster
- Scanner longs: strongest-by-mass hit rate **~87%** (n=15)
- EMA59-touch sample is much larger (**171**) and is the right base for threshold search

## Phase C — TP threshold calibration (executed)

Runner:

```bash
python -m ob_microstructure_breakout_bot.exit_backtest.calibrate_tp_from_touches
```

Outputs:

- `results/ob_pool_tp_threshold_calibration_phase_c.json`
- `results/ob_pool_tp_threshold_calibration_phase_c.md`

Method (same style as DOGE OB Phase C):

- label touches working / failed from max excursion vs EMA59
- quantile proposal for mass / distance / |Δ| / OB per side
- grid + walk-forward check
- **primary suggestion = quantile rule** (more stable than sparse grid winners)

Next:

1. Sanity-check suggested long rule on the 10 locked scanner longs
2. If stable, wire into `simulate_long` / exit thresholds
3. Re-run touch dump after short-distance fix if short rules look off
