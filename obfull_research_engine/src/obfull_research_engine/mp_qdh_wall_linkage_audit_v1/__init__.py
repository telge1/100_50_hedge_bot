"""Wall-linkage + public-trade attribution audit for the six-event QDH pilot.

Read-only. Reuses:
  - mp_qdh_canonical_integration_v1 (wall_id / band / pilot cases)
  - ob_forschungsengine_v1.qdh_engine.run_qdh_base
  - level_first_episode1_wall_flow_qdh_base_v1 (attribute_intervals, mass_balance, update_qdh)

Does NOT reimplement QDH or widen the ±5-tick search zone.
"""

from __future__ import annotations

PACKAGE_NAME = "mp_qdh_wall_linkage_audit_v1"
AUDIT_ID = "MP_QDH_WALL_LINKAGE_AUDIT_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "mp_qdh_wall_linkage_audit_v1"

TICK_SIZE = 0.1
BAND_TICKS = 5
PRE_TOUCH_S = 120.0
FORENSIC_TAIL_S = 300.0
WARMUP_S = 300.0  # LC seed warmup (same as integration)

SILVER_DATABASE = "research_full_ob_silver_v1_3"
SYMBOL = "BTCUSDT"
ALLOW_CLICKHOUSE_WRITES = False

PILOT_RUN_REL = "obfull_research_engine/runs/mp_qdh_canonical_pilot_v1_20260917"
BATCH_RUN_REL = "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"

# Phase-0 contract notes (file:function references — not guesses)
PHASE0_CONTRACT = {
    "wall_selection": {
        "module": "mp_qdh_canonical_integration_v1.wall_select.select_defense_wall",
        "rule": (
            "Replay LC up to zone_touch_ns on defense_side only; "
            "candidates = positive sizes in [zone_lo−band_ticks*tick, zone_hi+band_ticks*tick]; "
            "prefer wall_price_from_mp_event if visible; else size DESC, edge_dist ASC, price ASC; "
            "ties → WALL_SELECTION_UNRESOLVED."
        ),
        "wall_id": "make_wall_id = sha256(side|price|zone_id)[:16] prefixed w_",
        "blocked_reason_pilot": (
            "NO_VISIBLE_DEFENSE_LEVEL_AT_ZONE_TOUCH: snapshot at touch had no positive "
            "defense-side size inside zone±5 ticks (see dry_run / wall_selection_audit)."
        ),
    },
    "attribution": {
        "module": "level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution.attribute_intervals",
        "public_trades": "orderbook_analysis.public_trades_canonical",
        "trade_time": "CanonicalTrade.exchange_event_time from trade_ts; available_at = collector_received_at or bucket proxy",
        "book_time": "LevelChange event_time (exchange); available_at = collector_received_at or event_available_at proxy",
        "match_rule": (
            "Interval (t_i, t_{i+1}] on book exchange times; attack trades with price in view "
            "and matching aggressor (Buy vs ask / Sell vs bid); each trade_id consumed once."
        ),
        "fill_cap": (
            "Engine does NOT hard-cap attributed_hit_qty to book_decrease; "
            "decompose_mass_balance treats excess fill as net_refill identity. "
            "Audit labels FILL_EXCEEDS_DECREASE as diagnostic only."
        ),
        "units": "Queue/fill/pull/refill in base coin qty (BTC for BTCUSDT); notional in USDT.",
    },
    "mass_balance": {
        "module": "level_first_episode1_wall_flow_qdh_base_v1.mass_balance.decompose_mass_balance",
        "ResidualPull": "max(-(delta_queue + attributed_hit), 0)",
        "Refill": "max(delta_queue + attributed_hit, 0)",
        "NetDepletion": "attributed_hit + residual_pull - net_refill",
    },
    "qdh": {
        "module": "level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard.update_qdh",
        "bucket_ms": 100,
        "half_life_ms": 500,
    },
    "side_rule": {
        "module": "mp_ob_feature_enrichment_v1.zones.build_zone_bands",
        "UPPER": "defense_side=ask (attack Buy)",
        "LOWER": "defense_side=bid (attack Sell)",
        "true_break_note": "label_price_only / trade_side must NOT flip the searched book side",
    },
}

LINKAGE_STATUS = (
    "PRESENT_AT_TOUCH",
    "DEPLETED_BEFORE_TOUCH",
    "PULLED_BEFORE_TOUCH",
    "MOVED_BEFORE_TOUCH",
    "NO_CANONICAL_WALL",
    "AMBIGUOUS_MULTIPLE_WALLS",
    "DATA_INCOMPLETE",
)

REJECTION_REASONS = (
    "OUTSIDE_EVENT_WINDOW",
    "OUTSIDE_DEFENDED_BAND",
    "WRONG_AGGRESSOR_SIDE",
    "NO_MATCHING_BOOK_DECREASE",
    "EXCEEDS_BOOK_DECREASE_CAP",
    "MISSING_OR_INVALID_TRADE_ID",
    "DUPLICATE_TRADE_ID",
    "TIMESTAMP_ALIGNMENT_FAILURE",
    "DATA_GAP",
    "OTHER",
)
