# TRADE_NO_BE50 horizon: productive/diagnostic No-BE50 TP/SL + SL_FIRST outcomes.
# Stored alongside TRADE (Frozen BE50) without overwriting historical BE50 rows.
# Logical uniqueness remains (signal_id, horizon).
# Idempotent: no schema alteration required.
SELECT 1;
