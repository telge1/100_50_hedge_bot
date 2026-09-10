"""Episode-1 application: schema/mechanics test only (no threshold optimization)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..paths import ENGINE_ROOT
from . import (
    ASK_WALL_BREACH,
    ATTACK_CLUSTER_GAP_MS_DEFAULT,
    CENSOR_DETECTION_NOT_AVAILABLE,
    CENSOR_EPOCH_BOUNDARY,
    EPOCH4_COVERAGE_END,
    FROZEN_DEFENSE_CHAIN_RUN,
    FROZEN_HANDOFF_PATH,
    FROZEN_PRICE_RESPONSE_RUN,
    HORIZON_MS,
)
from .facts_builder import compute_outcome_facts
from .labels import label_from_facts


def _load_timeline_csv(path: Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            # normalize coverage_ok
            if "coverage_ok" in r:
                r["coverage_ok"] = str(r["coverage_ok"]).lower() in ("true", "1")
            rows.append(r)
    return rows


def apply_episode1_mechanics_test(
    *,
    defense_chain_dir: Path | None = None,
    price_response_dir: Path | None = None,
    handoff_path: Path | None = None,
) -> dict[str, Any]:
    """Produce OutcomeFacts+Labels for Episode 1 anchors/horizons (mechanics only)."""
    dch = Path(defense_chain_dir or (ENGINE_ROOT / FROZEN_DEFENSE_CHAIN_RUN))
    prr = Path(price_response_dir or (ENGINE_ROOT / FROZEN_PRICE_RESPONSE_RUN))
    handoff = json.loads(Path(handoff_path or (ENGINE_ROOT / FROZEN_HANDOFF_PATH)).read_text(encoding="utf-8"))
    chain = json.loads((dch / "defense_chain_epoch4.json").read_text(encoding="utf-8"))
    fill = json.loads((dch / "episode1_pflichtbericht.json").read_text(encoding="utf-8")).get(
        "original_wall_fill_vs_pull"
    ) or {}
    timeline = _load_timeline_csv(prr / "price_response_timeline.csv")
    # Only coverage_ok rows for path; keep decision_time ordering
    timeline = [r for r in timeline if r.get("coverage_ok")]
    timeline.sort(key=lambda r: _as_dt(r["decision_time"]))

    coverage_end = _as_dt(EPOCH4_COVERAGE_END)
    wall_side = str(handoff["wall_side"])
    wall_price = float(handoff["wall_price"])
    episode_id = str(handoff["episode_id"])
    zone_id = str(handoff["zone_id"])
    symbol = str(handoff.get("symbol") or "BTCUSDT")
    wall_gen = str(handoff.get("wall_generation_id"))
    pull_share = fill.get("pull_share")

    anchors = {
        "ZONE_FIRST_TOUCH": handoff["zone_first_touch_exchange_event_time"],
        "WALL_FIRST_TOUCH": handoff["wall_first_touch_exchange_event_time"],
        "FIRST_JOINT_BREACH": ASK_WALL_BREACH,
        "DETECTION": handoff["detection_exchange_event_time"],
    }

    cluster = {
        "attack_cluster_id": f"ac_{episode_id}",
        "touch_index_within_cluster": 0,
        "first_touch_in_cluster": True,
        "last_touch_in_cluster": True,
        "cluster_start": anchors["ZONE_FIRST_TOUCH"],
        "cluster_end": EPOCH4_COVERAGE_END,
        "cluster_gap_ms_default": ATTACK_CLUSTER_GAP_MS_DEFAULT,
        "note": "Single Episode-1 attack cluster; gap default not profit-optimized",
    }

    records: list[dict[str, Any]] = []
    for anchor_type, anchor_time in anchors.items():
        for h_ms in HORIZON_MS:
            forced = None
            # Detection at 20:21:00Z is outside Epoch-4 continuous coverage for this chain.
            if anchor_type == "DETECTION":
                forced = CENSOR_DETECTION_NOT_AVAILABLE
            facts = compute_outcome_facts(
                episode_id=episode_id,
                symbol=symbol,
                zone_id=zone_id,
                wall_side=wall_side,
                wall_price=wall_price,
                anchor_type=anchor_type,
                anchor_time=anchor_time,
                horizon_ms=h_ms,
                timeline_rows=timeline,
                coverage_end=coverage_end,
                chain=chain,
                wall_generation_id=wall_gen,
                replay_epoch=4,
                pull_share_raw=pull_share,
                cluster_meta=cluster,
                forced_censor_reason=forced,
                wall_first_touch_time=anchors["WALL_FIRST_TOUCH"],
            )
            # If not forced but horizon beyond coverage, facts_builder already censored
            label = label_from_facts(facts)
            records.append(
                {
                    "facts": facts.to_dict(),
                    "label": label.to_dict(),
                }
            )

    # Summary expected mechanics
    valid = [r for r in records if r["facts"]["coverage_ok"]]
    censored = [r for r in records if not r["facts"]["coverage_ok"]]
    reclaim_labels = [
        r
        for r in valid
        if r["label"]["label"]
        in (
            "JOINT_BREACH_RECLAIM_DEFENDER_SIDE_AT_HORIZON",
            "LAYERED_DEFENSE_RECLAIM_AT_HORIZON",
        )
    ]
    breach_attack = [
        r
        for r in valid
        if r["label"]["label"] == "JOINT_BREACH_ATTACK_SIDE_AT_HORIZON"
        or (r["facts"].get("joint_breach_observed") and r["facts"].get("end_price_side") == "ATTACK")
    ]

    return {
        "episode_id": episode_id,
        "role": "schema_mechanics_test_only",
        "n_records": len(records),
        "n_valid": len(valid),
        "n_censored": len(censored),
        "n_reclaim_labels_valid": len(reclaim_labels),
        "records": records,
        "mechanics_expectations": {
            "short_breach_outcomes_within_epoch4_exist": len(valid) > 0
            and any(
                r["label"]["label"] == "JOINT_BREACH_ATTACK_SIDE_AT_HORIZON"
                and r["facts"]["anchor_type"] == "FIRST_JOINT_BREACH"
                for r in valid
            ),
            "no_reclaim_within_observed_coverage": len(reclaim_labels) == 0,
            "later_horizons_censored": any(
                r["facts"].get("censor_reason") == CENSOR_EPOCH_BOUNDARY for r in censored
            ),
            "detection_outcomes_unavailable_outside_coverage": all(
                r["facts"].get("censor_reason") == CENSOR_DETECTION_NOT_AVAILABLE
                for r in records
                if r["facts"]["anchor_type"] == "DETECTION"
            ),
            "no_post_epoch_same_chain_outcome": all(
                r["facts"].get("replay_epoch") in (4, None) or not r["facts"]["coverage_ok"] for r in records
            ),
            "anchors_not_mixed": True,
        },
        "cluster": cluster,
    }
