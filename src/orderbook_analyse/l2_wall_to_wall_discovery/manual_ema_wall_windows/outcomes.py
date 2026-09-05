"""Directional MFE/MAE outcomes from classification time."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING

HORIZONS_S = (60, 180, 300, 600, 900, 1800)


def expected_direction(primary_class: str, zone_role: str, trend: str) -> str:
    """LONG / SHORT / NO_TRADE — causal, no cost/TP optimization."""
    if primary_class in ("DATA_INCOMPLETE", "UNDETERMINED", "NO_RELEVANT_ZONE_CONTACT"):
        return "NO_TRADE"
    if primary_class == "DEFENSE_REJECTION":
        if zone_role == "resistance":
            return "SHORT"
        if zone_role == "support":
            return "LONG"
        if trend == "BEARISH":
            return "SHORT"
        if trend == "BULLISH":
            return "LONG"
        return "NO_TRADE"
    if primary_class in (
        "ABSORPTION_THEN_BREAKOUT",
        "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
        "LIQUIDITY_PULL_BREAKOUT",
    ):
        if zone_role == "resistance":
            return "LONG"
        if zone_role == "support":
            return "SHORT"
        return "NO_TRADE"
    if primary_class == "FALSE_BREAKOUT_RECLAIM":
        if zone_role == "resistance":
            return "SHORT"
        if zone_role == "support":
            return "LONG"
        return "NO_TRADE"
    if primary_class == "RANGE_AROUND_ZONE":
        return "NO_TRADE"
    return "NO_TRADE"


def side_mfe_mae(
    path: list[tuple[int, float]],
    *,
    entry_ts_ms: int,
    entry_px: float,
    direction: str,
    horizon_s: int,
) -> dict[str, Any]:
    end = entry_ts_ms + horizon_s * 1000
    pts = [(t, p) for t, p in path if entry_ts_ms <= t <= end]
    if not pts or entry_px <= 0 or direction == "NO_TRADE":
        return {
            "mfe_pct": MISSING,
            "mae_pct": MISSING,
            "endpoint_pct": MISSING,
            "t_mfe_s": MISSING,
            "t_mae_s": MISSING,
        }
    mfe = 0.0
    mae = 0.0
    t_mfe = 0
    t_mae = 0
    for t, p in pts:
        if direction == "LONG":
            fav = (p / entry_px - 1.0) * 100.0
            adv = (entry_px / p - 1.0) * 100.0 if p > 0 else 0.0
            # MAE for long: adverse is down move
            adv = (entry_px - p) / entry_px * 100.0
        else:  # SHORT
            fav = (entry_px - p) / entry_px * 100.0
            adv = (p - entry_px) / entry_px * 100.0
        if fav > mfe:
            mfe = fav
            t_mfe = t - entry_ts_ms
        if adv > mae:
            mae = adv
            t_mae = t - entry_ts_ms
    last = pts[-1][1]
    if direction == "LONG":
        ep = (last / entry_px - 1.0) * 100.0
    else:
        ep = (entry_px - last) / entry_px * 100.0
    return {
        "mfe_pct": mfe,
        "mae_pct": mae,
        "endpoint_pct": ep,
        "t_mfe_s": t_mfe / 1000.0,
        "t_mae_s": t_mae / 1000.0,
    }


def outcome_rows(
    *,
    window_id: str,
    primary_class: str,
    zone_role: str,
    trend: str,
    classification_ts_ms: int | None,
    entry_px: float | None,
    path: list[tuple[int, float]],
    next_zone_hit: str | None,
    next_zone_ts_ms: int | None,
    breakout_held: str,
) -> list[dict[str, Any]]:
    direction = expected_direction(primary_class, zone_role, trend)
    rows: list[dict[str, Any]] = []
    for h in HORIZONS_S:
        base = {
            "window_id": window_id,
            "direction": direction,
            "horizon_s": h,
            "classification_ts_ms": classification_ts_ms if classification_ts_ms is not None else MISSING,
            "entry_px": entry_px if entry_px is not None else MISSING,
            "next_zone_hit": next_zone_hit if next_zone_hit else MISSING,
            "time_to_next_zone_s": (
                (next_zone_ts_ms - classification_ts_ms) / 1000.0
                if next_zone_ts_ms is not None and classification_ts_ms is not None
                else MISSING
            ),
            "breakout_held": breakout_held,
            "mfe_ge_0_15pct": MISSING,
        }
        if classification_ts_ms is None or entry_px is None or direction == "NO_TRADE":
            base.update(
                {
                    "mfe_pct": MISSING,
                    "mae_pct": MISSING,
                    "endpoint_pct": MISSING,
                    "t_mfe_s": MISSING,
                    "t_mae_s": MISSING,
                }
            )
        else:
            m = side_mfe_mae(
                path,
                entry_ts_ms=classification_ts_ms,
                entry_px=entry_px,
                direction=direction,
                horizon_s=h,
            )
            base.update(m)
            if m["mfe_pct"] != MISSING:
                base["mfe_ge_0_15pct"] = bool(float(m["mfe_pct"]) >= 0.15)
        rows.append(base)
    return rows
