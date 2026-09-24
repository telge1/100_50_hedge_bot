# 5m Pool Bounce Rule

This note freezes the first structure-only rule for the 5m liquidity-pool bounce study.

## Core idea

Only trade bounces from the **strongest upper pools (Rank 1 and Rank 2)** by
`strength_sum`, and only when the next meaningful lower pool leaves enough TP room.

## Rule

- Use **5m liquidity pools** only.
- Rank upper clusters by mass; **trade Rank 1 and Rank 2 only** (skip Rank 3+).
- Find the **touch candle**: the first candle that enters the pool.
- Use the **top edge of the touched pool** as the reference.
- Look at the **next meaningful pool below** that touch pool.
- Trade only when Entry→TP room is at least **0.8%**
  (TP = nearest ACTIVE lower pool top at short-entry time).

## Interpretation

- **>= 0.8% TP room under the pool**: enough distance to the TP, candidate.
- **< 0.8% TP room**: skip (example bug: top→top gap looked ok, but Entry→TP was only ~0.5%).
- Bigger lower-pool separation generally means a stronger possible reaction.

## SL / TP (short bounce from upper pool)

- Side: **short** after confirmed reversal from the upper pool.
- **SL** = touched pool **top + 0.2%**.
- **TP** = top edge of the **nearest ACTIVE lower pool at short-entry time**
  with Entry→TP room ≥ **0.8%** (skip nearer pools that are too close).
- Lower pools are loaded **as-of the short entry candle**, never from the day-before search start.
- Entry only after a **confirmed reversal candle** (5m close below the touched pool bottom).
- Same-bar SL+TP: count **SL first** (conservative).

## Next step

After this structure-only rule, add OB / delta confirmation as a second layer.

## OB + public-trade watch rule

- Start monitoring **OB + public trades** when price is about **0.8% before** the lower edge of the relevant pool.
- At the pool touch, check whether **bid strength / buy delta** supports a bounce.
- Open the trade only after a **confirmed reversal candle**; if price pierces the pool without confirmation, skip the entry.

## Full-history backtest definition

- Run the rule on the **full history** where OB and public-trade data are available.
- For each signal, use the **first touch candle** of the pool as the event anchor.
- Mark the outcome as **bounce** if price confirms away from the pool after touch.
- Mark the outcome as **pierce** if price runs through the pool without a confirmed reversal.
- Compare / trade **rank-1 / rank-2** only; smaller clusters are research-only, not traded.

## Bounce metric

- Take the **touch candle** that first enters the pool.
- Measure the price move **away from the pool after that touch**.
- For an upper pool, bounce is the percentage move from the post-touch high down to the next low.
- For a lower pool, bounce is the percentage move from the post-touch low up to the next high.
- Use the **pool top / bottom** only as the zone reference, not as an exact trigger price.

## Backtest columns

- `decision_ts`
- `touch_ts`
- `rank`
- `cluster_id`
- `pool_bottom`
- `pool_top`
- `next_lower_pool_gap_pct`
- `bounce_pct`
- `pierce_pct`
- `outcome` (`bounce` | `pierce` | `weak_reaction` | `not_reached`)
- later: `ob_ratio_at_touch`
- later: `delta_at_touch`

## Outcome definitions

- `bounce`: price touches the pool and then moves away by a meaningful amount.
- `pierce`: price moves through the pool without a valid reversal.
- `weak_reaction`: touch happens, but the move away is too small to count as a real bounce.
- `not_reached`: the pool was never touched.

## Backtest runner

```bash
python -m ob_microstructure_breakout_bot.exit_backtest.run_pool_bounce_backtest --universe locked10
python -m ob_microstructure_breakout_bot.exit_backtest.run_pool_bounce_backtest --universe scanner
python -m ob_microstructure_breakout_bot.exit_backtest.run_pool_bounce_backtest --universe full
```

`--universe full` = scanner ∪ strong ∪ fakeout inside the DOGE OB+trades window (~2026-09-05 … 09-19).

Outputs:

- `results/ob_pool_5m_bounce_backtest_locked10.json` / `.md`
- `results/ob_pool_5m_bounce_backtest_scanner.json` / `.md`

Code:

- `exit_backtest/pool_bounce_backtest.py`
- `exit_backtest/run_pool_bounce_backtest.py`

## OB + delta confirmation (from first backtest)

At the **touch candle**, require both:

- `ob_ratio_at_touch >= 1.05` (bid/ask in 5bps band)
- `delta_at_touch >= +100_000` (10m public-trade window ending at touch)

Then open only on a confirmed reversal away from the pool.

### Why this gate

On the first DOGE runs (structure already filtered):

| filter at touch | locked10 | scanner | full (n=71 signals) |
|---|---|---|---|
| no flow filter | ~67% | ~60% | **73%** (163 reached) |
| OB ≥ 1.05 + Δ ≥ 100k | **~92%** | **~89%** | **92%** (78 flow-confirmed) |
| OB ≥ 1.2 + Δ ≥ 100k | ~100% (n small) | ~100% (n small) | — |

Full-window OB coverage for DOGE is ~15 days (2026-09-05…09-19); outside that, no OB files yet.

Bounce touches also tend to have **higher median OB** and **stronger Δ** than pierce touches.

### Working constants

```text
FLOW_OB_OK = 1.05
FLOW_DELTA_OK = 100_000
FLOW_OB_STRONG = 1.20   # optional stricter gate
FLOW_DELTA_STRONG = 200_000
```

### Caveat

Sample is still small (tens of reached touches). Treat this as the **first working confirmation layer**, not a final live threshold.

## SL/TP trade layer (v1)

With gap raised to **0.8%**:

```text
MIN_LOWER_GAP_PCT = 0.8   # measured pool_bottom → next lower pool top (TP room)
SL_ABOVE_POOL_TOP_PCT = 0.2
TP = next_lower_pool_top
ENTRY = close of first 5m bar after touch that closes below pool_bottom
# also skip if (entry - TP) / entry < 0.8%
```

Reports with PnL:

- `results/ob_pool_5m_bounce_backtest_locked10.md`
- `results/ob_pool_5m_bounce_backtest_full.md`

## Frozen baseline

This is the version to keep fixed for now:

- Trade **Rank 1 and Rank 2 only**
- Require **OB ≥ 1.05** and **Δ ≥ 100k** at touch
- Enter only after the **first confirmed reversal candle**
- Load lower pools **at the short entry candle**, not earlier
- Use the **nearest ACTIVE lower pool** at entry with
  **Entry→TP room ≥ 0.8%**
- Use **SL = touched pool top + 0.2%**
- Skip Rank 3+ until a new explicit test says otherwise

Current baseline report:

- `results/ob_pool_5m_bounce_baseline_rank12.md`

