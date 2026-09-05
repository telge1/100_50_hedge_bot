"""Read-only signal-scoped context join templates (Phase 8)."""

from __future__ import annotations


def trades_for_signal_sql(database: str) -> str:
    return f"""
SELECT
  s.signal_id,
  s.profile_contract,
  s.continuity_epoch_id,
  s.overlap_cluster_id,
  s.research_eligible,
  t.*
FROM {database}.v_full_ob_signals_canonical AS s
INNER JOIN orderbook_analysis.public_trades_canonical AS t
  ON t.symbol = s.symbol
WHERE s.signal_id = {{signal_id:String}}
  AND t.trade_ts >= {{pre_ts:DateTime64(3)}}
  AND t.trade_ts <= {{post_ts:DateTime64(3)}}
SETTINGS join_use_nulls = 1
""".strip()


def context_coverage_note(*, oi_present: bool, liq_present: bool) -> str:
    if oi_present and liq_present:
        return "FULL"
    if oi_present or liq_present:
        return "PARTIAL"
    return "PARTIAL"
