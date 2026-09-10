"""Apply wall_defense_outcome_contract_v1 per episode/cluster/anchor/horizon."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..wall_defense_outcome_contract_v1 import (
    ANCHOR_TYPES,
    CONTRACT_HASH,
    HORIZON_MS,
    LABEL_CENSORED,
    outcome_contract_hash,
)
from ..wall_defense_outcome_contract_v1.facts_builder import compute_outcome_facts
from ..wall_defense_outcome_contract_v1.labels import label_from_facts
from . import EXPECTED_CONTRACT_HASH
from .exclusions import OUTCOME_CONTRACT_HASH_MISMATCH, exclusion_row


def verify_contract_hash() -> str:
    h = outcome_contract_hash()
    if h != EXPECTED_CONTRACT_HASH or CONTRACT_HASH != EXPECTED_CONTRACT_HASH:
        raise RuntimeError(
            f"outcome contract hash mismatch: got {h} / {CONTRACT_HASH}, "
            f"expected {EXPECTED_CONTRACT_HASH}"
        )
    return h


_FLOAT_KEYS = (
    "midprice",
    "microprice",
    "best_bid",
    "best_ask",
    "best_bid_size",
    "best_ask_size",
    "wall_price",
)


def sanitize_timeline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CSV DictReader yields '' for empty cells — coerce to None / typed values."""
    out: list[dict[str, Any]] = []
    for raw in rows:
        r = dict(raw)
        for k in _FLOAT_KEYS:
            if k not in r:
                continue
            v = r[k]
            if v is None or v == "":
                r[k] = None
                continue
            try:
                r[k] = float(v)
            except (TypeError, ValueError):
                r[k] = None
        if "coverage_ok" in r and isinstance(r["coverage_ok"], str):
            r["coverage_ok"] = r["coverage_ok"].strip().lower() in ("true", "1", "yes")
        out.append(r)
    return out


def _anchor_time_map(
    *,
    zone_touch: dict[str, Any],
    wall_touch: dict[str, Any] | None,
    detection: dict[str, Any] | None,
    first_joint_breach: str | None,
) -> dict[str, datetime | None]:
    m: dict[str, datetime | None] = {
        "ZONE_FIRST_TOUCH": parse_utc(zone_touch["exchange_event_time"]),
        "WALL_FIRST_TOUCH": None,
        "FIRST_JOINT_BREACH": parse_utc(first_joint_breach) if first_joint_breach else None,
        "DETECTION": None,
        "DECISION_TIME": parse_utc(zone_touch["exchange_event_time"]),
    }
    if wall_touch and wall_touch.get("exchange_event_time"):
        m["WALL_FIRST_TOUCH"] = parse_utc(wall_touch["exchange_event_time"])
    if detection and detection.get("available") and detection.get("exchange_event_time"):
        m["DETECTION"] = parse_utc(detection["exchange_event_time"])
    return m


def apply_outcomes_for_episode(
    *,
    episode_id: str,
    symbol: str,
    zone_id: str,
    wall_side: str,
    wall_price: float,
    zone_touch: dict[str, Any],
    wall_touch: dict[str, Any] | None,
    detection: dict[str, Any] | None,
    timeline_rows: list[dict[str, Any]],
    coverage_end: datetime | str,
    attack_cluster_id: str | None = None,
    chain: Any = None,
    wall_generation_id: str | None = None,
    replay_epoch: int | None = None,
    first_joint_breach: str | None = None,
    cluster_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply contract for each anchor×horizon. Split complete vs censored.
    Does NOT call apply_episode1_mechanics_test.
    """
    try:
        contract_hash = verify_contract_hash()
    except RuntimeError as exc:
        return {
            "ok": False,
            "exclusion": exclusion_row(
                reason=OUTCOME_CONTRACT_HASH_MISMATCH,
                subject_id=episode_id,
                detail=str(exc),
                stage="outcomes",
            ),
            "complete": [],
            "censored": [],
        }

    cov_end = parse_utc(coverage_end)
    anchors = _anchor_time_map(
        zone_touch=zone_touch,
        wall_touch=wall_touch,
        detection=detection,
        first_joint_breach=first_joint_breach,
    )
    wall_first = anchors.get("WALL_FIRST_TOUCH")
    complete: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    timeline_rows = sanitize_timeline_rows(list(timeline_rows or []))

    for anchor_type in ANCHOR_TYPES:
        if anchor_type not in ("ZONE_FIRST_TOUCH", "WALL_FIRST_TOUCH", "FIRST_JOINT_BREACH", "DETECTION"):
            # Skip DECISION_TIME for generic builder default matrix (optional later)
            if anchor_type == "DECISION_TIME":
                continue
        at = anchors.get(anchor_type)
        if at is None:
            continue
        for horizon_ms in HORIZON_MS:
            try:
                facts = compute_outcome_facts(
                    episode_id=episode_id,
                    symbol=symbol,
                    zone_id=zone_id,
                    wall_side=wall_side,
                    wall_price=float(wall_price),
                    anchor_type=anchor_type,
                    anchor_time=at,
                    horizon_ms=int(horizon_ms),
                    timeline_rows=timeline_rows,
                    coverage_end=cov_end,
                    chain=chain,
                    wall_generation_id=wall_generation_id,
                    replay_epoch=replay_epoch,
                    cluster_meta=cluster_meta,
                    wall_first_touch_time=wall_first,
                )
            except (TypeError, ValueError, KeyError) as exc:
                from ..wall_defense_outcome_contract_v1 import CENSOR_BOOK_COVERAGE_MISSING
                from ..timeparse import format_utc_z
                from ..wall_defense_outcome_contract_v1.models import empty_facts_template

                facts = empty_facts_template(
                    episode_id=episode_id,
                    symbol=symbol,
                    zone_id=zone_id,
                    wall_side=wall_side,
                    anchor_type=anchor_type,
                    anchor_time=format_utc_z(at),
                    horizon_ms=int(horizon_ms),
                )
                facts.coverage_ok = False
                facts.censor_reason = CENSOR_BOOK_COVERAGE_MISSING
                # Keep error in a non-schema field via setattr if available
                if hasattr(facts, "pull_share_raw"):
                    pass
                facts_d_err = {"timeline_coercion_error": str(exc)}
                label = label_from_facts(facts)
                facts_d = facts.to_dict() if hasattr(facts, "to_dict") else dict(facts.__dict__)
                facts_d.update(facts_d_err)
                label_d = label.to_dict() if hasattr(label, "to_dict") else dict(label.__dict__)
                censored.append(
                    {
                        "episode_id": episode_id,
                        "attack_cluster_id": attack_cluster_id,
                        "anchor_type": anchor_type,
                        "horizon_ms": int(horizon_ms),
                        "outcome_contract_hash": contract_hash,
                        "facts": facts_d,
                        "label": label_d,
                    }
                )
                continue
            label = label_from_facts(facts)
            facts_d = facts.to_dict() if hasattr(facts, "to_dict") else dict(facts.__dict__)
            label_d = label.to_dict() if hasattr(label, "to_dict") else dict(label.__dict__)
            row = {
                "episode_id": episode_id,
                "attack_cluster_id": attack_cluster_id,
                "anchor_type": anchor_type,
                "horizon_ms": int(horizon_ms),
                "outcome_contract_hash": contract_hash,
                "facts": facts_d,
                "label": label_d,
            }
            lab = label_d.get("label") or label_d.get("mechanical_label")
            if lab == LABEL_CENSORED or facts_d.get("censored") is True or facts_d.get("coverage_ok") is False:
                censored.append(row)
            else:
                complete.append(row)

    return {
        "ok": True,
        "outcome_contract_hash": contract_hash,
        "complete": complete,
        "censored": censored,
        "n_complete": len(complete),
        "n_censored": len(censored),
    }
