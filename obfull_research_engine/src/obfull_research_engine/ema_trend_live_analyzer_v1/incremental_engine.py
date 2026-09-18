"""Thin incremental wrapper around canonical engines — no formula copies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow import (
    AggressorState,
    update_aggressor,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance import (
    decompose_mass_balance,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.price_response import (
    PriceResponseState,
    microprice,
    update_price_response,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (
    QdhState,
    qdh_to_dict,
    update_qdh,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    BookNode,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.flow_v2 import recompute_bucket_mass

from . import ENGINE_VERSION
from .schema import iso_z


@dataclass
class IncrementalEngineState:
    """Holds stateful canonical objects; advances only when coverage valid."""

    tick_size: float
    wall_price: float
    wall_side: str
    direction: int  # +1 ask-attack (buy), -1 bid-attack (sell)
    qdh: QdhState = field(default_factory=QdhState)
    aggressor: AggressorState = field(default_factory=AggressorState)
    price_resp: PriceResponseState = field(default_factory=PriceResponseState)
    last_queue: float | None = None
    cumulative_hit_qty: float = 0.0
    cumulative_hit_notional: float = 0.0
    cumulative_refill: float = 0.0
    cumulative_pull: float = 0.0
    queue_at_touch: float | None = None
    mass_ok: bool | None = None
    last_mass: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    engine_version: str = ENGINE_VERSION

    def __post_init__(self) -> None:
        self.price_resp.direction = self.direction
        if self.tick_size is None or float(self.tick_size) <= 0:
            raise ValueError("tick_size_required")

    def on_nodes(
        self,
        nodes: list[BookNode],
        *,
        attributed_hit_qty: float = 0.0,
        attributed_hit_notional: float = 0.0,
        hit_trade_count: int = 0,
        interval_duration_s: float = 0.1,
        confidence: str = "MEDIUM",
        best_bid: float | None = None,
        bid_size: float = 1.0,
        best_ask: float | None = None,
        ask_size: float = 1.0,
        exact_features_valid: bool = True,
    ) -> dict[str, Any]:
        if not exact_features_valid or self.blocked:
            self.blocked = True
            return {"blocked": True, "reason": "BLOCKED_COVERAGE", "engine_version": self.engine_version}
        if not nodes:
            return {"blocked": False, "engine_version": self.engine_version}
        q0 = self.last_queue if self.last_queue is not None else float(nodes[0].queue)
        q1 = float(nodes[-1].queue)
        if self.queue_at_touch is None:
            self.queue_at_touch = q0
        mass = recompute_bucket_mass(
            queue_before=q0,
            queue_after=q1,
            raw_fill=float(attributed_hit_qty),
            confidence=confidence,
        )
        # UNKNOWN must not enter QDH depletion
        net_for_qdh = float(mass["attributed_fill_capped"]) + float(mass["residual_pull"]) - float(
            mass["refill"]
        )
        if float(mass["unknown"]) > 1e-15:
            # unknown replaces pull; excluded from depletion already in recompute
            pass
        mb = decompose_mass_balance(
            queue_before=q0,
            queue_after=q1,
            attributed_hit_qty=float(mass["attributed_fill_capped"]),
        )
        self.mass_ok = mb.identity_ok
        self.last_mass = {
            **mass,
            "mass_balance_error": mb.mass_balance_error,
            "identity_ok": mb.identity_ok,
        }
        self.cumulative_hit_qty += float(mass["attributed_fill_capped"])
        self.cumulative_hit_notional += float(attributed_hit_notional)
        self.cumulative_refill += float(mass["refill"])
        self.cumulative_pull += float(mass["residual_pull"])

        self.aggressor = update_aggressor(
            self.aggressor,
            hit_qty=float(mass["attributed_fill_capped"]),
            hit_notional=float(attributed_hit_notional),
            hit_trade_count=int(hit_trade_count),
            interval_duration_s=float(interval_duration_s),
            exchange_time=nodes[-1].exchange_event_time,
            interarrival_ms_samples=[],
            buy_hit_qty=float(mass["attributed_fill_capped"]) if self.direction > 0 else 0.0,
            sell_hit_qty=float(mass["attributed_fill_capped"]) if self.direction < 0 else 0.0,
        )
        self.qdh = update_qdh(
            self.qdh,
            net_depletion_qty=net_for_qdh,
            interval_duration_s=float(interval_duration_s),
            current_queue=q1,
            persistence_ratio=self.aggressor.persistence_ratio,
        )
        bb = best_bid if best_bid is not None else self.wall_price - self.tick_size
        ba = best_ask if best_ask is not None else self.wall_price + self.tick_size
        self.price_resp = update_price_response(
            self.price_resp,
            best_bid=bb,
            bid_size=bid_size,
            best_ask=ba,
            ask_size=ask_size,
            cumulative_hit_notional_since_wall_touch=self.cumulative_hit_notional,
            cumulative_hit_qty=self.cumulative_hit_qty,
            cumulative_net_refill_qty=self.cumulative_refill,
            cumulative_residual_pull_qty=self.cumulative_pull,
            current_defended_band_queue=q1,
            queue_at_wall_touch=float(self.queue_at_touch),
            wall_price=self.wall_price,
            tick_size=self.tick_size,
        )
        self.last_queue = q1
        mp = microprice(bb, bid_size, ba, ask_size)
        return {
            "blocked": False,
            "engine_version": self.engine_version,
            "mass": self.last_mass,
            "mass_balance_ok": self.mass_ok,
            "qdh": qdh_to_dict(self.qdh),
            "persistence_ratio": self.aggressor.persistence_ratio,
            "impact_efficiency": self.price_resp.impact_efficiency_bps_per_million,
            "microprice": mp,
            "queue": q1,
            "last_exchange_time": iso_z(nodes[-1].exchange_event_time),
        }
