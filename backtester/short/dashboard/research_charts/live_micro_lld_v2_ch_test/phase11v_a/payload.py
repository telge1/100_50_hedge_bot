"""Generic historical heatmap payload. Colors come from the original strength map."""

from __future__ import annotations

from ..remaining_pool_projection_v1_config import STRENGTH_CLASS_COLORS


def generic_payload(result: dict) -> dict:
    active = []
    for pool in result.get("active") or []:
        color = STRENGTH_CLASS_COLORS.get(str(pool.get("strength_class")))
        if color is None or pool.get("color") not in STRENGTH_CLASS_COLORS.values():
            raise ValueError(f"strength color is not an original class for {pool.get('pool_id')}")
        if float(pool["top"]) <= float(pool["bottom"]):
            raise ValueError(f"original bounds collapsed for {pool.get('pool_id')}")
        active.append(
            {
                "pool_id": pool["pool_id"],
                "side": pool["side"],
                "original_bottom": pool["bottom"],
                "original_top": pool["top"],
                "strength": pool["strength"],
                "strength_class": pool["strength_class"],
                "color": color,
                "known_at": pool["known_at"],
            }
        )
    return {
        "symbol": result["symbol"],
        "timeframe": result["timeframe"],
        "pool_detail": result["pool_detail"],
        "decision_close": result["decision_close"],
        "original_snapshot_id": result["published_snapshot_id"],
        "immutable_input_id": result["immutable_input_id"],
        "coverage_status": result.get("coverage"),
        "original_active_pools": active,
        "remaining_segments": list(result.get("remaining") or []),
        "fully_traversed_active_ids": list(result.get("fully_traversed_active_ids") or []),
        "payload_id": result.get("payload_id"),
        "clickhouse_candle_reads": int(result.get("clickhouse_candle_reads") or 0),
    }
