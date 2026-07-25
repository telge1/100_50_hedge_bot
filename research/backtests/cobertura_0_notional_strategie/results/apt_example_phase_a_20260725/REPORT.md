# Cobertura-0-Notional Recovery Report

Generated: 2026-07-25T13:45:17.330725+00:00

## Summary

- symbol: `APTUSDT`
- start: `2026-01-19T03:55:00+00:00` @ `1.6456`
- locked_spread_loss: `28.309310`
- final_state: `RECOVERED`
- exit_reason: `recovered_profit`
- recovery_rounds: `8`
- bars_processed: `5141`
- overlay_add_fills: `16`
- overlay_be_closes: `7`
- realized_overlay_pnl: `62.35506449999998`
- cumulative_entry_fees: `1.5448700369850004`
- cumulative_close_fees: `1.9040364966700005`
- final_total_exit_economics: `30.596847805021635`

## Fee / BE semantics

- Open/close fees booked per fill: `|price * qty| * fee_rate`.
- Slippage worsens fill prices; informational slippage cost is not subtracted again from total_exit_economics.
- Overlay BE solves for short-close trigger including round entry fees, exit fee, close slippage and fee_buffer.
- Full exit only when total_exit_economics >= target - tolerance, including estimated remaining close fees.

## Integrity

- core_unchanged_until_full_exit_or_still_frozen: `True`
- start_qty_neutral: `True`
- no_negative_qty: `True`
- locked_spread_loss: `28.309310161323403`
- fill_count: `26`
- recovery_rounds: `8`
- final_state: `RECOVERED`
- exit_reason: `recovered_profit`
