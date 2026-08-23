"""Documented mapping of requested features → physical columns (no invented cols)."""

from __future__ import annotations

SOURCE_SCHEMA_AUDIT: dict = {
    "orderbook": {
        "table": "orderbook_analysis.orderbook_features_1s_v2",
        "filters": {"parser_version": "ob200_v3", "depth": 200},
        "available_columns": {
            "imbalance_l10": "imbalance_l10",
            "imbalance_l25": "imbalance_l25",
            "imbalance_l50": "imbalance_l50",
            "spread_bps": "spread_bps",
            "bid_qty_l50": "bid_qty_l50",
            "ask_qty_l50": "ask_qty_l50",
            "bid_notional_l50": "bid_notional_l50",
            "ask_notional_l50": "ask_notional_l50",
            "bucket_start": "bucket_start",
            "is_valid": "is_valid",
            "last_source_ts": "last_source_ts",
        },
        "not_available_columns": {
            "imbalance_l20": "Schema has l5/l10/l25/l50 only; no imbalance_l20",
            "impact_proxy_buy": "No impact_proxy_* column in orderbook_features_1s_v2",
            "impact_proxy_sell": "No impact_proxy_* column in orderbook_features_1s_v2",
            "directional_impact_proxy": "Derived from missing impact proxies",
        },
        "feature_column_map": {
            "ob_imbalance_l10_last": "imbalance_l10",
            "ob_imbalance_l20_last": None,
            "ob_imbalance_l50_last": "imbalance_l50",
            "ob_imbalance_l50_mean_1m": "imbalance_l50 (mean over 1m window)",
            "ob_imbalance_l50_mean_5m": "imbalance_l50 (mean over 5m window)",
            "ob_imbalance_l50_std_5m": "imbalance_l50 (std over 5m window)",
            "ob_imbalance_directional": "signed imbalance_l50_last mirrored by direction",
            "spread_bps_last": "spread_bps",
            "spread_bps_mean_1m": "spread_bps",
            "spread_bps_mean_5m": "spread_bps",
            "impact_proxy_buy": None,
            "impact_proxy_sell": None,
            "directional_impact_proxy": None,
            "ob_sample_count_1m": "count(valid buckets) in 1m",
            "ob_sample_count_5m": "count(valid buckets) in 5m",
            "ob_freshness_seconds": "decision_at - last_source_ts",
        },
    },
    "trades": {
        "table": "orderbook_analysis.public_trades_canonical",
        "available_columns": {
            "trade_ts": "trade_ts",
            "side": "side",
            "size": "size",
            "price": "price",
        },
        "aggregation_note": "1m rolls use buy_notional/sell_notional/trade_count; enrichment may use 1s rows or pre-agg minutes.",
    },
    "candles": {
        "table": "orderbook_analysis.candles_1m (aggregated to 5m)",
        "note": "Features use completed 5m bars with close_time <= decision_at",
    },
    "lld": {
        "status": "CAUSALITY_UNPROVEN",
        "reason": "No module proves pool formation timestamp <= decision_at without repaint risk.",
    },
}
