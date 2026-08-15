# OI Compression Breakout — Feature Semantics

## Join
- `market_candles` 5m ⋈ `research_open_interest_5m` ⋈ liquidations ⋈ orderflow
- Keys: `symbol`, `open_time = bucket_start`, `import_version = derivatives_5m_v1`
- Only `data_available = true`; no forward-fill across gaps

## Box
- Lengths: B16 / B32 / B64 (bars before confirm)
- At confirm candle `t`: `box_high/low = max/min(high/low[t-N : t])` — **confirm excluded**
- Quality: Q1 `width/ATR14 ≤ 2.0`, Q2 `≤ 1.5`
- Drift: `|close_end - close_start| / width ≤ 0.35`
- Inner closes: ≥75% of box closes in inner 60% zone
- Freeze: high/low fixed at confirm; never expanded
- One active box per `(symbol, box_length)`; early release on breakout or timeout
- Lengths never block each other
- No box across `sequence_id` change or non-300s gaps
- Funnel counters: `box_filter_diagnostics.csv`

## OI groups (parent / subset)
- **O0** parent: all valid boxes
- **O1** `oi_change_pct > 0`
- **O2** `oi_change_pct ≥` causal 75th pct of **prior** same-coin boxes (else `insufficient_warmup`)
- **O3** `positive_oi_step_ratio ≥ 0.65` AND `oi_change_pct > 0`
- **O4** (O2 or O3) AND `box_drift_ratio ≤ 0.20`
- O1–O4 are subsets of O0 — not independent market events

## Breakout
- Long: `close > frozen_box_high`; Short: `close < frozen_box_low`
- Wick-only insufficient; confirm at close
- Fill: **next** 5m open after breakout close (never same candle)
- Wait windows: W3/W6/W12/W24/W48; max wait 48 bars
- Timeout → `no_breakout=true` (not invalidated)
- Gap/sequence → `invalidated=true`

## Populations
- `confirmed_boxes.csv`: all confirms (`box_id`)
- `breakout_events.csv`: one row per box including `no_breakout=true`
- `box_oi_features.csv`: OI membership / `candidate_id`
- `candidate_breakout_outcomes.csv`: one row per `candidate_id`
- `candidate_forward_outcomes.csv`: trading path only if fill exists
- `forward_outcomes.csv`: box-level forwards (compat)

## Outcomes
- Horizons 1…96: MFE/MAE/close return (+ ATR and box-width normalized)
- Fakeout: F1 ≤3, F2 ≤6, F3 opposite ≤12
- First touch % / ATR / box; same-bar → adverse-first conservative
- Exits via `evaluate_outcome_params` → `net_pnl_pct` / `exit_reason`

## IDs
- `physical_id = symbol|sequence_id|start_bucket|confirm_bucket`
- `box_id = physical_id|B{length}x{quality}`
- `candidate_id = physical_id|B{length}|quality|oi_group`
