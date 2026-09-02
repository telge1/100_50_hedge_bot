"""Liquidation semantics proof from collector code and schema."""

from __future__ import annotations

from typing import Any

FORCED_TRADE_DIRECTION = {
    "LIQUIDATED_SHORT": "FORCED_BUY",
    "LIQUIDATED_LONG": "FORCED_SELL",
}

EXPECTED_AGGRESSOR = {
    "LIQUIDATED_SHORT": "Buy",
    "LIQUIDATED_LONG": "Sell",
}


def build_liquidation_semantics_audit(cl) -> dict[str, Any]:
    """Document side semantics from schema, collector, and tests."""
    schema_rows = cl.query("DESCRIBE TABLE orderbook_analysis.all_liquidations").result_rows
    schema = {r[0]: r[1] for r in schema_rows}

    # Verify event_key uniqueness in core window
    from .config import CORE_END, CORE_START, SYMBOL
    from research.btc_ob_fight.loaders import _dt_sql

    dup = cl.query(
        f"""
        SELECT count(), uniqExact(event_key)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{SYMBOL}'
          AND event_time >= toDateTime64('{_dt_sql(CORE_START)}', 3, 'UTC')
          AND event_time < toDateTime64('{_dt_sql(CORE_END)}', 3, 'UTC')
        """
    ).result_rows[0]

    return {
        "contract_version": "liquidation_semantics_audit_v1",
        "table": "orderbook_analysis.all_liquidations",
        "schema_columns": schema,
        "side_field": "liquidated_position_side",
        "side_semantics": {
            "meaning": "Liquidated POSITION side, NOT taker aggressor field",
            "LIQUIDATED_SHORT": "Short position forcibly closed → economically a FORCED BUY against Ask",
            "LIQUIDATED_LONG": "Long position forcibly closed → economically a FORCED SELL against Bid",
            "collector_evidence": "orderbook_analyse/oi_liquidation_collector/logic.py interpret_liquidated_position_side",
            "bybit_raw_S_field": "S=Buy → LIQUIDATED_LONG; S=Sell → LIQUIDATED_SHORT",
            "forced_trade_direction_map": FORCED_TRADE_DIRECTION,
            "expected_aggressive_public_trade_side": EXPECTED_AGGRESSOR,
        },
        "bankruptcy_price_semantics": {
            "field": "bankruptcy_price",
            "meaning": "Reference bankruptcy/liquidation price from exchange feed, NOT proven exact execution print",
            "use_for_association": "HEURISTIC_TEMPORAL_PRICE_PROXIMITY_ONLY",
        },
        "row_granularity": {
            "each_row_represents": "one liquidation event message from Bybit allLiquidation stream",
            "not_guaranteed": "one unique closed position (size may be partial fill aggregate)",
            "dedup_key": "event_key = exchange|symbol|event_ms|position_side_raw|size|bankruptcy_price",
            "core_window_row_count": int(dup[0]),
            "core_window_unique_event_keys": int(dup[1]),
            "dedup_status": "UNIQUE" if int(dup[0]) == int(dup[1]) else "DUPLICATE_KEYS_PRESENT",
        },
        "public_trade_linkage": {
            "shared_id_exists": False,
            "association_method_allowed": "HEURISTIC_TEMPORAL_PRICE_ASSOCIATION only",
            "direct_identification_allowed": False,
        },
        "position_count_claim": {
            "allowed_wording": "N liquidation events (deduplicated by event_key)",
            "disallowed_wording": "N positions unless position-level ID proven",
        },
        "test_evidence": [
            "orderbook_analyse/tests/test_ob200_v3_raw_discovery_v3.py interpret_liquidated_position_side",
            "orderbook_analyse/oi_liq_impact_l2/contracts.py LIQUIDATION_SIDE_BY_DIRECTION + AGGRESSOR_SIDE_BY_DIRECTION",
        ],
    }
