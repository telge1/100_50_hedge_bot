# recommended_backfill_plan.md

## Verdict context

Use **Signal_Generator** pipeline into `orderbook_analysis.public_trades_canonical`.
Do **not** use OA `ingest_public_trades_archive.py` for Full-OB analysis fills (wrong table).

## Safe pilot (after explicit approval)

1. BTCUSDT + DOGEUSDT only  
2. Window: 180 UTC days ending at current canonical min day (exclusive of already-AUDITED days)  
   Example: `--start-date 2026-01-20 --end-date-exclusive 2026-07-19`  
3. `workers 1`, existing pause/batch defaults  
4. Separate `--run-dir` under `results/public_trades_backfill_6m_pilot/`  
5. Gate A style smoke: one BTC day HEAD/download/import/idempotent rerun  
6. Keep live collector PID 1661773 running; do not restart OB/OI collectors  

## 12-month BTC/DOGE

Same CLI with earlier start (archive exists for BTC back to ≥2021).  
Expect longer runtime and more disk; still within 430 GiB gate if limited to 2 symbols.

## 51-coin 6–12 months

**Do not run as one shot** without revisiting `MAX_SAFE_USE_BYTES` / staged monthly imports.  
Linear scale from current density implies hundreds of GiB CH for 6m and ~0.8 TiB-class for 12m.  
Prefer monthly slices + coverage CSV after each month; treat 404 as `LISTING_LIMITED`/`ARCHIVE_UNAVAILABLE`.

## Full-OB join

Always join `orderbook_analysis.public_trades_canonical` on `symbol` + `trade_ts` within each `signal_id` window.  
Never aggregate across overlapping signals.  
Prefer `uniqExact(trade_id)` / `GROUP BY trade_id` for exact counts.

## Not executed

All commands in `proposed_commands_not_executed.sh` are proposals only.
