"""Ask-side wall-flow via frozen compute_wall_flow_bundle; bid walls excluded."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..level_first_episode1_wall_flow_qdh_base_v1.pipeline import (
    compute_wall_flow_bundle,
    write_wall_flow_outputs,
)
from .exclusions import WALL_FLOW_FROZEN_ASK_ONLY_V1, exclusion_row
from .handoff import Episode1Handoff, wall_flow_inputs_from_handoff


def run_wall_flow_stage(
    *,
    handoff: Episode1Handoff,
    persist_dir: Path,
    trades_path: Path,
    out_dir: Path,
    run_key: str,
    band_ticks: int = 5,
    tick_size: float = 0.1,
) -> dict[str, Any]:
    """
    Ask walls only. Bid walls get explicit exclusion WALL_FLOW_FROZEN_ASK_ONLY_V1
    but discovery/handoff/price/outcomes still proceed elsewhere.
    """
    if str(handoff.wall_side).lower() != "ask":
        return {
            "ok": False,
            "skipped": True,
            "exclusion": exclusion_row(
                reason=WALL_FLOW_FROZEN_ASK_ONLY_V1,
                subject_id=handoff.episode_id,
                detail=f"wall_side={handoff.wall_side}; frozen package hardcodes ask",
                stage="wall_flow",
            ),
        }
    inputs = wall_flow_inputs_from_handoff(handoff)
    bundle = compute_wall_flow_bundle(
        persist_dir=Path(persist_dir),
        trades_path=Path(trades_path),
        zone_touch=inputs["zone_touch"],
        wall_observation=inputs["wall_observation"],
        wall_touch=inputs["wall_touch"],
        detection=inputs["detection"],
        band_ticks=band_ticks,
        tick_size=tick_size,
        wall_price=float(handoff.wall_price),
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes = write_wall_flow_outputs(out_dir, bundle, run_key=run_key)
    return {
        "ok": True,
        "skipped": False,
        "out_dir": str(out_dir),
        "hashes": hashes,
        "bundle_keys": sorted(bundle.keys()),
        "timeline_path": str(out_dir / "feature_timeline.csv"),
    }
