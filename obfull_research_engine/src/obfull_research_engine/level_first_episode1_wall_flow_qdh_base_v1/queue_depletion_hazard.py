"""Queue Depletion Hazard (QDH_base) and QDH_toxic_base_only.

QDH_toxic_base_only = QDH_base * M_persistence with M_OI=M_LIQ=1.0.
Not a validated trading score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import EPSILON, M_LIQ, M_OI, PERSISTENCE_MAX, PERSISTENCE_MIN, QDH_HALF_LIFE_MS
from .aggressor_flow import clip, ewma_update


@dataclass
class QdhState:
    net_depletion_rate: float = 0.0
    qdh_base: float = 0.0
    qdh_base_slope: float = 0.0
    qdh_base_peak_so_far: float = 0.0
    queue_runway_seconds: float | None = None
    m_persistence: float = 1.0
    m_oi: float = M_OI
    m_liq: float = M_LIQ
    qdh_toxic_base_only: float = 0.0
    prev_qdh: float = 0.0


def update_qdh(
    state: QdhState,
    *,
    net_depletion_qty: float,
    interval_duration_s: float,
    current_queue: float,
    persistence_ratio: float,
    half_life_ms: float = QDH_HALF_LIFE_MS,
    persistence_min: float = PERSISTENCE_MIN,
    persistence_max: float = PERSISTENCE_MAX,
) -> QdhState:
    dur = max(float(interval_duration_s), EPSILON)
    raw_rate = float(net_depletion_qty) / dur
    dep_rate = ewma_update(state.net_depletion_rate, raw_rate, dt_s=dur, half_life_s=half_life_ms / 1000.0)
    if dep_rate <= 0:
        qdh = 0.0
        runway = None  # infinity / null serialization
    else:
        qdh = max(dep_rate, 0.0) / (float(current_queue) + EPSILON)
        runway = 1.0 / (qdh + EPSILON)
    slope = (qdh - state.prev_qdh) / dur
    peak = max(state.qdh_base_peak_so_far, qdh)
    m_pers = clip(float(persistence_ratio), persistence_min, persistence_max)
    toxic = qdh * m_pers  # M_OI * M_LIQ = 1
    return QdhState(
        net_depletion_rate=dep_rate,
        qdh_base=qdh,
        qdh_base_slope=slope,
        qdh_base_peak_so_far=peak,
        queue_runway_seconds=runway,
        m_persistence=m_pers,
        m_oi=M_OI,
        m_liq=M_LIQ,
        qdh_toxic_base_only=toxic,
        prev_qdh=qdh,
    )


def qdh_to_dict(state: QdhState) -> dict[str, Any]:
    return {
        "net_depletion_rate": state.net_depletion_rate,
        "qdh_base": state.qdh_base,
        "qdh_base_slope": state.qdh_base_slope,
        "qdh_base_peak_so_far": state.qdh_base_peak_so_far,
        "queue_runway_seconds": state.queue_runway_seconds,
        "M_persistence": state.m_persistence,
        "M_OI": state.m_oi,
        "M_LIQ": state.m_liq,
        "qdh_toxic_base_only": state.qdh_toxic_base_only,
        "persistence_min_research_default": PERSISTENCE_MIN,
        "persistence_max_research_default": PERSISTENCE_MAX,
        "qdh_half_life_ms_research_default": QDH_HALF_LIFE_MS,
    }
