"""Flow strength, price reaction, and absorption pattern assignment."""

from __future__ import annotations

from typing import Any

from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig, FLOW_RULES


def oi_state(oi_change_pct: float, *, oi_valid: bool) -> str:
    if not oi_valid or oi_change_pct != oi_change_pct:
        return "oi_invalid"
    if oi_change_pct > 0:
        return "oi_up"
    if oi_change_pct < 0:
        return "oi_down"
    return "oi_flat"


def close_location_class(close_loc: float, *, flow_positive: bool) -> str:
    if close_loc != close_loc:
        return "close_invalid"
    if flow_positive:
        if close_loc <= 0.35:
            return "weak_strong"
        if close_loc <= 0.50:
            return "weak"
        return "not_weak"
    if close_loc >= 0.65:
        return "strong_strong"
    if close_loc >= 0.50:
        return "strong"
    return "not_strong"


def flow_active(delta_ratio: float, flow_rule: str, feat: dict[str, Any], cfg: AbsorptionConfig) -> tuple[bool, bool]:
    """Return (strong_positive, strong_negative) under flow_rule."""
    if delta_ratio != delta_ratio:
        return False, False
    if flow_rule == "F1":
        return delta_ratio >= cfg.f1_abs, delta_ratio <= -cfg.f1_abs
    if flow_rule == "F2":
        return delta_ratio >= cfg.f2_abs, delta_ratio <= -cfg.f2_abs
    if flow_rule == "F3":
        p90 = feat.get("abs_delta_ratio_p90_prior")
        if p90 is None or p90 != p90 or p90 <= 0:
            return False, False
        strong = abs(delta_ratio) >= float(p90)
        return (strong and delta_ratio > 0), (strong and delta_ratio < 0)
    return False, False


def price_reaction_for_positive_flow(price_return: float, cfg: AbsorptionConfig) -> str:
    if price_return != price_return:
        return "invalid"
    if price_return >= cfg.normal_progress_abs:
        return "normal_progress"
    if price_return <= 0:
        return "counter"
    if price_return < cfg.weak_progress_abs:
        return "weak_progress"
    return "mid"  # between weak and normal (0.10% .. 0.25%)


def price_reaction_for_negative_flow(price_return: float, cfg: AbsorptionConfig) -> str:
    if price_return != price_return:
        return "invalid"
    if price_return <= -cfg.normal_progress_abs:
        return "normal_progress"
    if price_return >= 0:
        return "counter"
    if price_return > -cfg.weak_progress_abs:
        return "weak_progress"
    return "mid"


def patterns_for_flow(
    *,
    flow_rule: str,
    pos_flow: bool,
    neg_flow: bool,
    price_return: float,
    close_loc: float,
    cfg: AbsorptionConfig,
) -> list[tuple[str, str, str, str]]:
    """Return list of (pattern, flow_direction, price_reaction, close_location_class)."""
    out: list[tuple[str, str, str, str]] = []
    if pos_flow:
        pr = price_reaction_for_positive_flow(price_return, cfg)
        cl = close_location_class(close_loc, flow_positive=True)
        out.append(("C3", "positive", pr, cl))
        if pr == "normal_progress":
            out.append(("C1", "positive", pr, cl))
        if pr == "weak_progress":
            out.append(("A1", "positive", pr, cl))
        # A2: counter OR weak close location
        if pr == "counter" or cl in ("weak", "weak_strong"):
            out.append(("A2", "positive", pr, cl))
    if neg_flow:
        pr = price_reaction_for_negative_flow(price_return, cfg)
        cl = close_location_class(close_loc, flow_positive=False)
        out.append(("C4", "negative", pr, cl))
        if pr == "normal_progress":
            out.append(("C2", "negative", pr, cl))
        if pr == "weak_progress":
            out.append(("A3", "negative", pr, cl))
        if pr == "counter" or cl in ("strong", "strong_strong"):
            out.append(("A4", "negative", pr, cl))
    return out


def assignment_rows(feature_rows: list[dict[str, Any]], cfg: AbsorptionConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stated: list[dict[str, Any]] = []
    assigns: list[dict[str, Any]] = []
    for feat in feature_rows:
        os_ = oi_state(float(feat.get("oi_change_pct", float("nan"))), oi_valid=bool(feat.get("oi_valid")))
        row = {**feat, "oi_state": os_}
        stated.append(row)
        # C5 once per anchor
        assigns.append(
            {
                "symbol": row["symbol"],
                "timestamp": row["timestamp"],
                "lookback": row["lookback"],
                "flow_rule": "ALL",
                "pattern": "C5",
                "flow_direction": "any",
                "price_reaction": "any",
                "close_location_class": "any",
                "oi_state": os_,
                "anchor_i": row["anchor_i"],
                "expected_side": "neutral",
            }
        )
        dr = float(row["delta_ratio"])
        cl = float(row["close_location"])
        pr_ret = float(row["price_return"])
        for fr in FLOW_RULES:
            pos, neg = flow_active(dr, fr, row, cfg)
            for pattern, flow_dir, price_rx, cl_cls in patterns_for_flow(
                flow_rule=fr,
                pos_flow=pos,
                neg_flow=neg,
                price_return=pr_ret,
                close_loc=cl,
                cfg=cfg,
            ):
                if pattern in ("A1", "A2", "C1", "C3"):
                    side = "bearish"
                elif pattern in ("A3", "A4", "C2", "C4"):
                    side = "bullish"
                else:
                    side = "neutral"
                assigns.append(
                    {
                        "symbol": row["symbol"],
                        "timestamp": row["timestamp"],
                        "lookback": row["lookback"],
                        "flow_rule": fr,
                        "pattern": pattern,
                        "flow_direction": flow_dir,
                        "price_reaction": price_rx,
                        "close_location_class": cl_cls,
                        "oi_state": os_,
                        "anchor_i": row["anchor_i"],
                        "expected_side": side,
                    }
                )
    return stated, assigns
