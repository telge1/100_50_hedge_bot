"""Feed attributed intervals into existing FootprintClusterEvent builder — no new formula."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (
    CanonicalTrade,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    WallFlowEvent,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.footprint_cluster import (
    FootprintClusterEvent,
    build_footprint_cluster_event,
)
from obfull_research_engine.timeparse import format_utc_z


@dataclass
class FootprintLiveAdapter:
    episode_id: str
    wall_id: str
    wall_side: str
    wall_price: float
    band_low: float
    band_high: float
    clusters: list[FootprintClusterEvent] = field(default_factory=list)
    last_summary: dict[str, Any] = field(default_factory=dict)

    def from_trades_and_interval(
        self,
        trades: list[CanonicalTrade],
        *,
        interval_start: datetime,
        interval_end: datetime,
        attributed_hit_qty: float,
        attributed_hit_notional: float,
        confidence: str = "MEDIUM",
        replay_epoch: int | None = 1,
        touch_at: datetime | None = None,
        decision_at: datetime | None = None,
    ) -> FootprintClusterEvent | None:
        trade_ids = [str(t.trade_id) for t in trades]
        now = format_utc_z(datetime.now(timezone.utc))
        symbol = trades[0].symbol if trades else ""
        interval = WallFlowEvent(
            episode_id=self.episode_id,
            symbol=symbol,
            zone_id=f"zone:{self.wall_id}",
            wall_id=self.wall_id,
            wall_side=self.wall_side,
            wall_price=self.wall_price,
            band_low=self.band_low,
            band_high=self.band_high,
            view="exact_price",
            event_type="ATTRIBUTION_INTERVAL",
            interval_start_exchange_time=format_utc_z(interval_start),
            interval_end_exchange_time=format_utc_z(interval_end),
            interval_start_available_at=format_utc_z(interval_start),
            interval_end_available_at=format_utc_z(interval_end),
            max_input_available_at=format_utc_z(interval_end),
            computed_at=now,
            attribution_available_at=now,
            replay_epoch=replay_epoch,
            book_sequence_start=None,
            book_sequence_end=None,
            book_update_id_start=None,
            book_update_id_end=None,
            queue_before=0.0,
            queue_after=0.0,
            delta_queue=0.0,
            attributed_trade_count=len(trade_ids),
            attributed_trade_ids=trade_ids,
            attributed_trade_ids_hash="",
            attributed_hit_qty=float(attributed_hit_qty),
            attributed_hit_notional=float(attributed_hit_notional),
            net_refill_qty=0.0,
            residual_pull_qty=0.0,
            net_depletion_qty=float(attributed_hit_qty),
            aggregate_queue_survival_proxy=1.0,
            aggressor_side="Buy" if self.wall_side.lower() == "ask" else "Sell",
            attribution_rule_version="live_adapter_v1",
            attribution_confidence=confidence,
            mass_balance_error=0.0,
            coverage_ok=True,
            look_ahead=False,
            source_record_ids=[],
        )
        touch = touch_at or interval_start
        decision = decision_at or interval_end
        ev = build_footprint_cluster_event(
            event_id=f"fp:{self.wall_id}:{format_utc_z(interval_end)}",
            episode_id=self.episode_id,
            wall_id=self.wall_id,
            wall_side=self.wall_side,
            wall_price=self.wall_price,
            band_low=self.band_low,
            band_high=self.band_high,
            band_events=[interval],
            decision_at=decision,
            touch_at=touch,
            coverage_ok=True,
            flow_attribution_confidence=confidence,
            availability_confidence="LIVE",
            attributed_hit_notional_override=float(attributed_hit_notional),
        )
        self.clusters.append(ev)
        buy_qty = sum(t.size_base for t in trades if t.taker_side == "Buy")
        sell_qty = sum(t.size_base for t in trades if t.taker_side == "Sell")
        self.last_summary = {
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "signed_delta_notional": sum(
                t.notional_usdt if t.taker_side == "Buy" else -t.notional_usdt for t in trades
            ),
            "trade_count": len(trades),
            "cluster": ev.to_dict(),
        }
        return ev

    def summary(self) -> dict[str, Any]:
        if self.clusters:
            return self.clusters[-1].to_dict()
        return dict(self.last_summary)
