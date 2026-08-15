# Cobertura-0-Notional Recovery Report

Generated: 2026-07-26T09:41:17.870607+00:00

## Summary

- symbol: `APTUSDT`
- start: `2026-01-19T03:55:00+00:00` @ `1.6456`
- locked_spread_loss: `14.041991`
- final_state: `RECOVERED`
- exit_reason: `recovered_profit`
- recovery_rounds: `8`
- bars_processed: `5141`
- overlay_add_fills: `16`
- overlay_be_closes: `7`
- realized_overlay_pnl: `46.76639699999999`
- cumulative_entry_fees: `1.15865497121`
- cumulative_close_fees: `1.4280300106600003`
- final_total_exit_economics: `30.137720809588785`

## Fee / BE semantics

- Open/close fees booked per fill: `|price * qty| * fee_rate`.
- Slippage worsens fill prices; informational slippage cost is not subtracted again from total_exit_economics.
- Overlay BE solves for short-close trigger including round entry fees, exit fee, close slippage and fee_buffer.
- Full exit only when total_exit_economics >= target - tolerance, including estimated remaining close fees.

## Integrity

- core_unchanged_until_full_exit_or_still_frozen: `True`
- start_qty_neutral: `True`
- no_negative_qty: `True`
- tranche_ledger_qty_sync: `True`
- locked_spread_loss: `14.04199120854124`
- fill_count: `26`
- recovery_rounds: `8`
- final_state: `RECOVERED`
- exit_reason: `recovered_profit`
- overlay_exit_policy: `shared_be`
- full_exit_target_mode: `legacy`
- safety_violation_count: `0`
- short_add_count_total: `16`
- equalization_fill_count: `0`
- flat_after_full_exit: `True`
