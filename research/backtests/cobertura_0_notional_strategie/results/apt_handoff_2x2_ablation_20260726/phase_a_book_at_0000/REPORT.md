# Cobertura-0-Notional Recovery Report

Generated: 2026-07-26T09:41:18.963569+00:00

## Summary

- symbol: `APTUSDT`
- start: `2026-01-19T00:00:00+00:00` @ `1.7223`
- locked_spread_loss: `28.309310`
- final_state: `DATA_END_OPEN`
- exit_reason: `data_end_open`
- recovery_rounds: `16`
- bars_processed: `45945`
- overlay_add_fills: `26`
- overlay_be_closes: `16`
- realized_overlay_pnl: `5.15278859999989`
- cumulative_entry_fees: `2.4586198876800003`
- cumulative_close_fees: `2.45578585395`
- final_total_exit_economics: `-28.330206943903477`

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
- locked_spread_loss: `28.309310161323403`
- fill_count: `42`
- recovery_rounds: `16`
- final_state: `DATA_END_OPEN`
- exit_reason: `data_end_open`
- overlay_exit_policy: `shared_be`
- full_exit_target_mode: `legacy`
- safety_violation_count: `0`
- short_add_count_total: `26`
- equalization_fill_count: `0`
- flat_after_full_exit: `False`
