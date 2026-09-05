"""Two-layer research output: EMA setup (Stage A) vs microstructure confirmation (Stage B).

Paket 2F — ``confirmation_mode`` separates chart/research layers without inventing
trade direction on the EMA-only path.
"""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING

EZM_STRATEGY_ID = "ema_zone_microstructure_confirmation_v1"

CONFIRMATION_MODE_EMA_ONLY = "ema_only"
CONFIRMATION_MODE_EMA_PLUS_MICRO = "ema_plus_microstructure"

# Job-level computation mode (before job start) — distinct from row ``confirmation_mode``.
COMPUTATION_MODE_EMA_ONLY = CONFIRMATION_MODE_EMA_ONLY
COMPUTATION_MODE_EMA_PLUS_MICRO = CONFIRMATION_MODE_EMA_PLUS_MICRO

OUTPUT_LAYER_EMA_SETUP = "ema_setup"
OUTPUT_LAYER_MICROSTRUCTURE = "microstructure_confirmation"


def normalize_computation_mode(mode: str | None) -> str:
    """Normalize job-level EZM computation mode."""
    text = str(mode or "").strip().lower()
    if text in ("", COMPUTATION_MODE_EMA_PLUS_MICRO, "ema_plus_micro", "full"):
        return COMPUTATION_MODE_EMA_PLUS_MICRO
    if text == COMPUTATION_MODE_EMA_ONLY:
        return COMPUTATION_MODE_EMA_ONLY
    raise ValueError(f"INVALID_COMPUTATION_MODE:{text or 'empty'}")


def make_setup_id(*, symbol: str, zone_key: str, anchor_ms: int) -> str:
    """Stable setup key: symbol + zone + causal anchor timestamp (ms)."""
    return f"{symbol}_{zone_key}_{int(anchor_ms)}"


def ema_setup_layer_fields(
    *,
    setup_id: str,
    symbol: str,
    zone_key: str,
    touch_at: str,
    episode_id: str = "",
    zone_watch_started_at: str = "",
) -> dict[str, Any]:
    """Fields persisted on EMA-setup layer rows (no LONG/SHORT)."""
    return {
        "setup_id": setup_id,
        "episode_id": episode_id or setup_id,
        "symbol": symbol,
        "strategy_id": EZM_STRATEGY_ID,
        "zone": zone_key,
        "zone_name": zone_key,
        "touch_at": touch_at if touch_at not in (None, "", MISSING) else MISSING,
        "zone_touch_at": touch_at if touch_at not in (None, "", MISSING) else MISSING,
        "zone_watch_started_at": zone_watch_started_at or MISSING,
        "confirmation_mode": CONFIRMATION_MODE_EMA_ONLY,
        "output_layer": OUTPUT_LAYER_EMA_SETUP,
    }


def microstructure_layer_fields(
    *,
    setup_id: str,
    symbol: str,
    zone_key: str,
    touch_at: str,
    episode_id: str,
) -> dict[str, Any]:
    """Fields linking Stage-B micro results back to the EMA setup."""
    return {
        "setup_id": setup_id,
        "episode_id": episode_id,
        "symbol": symbol,
        "strategy_id": EZM_STRATEGY_ID,
        "zone": zone_key,
        "zone_name": zone_key,
        "touch_at": touch_at,
        "zone_touch_at": touch_at,
        "confirmation_mode": CONFIRMATION_MODE_EMA_PLUS_MICRO,
        "output_layer": OUTPUT_LAYER_MICROSTRUCTURE,
    }


def build_ema_setup_event(
    *,
    setup_id: str,
    symbol: str,
    zone_key: str,
    zone_event: str,
    touch_at: str,
    episode_id: str = "",
    zone_watch_started_at: str = "",
    marker_at: str = "",
    marker_price: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalized EMA-setup layer row for CSV / dashboard import."""
    row: dict[str, Any] = {
        **ema_setup_layer_fields(
            setup_id=setup_id,
            symbol=symbol,
            zone_key=zone_key,
            touch_at=touch_at,
            episode_id=episode_id,
            zone_watch_started_at=zone_watch_started_at,
        ),
        "zone_event": zone_event,
        "marker_at": marker_at or zone_watch_started_at or touch_at or MISSING,
        "marker_price": marker_price if marker_price is not None else MISSING,
        "candidate_direction": "NONE",
        "emit_directional_marker": False,
        "emit_setup_marker": True,
    }
    if extra:
        row.update(extra)
    return row
