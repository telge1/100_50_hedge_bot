# Exit Rules V2

This file tracks the planned exit logic for the EMA Touch V2 work.

## Goal

Define a no-lookahead exit plan that uses only:

- order book context
- public trade flow
- EMA structure

The goal is to keep winners alive while locking in risk early enough to avoid giving back the whole move.

## Core Principle

Never use information from candles or flow that are not fully available yet.

Exit decisions must be based on the latest closed bar plus the already known OB / public-trade state at that moment.

## Initial Stop Loss

The protective stop belongs below the most recent validated lower low, not on a fixed time.

For the current DOGE example, the first hard stop is around `0.08293`.
That stop should stay in place until the trade either:

- confirms a strong continuation and the stop is moved up, or
- invalidates the setup and gets closed by price

### Stop-setting rule

1. Find the last structural lower-low area before the entry.
2. Use the **actual candle low** of that lower-low leg as the anchor.
3. Place the initial SL 0.15% below that low as a buffer.
4. Do not move the SL based on an unconfirmed intrabar dip.
5. Only move the SL up after a new confirmed higher low or after a partial exit that reduces risk.

The confirmed pivot low is still useful as structure context, but the SL anchor is the actual price low, not the later pivot confirmation timestamp.

For the `2026-09-07T08:45:00Z | tier2_strong` case:

- last lower-low candle low: `0.08909`
- buffered SL target: `0.08909 * 0.9985 = 0.08896` approx

## Exit Ladder

### 1. First partial exit

Take the first partial exit when the move looks stretched and a local expansion leg is maturing.

Long example:

- price has already pushed well away from EMA9 / EMA20
- the first strong local high is printed
- OB support starts to flatten or fade
- public trade aggression no longer expands in the trade direction

Suggested action:

- close 50% of the position
- keep the rest open

### 2. Break-even protection

After the first partial exit:

- move the stop on the remainder to break-even, or
- to a very small buffer if fees/slippage require it

This is the default protection step once the first impulse has paid.

### 3. Final exit

Close the remainder when structure really breaks.

For longs, the hard exit conditions are:

- EMA20 crosses below EMA59
- EMA59 crosses below EMA200
- or strong adverse public-trade follow-through appears together with ask-heavy OB

For shorts, mirror the same logic in the opposite direction.

## Evaluation Order

1. Check whether the trade is still structurally valid.
2. If valid but stretched, allow a first partial exit.
3. After partial exit, move stop to BE.
4. If EMA structure breaks, exit the rest.

## What Counts as a Stretch

A move is considered stretched when:

- the price has already produced a visible impulse from the entry area
- the current leg is extended relative to the short EMAs
- follow-through in public trades stops improving
- OB no longer shows strong support in the trade direction
- the move is a confirmed expansion leg, not just an early post-entry bounce

This is intentionally a qualitative rule first.
We can turn it into exact numeric thresholds later.

## Lesson From the First Signal

The first DOGE long taught us a useful constraint:

- the first opposing print is **not** a full exit signal by itself
- the first local top is the right place for a **partial exit**
- after that, the remainder should move to **break-even**
- only a real structure break should close the rest
- a partial exit that would still leave the trade at BE or worse is too early

So the rule is:

- do not close the full position on the first short/public counter-signal
- keep the trade open while EMA20 / EMA59 structure still supports the long
- use the first stretched local high for the partial exit
- use the later structural break for the final exit
- if the first local high is too close to entry, wait for the next expansion leg

## Example From the Current DOGE Case

For the `2026-09-07 19:35` long:

- first partial exit: around the first local high near `2026-09-08T01:00:00Z`
- move remainder to BE: around `2026-09-08T02:30:00Z`
- final exit: only when the later structure breaks clearly

## Higher Timeframe Liquidity Map

Use the 1h liquidity map as a higher-timeframe exit filter.

After the trade is opened, check the liquidity pools **above price** first:

- first upper pool
- second upper pool
- only then larger / more distant pools

If the first major peak already sits under a large liquidity void and the next
heavy cluster above is far away, while thick liquidity blocks build up below,
then the first peak is a valid **100% exit candidate**.

In that situation, the market has:

- limited upside room into the next cluster
- clear downside liquidity targets below price
- a high chance of mean reversion after the first exhaustion peak

This is a stronger exit signal than the EMA stack alone and can justify a full
de-risk at the first peak.

### Two-pool continuation rule

If price has already broken the first pool and the public-trade delta is still
strongly positive, do **not** wait for the move to run into the middle of the
next cluster.

Instead:

- treat the first pool as a checkpoint only
- if you de-risk there, keep it small
- set TP immediately at the **lower edge of the second upper pool**
- treat the second pool as the target zone
- use the first pullback into that zone as the exit, not a later extension
- if the first pool is hit with strong positive delta and high volatility,
  lock the TP at the second pool immediately
- do **not** keep ratcheting the TP higher just because price temporarily keeps
  moving; once the second pool is chosen, keep it fixed unless a new breakout
  and fresh OB/public-trade expansion justify a new plan
- if delta is **not** strongly positive at the pool touch, keep a small safety
  buffer under the pool edge instead of assuming a perfect touch fill

This avoids giving back the run after the first strong breakout leg has already
done its job.

### Sparse ladder filter

Sometimes the first upper pool is very close to the entry, so the available
profit into that pool is too small to matter after fees and noise. If the next
upper pool is much farther away, the trade only makes sense when the entry is
backed by clearly positive and expanding delta.

Use this as a filter:

- if the first pool is closer than about `0.8%` from the entry, treat it as a
  weak reward setup
- if the first pool is closer than `0.8%` and delta is also very low, ignore
  the trade
- if the next pool is far away, require strong delta and clean continuation
  structure before entering
- if delta is not strong enough, skip the trade instead of forcing a setup that
  only pays a few tenths of a percent into the first pool

In the DOGE example, a first pool around `0.08392` with only about `0.3%`
room is not enough on its own if the next pool sits much higher, around
`0.08512`. In that case, the trade should only be taken when public-trade delta
confirms that the move can realistically reach beyond the first pool.

## Open Work

- define exact stretch thresholds
- define exact OB/public-trade decay rules
- separate long and short thresholds if needed
- convert this note into a reusable testable rule
