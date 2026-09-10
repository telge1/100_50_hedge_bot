"""Best-effort ask defense chain from generations + wall-flow timeline; fail closed."""

from __future__ import annotations

from typing import Any

from ..level_first_episode1_defense_chain_v1.chain_builder import build_ask_defense_chain
from ..level_first_episode1_defense_chain_v1.relevance import score_generations_past_only
from .exclusions import (
    DEFENSE_CHAIN_ASK_ONLY_V1,
    DEFENSE_CHAIN_INPUTS_MISSING,
    exclusion_row,
)


def run_defense_chain_stage(
    *,
    wall_side: str,
    generations: list[dict[str, Any]] | None,
    wall_flow_rows: list[dict[str, Any]] | None,
    liquidity_rows: list[dict[str, Any]] | None,
    replay_epoch: int,
    coverage_start: str,
    coverage_end: str,
    wall_price: float,
    episode_id: str,
    breach_iso: str | None = None,
) -> dict[str, Any]:
    if str(wall_side).lower() != "ask":
        return {
            "ok": False,
            "skipped": True,
            "exclusion": exclusion_row(
                reason=DEFENSE_CHAIN_ASK_ONLY_V1,
                subject_id=episode_id,
                detail=f"wall_side={wall_side}",
                stage="defense_chain",
            ),
        }
    if not generations:
        return {
            "ok": False,
            "skipped": True,
            "exclusion": exclusion_row(
                reason=DEFENSE_CHAIN_INPUTS_MISSING,
                subject_id=episode_id,
                detail="no generations for score_generations_past_only",
                stage="defense_chain",
            ),
        }
    try:
        scored = score_generations_past_only(
            generations,
            liquidity_by_time=liquidity_rows,
            wall_price=float(wall_price),
        )
        chain = build_ask_defense_chain(
            scored_gens=scored,
            replay_epoch=int(replay_epoch),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            wall_flow_rows=wall_flow_rows,
            liquidity_rows=liquidity_rows,
            wall_price=float(wall_price),
            episode_id=episode_id,
            breach_iso=breach_iso,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "skipped": True,
            "exclusion": exclusion_row(
                reason=DEFENSE_CHAIN_INPUTS_MISSING,
                subject_id=episode_id,
                detail=str(exc),
                stage="defense_chain",
            ),
        }
    payload = chain.to_dict() if hasattr(chain, "to_dict") else chain
    if hasattr(chain, "__dict__") and not isinstance(payload, dict):
        from dataclasses import asdict, is_dataclass

        payload = asdict(chain) if is_dataclass(chain) else {"chain": str(chain)}
    return {"ok": True, "skipped": False, "chain": payload, "scored_generations": scored}
