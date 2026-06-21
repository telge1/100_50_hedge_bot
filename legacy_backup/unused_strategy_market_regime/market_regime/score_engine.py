from __future__ import annotations

from .models import NormalizedSnapshot, PrimitiveEvents, ScoreSnapshot


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def compute_pressure_score(snapshot: NormalizedSnapshot, events: PrimitiveEvents) -> tuple[float, dict[str, float]]:
    base_terms = {
        "price_change_1m_z": 25.0 * clamp(snapshot.z("price_change_1m"), -2.0, 2.0),
        "price_change_5m_z": 20.0 * clamp(snapshot.z("price_change_5m"), -2.0, 2.0),
        "orderflow_ratio_z": 20.0 * clamp(snapshot.z("orderflow_ratio"), -2.0, 2.0),
        "delta_ratio_z": 15.0 * clamp(snapshot.z("delta_ratio"), -2.0, 2.0),
        "velocity_1m_z": 20.0 * clamp(snapshot.z("velocity_1m"), -2.0, 2.0),
    }
    pressure_score_raw = sum(base_terms.values()) / 2.0

    event_adjustment = 0.0
    event_adjustment += 8.0 if events.price_impulse_up else 0.0
    event_adjustment -= 8.0 if events.price_impulse_down else 0.0
    event_adjustment += 10.0 if events.orderflow_push_long else 0.0
    event_adjustment -= 10.0 if events.orderflow_push_short else 0.0
    event_adjustment += 6.0 if events.price_flip_long else 0.0
    event_adjustment -= 6.0 if events.price_flip_short else 0.0

    final_score = clamp(pressure_score_raw + event_adjustment, -100.0, 100.0)
    debug = dict(base_terms)
    debug["event_adjustment"] = event_adjustment
    debug["final"] = final_score
    return final_score, debug


def compute_participation_score(snapshot: NormalizedSnapshot) -> tuple[float, dict[str, float]]:
    terms = {
        "oi_change_ratio_z": 30.0 * clamp(snapshot.z("oi_change_ratio"), 0.0, 2.0),
        "volume_spike_ratio_z": 25.0 * clamp(snapshot.z("volume_spike_ratio"), 0.0, 2.0),
        "trade_count_1m_z": 25.0 * clamp(snapshot.z("trade_count_1m"), 0.0, 2.0),
        "avg_trade_size_z": 20.0 * clamp(snapshot.z("avg_trade_size"), 0.0, 2.0),
    }
    final_score = clamp(sum(terms.values()), 0.0, 100.0)
    terms["final"] = final_score
    return final_score, terms


def compute_instability_score(snapshot: NormalizedSnapshot) -> tuple[float, dict[str, float]]:
    terms = {
        "microburst_score_z": 30.0 * clamp(snapshot.z("microburst_score"), 0.0, 2.0),
        "liquidation_density_5m_z": 25.0 * clamp(snapshot.z("liquidation_density_5m"), 0.0, 2.0),
        "liquidation_cluster_score_z": 20.0 * clamp(snapshot.z("liquidation_cluster_score"), 0.0, 2.0),
        "spread_ratio_z": 25.0 * clamp(snapshot.z("spread_ratio"), 0.0, 2.0),
    }
    final_score = clamp(sum(terms.values()), 0.0, 100.0)
    terms["final"] = final_score
    return final_score, terms


def compute_exhaustion_score(events: PrimitiveEvents) -> tuple[float, dict[str, float]]:
    velocity_slowdown_flag = int(events.velocity_slowdown_long or events.velocity_slowdown_short)
    pressure_divergence_flag = int(events.pressure_divergence_long or events.pressure_divergence_short)
    oi_flush_flag = int(events.oi_flush)
    microburst_risk_flag = int(events.microburst_risk)
    liq_cluster_event_flag = int(events.liq_cluster_event)

    terms = {
        "velocity_slowdown_flag": 30.0 * velocity_slowdown_flag,
        "pressure_divergence_flag": 25.0 * pressure_divergence_flag,
        "oi_flush_flag": 20.0 * oi_flush_flag,
        "microburst_risk_flag": 15.0 * microburst_risk_flag,
        "liq_cluster_event_flag": 10.0 * liq_cluster_event_flag,
    }
    final_score = clamp(sum(terms.values()), 0.0, 100.0)
    terms["final"] = final_score
    return final_score, terms


def compute_scores(snapshot: NormalizedSnapshot, events: PrimitiveEvents) -> ScoreSnapshot:
    pressure_score, pressure_debug = compute_pressure_score(snapshot, events)
    participation_score, participation_debug = compute_participation_score(snapshot)
    instability_score, instability_debug = compute_instability_score(snapshot)
    exhaustion_score, exhaustion_debug = compute_exhaustion_score(events)
    return ScoreSnapshot(
        pressure_score=pressure_score,
        participation_score=participation_score,
        instability_score=instability_score,
        exhaustion_score=exhaustion_score,
        debug={
            "pressure": pressure_debug,
            "participation": participation_debug,
            "instability": instability_debug,
            "exhaustion": exhaustion_debug,
        },
    )
