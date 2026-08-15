"""State classification and fixed pattern assignments."""

from __future__ import annotations

from typing import Any

from research.regime_scanner.oi_price_delta_pattern.config import PatternConfig


def price_state(price_return: float, *, flat_abs: float) -> str:
    if price_return != price_return:
        return "price_invalid"
    if price_return >= flat_abs:
        return "price_up"
    if price_return <= -flat_abs:
        return "price_down"
    return "price_flat"


def oi_state(oi_change_pct: float, *, oi_valid: bool) -> str:
    if not oi_valid or oi_change_pct != oi_change_pct:
        return "oi_invalid"
    if oi_change_pct > 0:
        return "oi_up"
    if oi_change_pct < 0:
        return "oi_down"
    return "oi_flat"


def delta_state(delta_ratio: float, *, neutral_abs: float) -> str:
    if delta_ratio != delta_ratio:
        return "delta_invalid"
    if delta_ratio >= neutral_abs:
        return "delta_positive"
    if delta_ratio <= -neutral_abs:
        return "delta_negative"
    return "delta_neutral"


def assign_states(feat: dict[str, Any], cfg: PatternConfig) -> dict[str, Any]:
    ps = price_state(float(feat["price_return"]), flat_abs=cfg.price_flat_abs)
    os_ = oi_state(float(feat.get("oi_change_pct", float("nan"))), oi_valid=bool(feat.get("oi_valid")))
    ds = delta_state(float(feat.get("delta_ratio", float("nan"))), neutral_abs=cfg.delta_neutral_abs)
    return {**feat, "price_state": ps, "oi_state": os_, "delta_state": ds}


def patterns_for_row(row: dict[str, Any]) -> list[str]:
    """Return pattern ids that match this state row (P6 always if states valid-ish)."""
    ps, os_, ds = row["price_state"], row["oi_state"], row["delta_state"]
    out = ["P6"]
    if ps == "price_flat" and os_ == "oi_up" and ds == "delta_positive":
        out.append("P1")
    if ps == "price_flat" and os_ == "oi_up" and ds == "delta_negative":
        out.append("P2")
    if ps == "price_up" and os_ == "oi_up" and ds == "delta_positive":
        out.append("P3")
    if ps == "price_down" and os_ == "oi_up" and ds == "delta_negative":
        out.append("P4")
    if ps == "price_flat" and os_ == "oi_down":
        out.append("P5")
    return out


def assignment_rows(feature_rows: list[dict[str, Any]], cfg: PatternConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (stated feature rows, pattern assignment rows)."""
    stated: list[dict[str, Any]] = []
    assigns: list[dict[str, Any]] = []
    for feat in feature_rows:
        row = assign_states(feat, cfg)
        stated.append(row)
        for p in patterns_for_row(row):
            assigns.append(
                {
                    "symbol": row["symbol"],
                    "timestamp": row["timestamp"],
                    "lookback": row["lookback"],
                    "pattern": p,
                    "price_state": row["price_state"],
                    "oi_state": row["oi_state"],
                    "delta_state": row["delta_state"],
                    "combo": f"{row['price_state']}|{row['oi_state']}|{row['delta_state']}",
                    "anchor_i": row["anchor_i"],
                }
            )
        # also emit full combo as diagnostic pattern label
        assigns.append(
            {
                "symbol": row["symbol"],
                "timestamp": row["timestamp"],
                "lookback": row["lookback"],
                "pattern": f"COMBO::{row['price_state']}|{row['oi_state']}|{row['delta_state']}",
                "price_state": row["price_state"],
                "oi_state": row["oi_state"],
                "delta_state": row["delta_state"],
                "combo": f"{row['price_state']}|{row['oi_state']}|{row['delta_state']}",
                "anchor_i": row["anchor_i"],
            }
        )
    return stated, assigns
