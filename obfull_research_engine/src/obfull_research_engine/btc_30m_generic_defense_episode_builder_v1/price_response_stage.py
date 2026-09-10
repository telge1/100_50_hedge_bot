"""Wrap frozen run_price_response."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..level_first_episode1_price_response_reclaim_v1.pipeline import run_price_response
from .exclusions import PRICE_RESPONSE_FAILED, exclusion_row
from .handoff import Episode1Handoff, HandoffError


def run_price_response_stage(
    *,
    handoff: Episode1Handoff | dict[str, Any],
    persist_dir: Path,
    out_dir: Path,
    run_key: str,
    wall_flow_timeline: Path | None = None,
    skip_generation_check: bool = True,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_price_response(
            run_key=run_key,
            out_dir=out_dir,
            handoff=handoff,
            persist_dir=Path(persist_dir),
            wall_flow_timeline=wall_flow_timeline,
            skip_generation_check=skip_generation_check,
        )
    except HandoffError as exc:
        return {
            "ok": False,
            "exclusion": exclusion_row(
                reason=PRICE_RESPONSE_FAILED,
                detail=str(exc),
                stage="price_response",
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "exclusion": exclusion_row(
                reason=PRICE_RESPONSE_FAILED,
                detail=str(exc),
                stage="price_response",
            ),
        }
    return {"ok": True, "result": result, "out_dir": str(out_dir)}
