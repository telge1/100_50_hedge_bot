"""Rule-based wall toxicity classification and scores."""

from __future__ import annotations

from orderbook_analyse.wall_toxicity_audit.types import (
    MarketInteraction,
    MigrationMetrics,
    PullMetrics,
    ScoreComponents,
    SpoofingSuspicion,
    WallToxicityClass,
    WallToxicityParams,
    WallToxicityResult,
)


def classify_wall(
    *,
    pull: PullMetrics,
    migration: MigrationMetrics,
    market: MarketInteraction,
    params: WallToxicityParams,
    incomplete_ratio: float,
    sample_event_count: int,
) -> WallToxicityResult:
    notes: list[str] = []
    if sample_event_count < 2 or incomplete_ratio > 0.85:
        scores = ScoreComponents()
        return WallToxicityResult(
            classification=WallToxicityClass.INSUFFICIENT_DATA,
            reliability_score=max(0.0, 20.0 * (1.0 - incomplete_ratio)),
            toxicity_score=0.0,
            spoofing_suspicion=SpoofingSuspicion.LOW,
            score_components=scores,
            pull=pull,
            migration=migration,
            market=market,
            notes="insufficient level history or mostly incomplete initial states",
        )

    executed_ratio = 0.0
    if pull.gross_removed_qty > 0:
        executed_ratio = 1.0 - float(pull.removed_without_trade_ratio or 0.0)

    if migration.migration_event_count > 0 and pull.gross_removed_qty > 0:
        migration.migration_ratio = migration.migrated_qty / pull.gross_removed_qty

    # Score components (0–100 scale pieces; combined later)
    sc = ScoreComponents()
    sc.executed_ratio_score = executed_ratio * 100.0
    sc.cancellation_before_touch_score = min(
        100.0, float(pull.removed_without_trade_ratio or 0.0) * 100.0
    )
    if market.remained_remote or (
        market.min_distance_bps is not None
        and market.min_distance_bps >= params.remote_min_bps
    ):
        sc.cancellation_before_touch_score = min(
            100.0, sc.cancellation_before_touch_score + 15.0
        )
    sc.order_chasing_score = min(100.0, migration.migration_event_count * 20.0)
    if migration.moved_toward_market_qty > migration.moved_away_from_market_qty:
        sc.order_chasing_score = min(100.0, sc.order_chasing_score + 15.0)
    sc.layering_score = min(100.0, migration.oscillating_liquidity_count * 25.0)
    sc.remote_migration_score = 0.0
    if (
        migration.migration_event_count >= 2
        and (migration.migration_ratio or 0.0) >= 0.3
        and (market.min_distance_bps or 0.0) >= params.remote_min_bps
    ):
        sc.remote_migration_score = min(
            100.0,
            40.0
            + migration.migration_event_count * 10.0
            + (migration.migration_ratio or 0.0) * 40.0,
        )
    sc.absorption_score = 0.0
    if market.trades_in_bucket and executed_ratio >= params.executed_coverage_min:
        sc.absorption_score = min(100.0, executed_ratio * 80.0 + 20.0)
    sc.refill_score = 0.0
    if pull.gross_added_qty > 0 and pull.gross_removed_qty > 0:
        sc.refill_score = min(
            100.0, (pull.gross_added_qty / max(pull.gross_removed_qty, 1e-9)) * 50.0
        )
    sc.persistence_score = 50.0
    if pull.large_pull_count == 0 and migration.migration_event_count == 0:
        sc.persistence_score = 80.0
    elif pull.large_pull_count >= 3:
        sc.persistence_score = 20.0

    # Reliability: higher when executed/persistent/near-market genuine; lower when remote pulls
    reliability = (
        0.25 * sc.persistence_score
        + 0.25 * sc.executed_ratio_score
        + 0.15 * sc.absorption_score
        + 0.10 * sc.refill_score
        - 0.20 * sc.cancellation_before_touch_score
        - 0.15 * sc.order_chasing_score
        - 0.15 * sc.layering_score
        - 0.20 * sc.remote_migration_score
    )
    reliability = max(0.0, min(100.0, reliability))

    toxicity = (
        0.30 * sc.cancellation_before_touch_score
        + 0.25 * sc.order_chasing_score
        + 0.15 * sc.layering_score
        + 0.30 * sc.remote_migration_score
        - 0.20 * sc.executed_ratio_score
        - 0.15 * sc.absorption_score
    )
    toxicity = max(0.0, min(100.0, toxicity))

    # Classification (ordered rules)
    classification = WallToxicityClass.STABLE_PERSISTENT_WALL
    remote = market.remained_remote or (
        market.min_distance_bps is not None
        and market.min_distance_bps >= params.remote_min_bps
    )
    strong_pull = (
        pull.large_pull_count >= 1
        and (pull.removed_without_trade_ratio or 0.0) >= 0.7
    )
    strong_migration = (
        migration.migration_event_count >= 2
        and (migration.migration_ratio or 0.0) >= 0.25
    )

    if (
        market.trades_in_bucket
        and pull.trade_qty_in_bucket >= params.absorption_trade_min_qty
        and executed_ratio >= params.executed_coverage_min
        and not remote
    ):
        if pull.gross_added_qty > pull.trade_qty_in_bucket * 0.3:
            classification = WallToxicityClass.ABSORPTION_CANDIDATE
            notes.append("trades cover removals near market with refill")
        else:
            classification = WallToxicityClass.EXECUTED_LIQUIDITY
            notes.append("removals largely explained by aggressive trades")
    elif remote and strong_migration and strong_pull and not market.trades_in_bucket:
        classification = WallToxicityClass.REMOTE_LIQUIDITY_MIGRATION
        notes.append("remote wall; large unexplained removals matched by nearby adds")
    elif (
        not remote
        and strong_migration
        and strong_pull
        and (market.min_distance_bps or 999) <= params.near_market_bps
    ):
        classification = WallToxicityClass.NEAR_MARKET_LIQUIDITY_MIGRATION
        notes.append("near-market quantity shifts without sufficient trade cover")
    elif remote and strong_pull and migration.migration_event_count == 0:
        classification = WallToxicityClass.REMOTE_LIQUIDITY_PULL
        notes.append("remote unexplained removals without matched migration")
    elif market.removed_before_touch and strong_pull and not market.trades_in_bucket:
        classification = WallToxicityClass.PULLED_BEFORE_TOUCH
        notes.append("size withdrawn before touch without trade cover")
    elif (
        pull.large_pull_count == 0
        and migration.migration_event_count == 0
        and (pull.removed_without_trade_ratio or 0.0) < 0.3
    ):
        classification = WallToxicityClass.STABLE_PERSISTENT_WALL
        notes.append("limited pulling / migration evidence")
    elif strong_pull and not market.trades_in_bucket:
        classification = (
            WallToxicityClass.REMOTE_LIQUIDITY_PULL
            if remote
            else WallToxicityClass.PULLED_BEFORE_TOUCH
        )
        notes.append("fallback pull classification")

    # Never claim proven spoofing — suspicion only.
    if toxicity >= 70 and sc.remote_migration_score >= 50:
        suspicion = SpoofingSuspicion.HIGH
    elif toxicity >= 40 or (strong_migration and remote):
        suspicion = SpoofingSuspicion.MEDIUM
    else:
        suspicion = SpoofingSuspicion.LOW

    if classification == WallToxicityClass.REMOTE_LIQUIDITY_MIGRATION:
        # Known-case target profile: reliability low, suspicion at least medium
        reliability = min(reliability, 35.0)
        if suspicion == SpoofingSuspicion.LOW:
            suspicion = SpoofingSuspicion.MEDIUM

    return WallToxicityResult(
        classification=classification,
        reliability_score=round(reliability, 2),
        toxicity_score=round(toxicity, 2),
        spoofing_suspicion=suspicion,
        score_components=sc,
        pull=pull,
        migration=migration,
        market=market,
        notes="; ".join(notes),
    )
