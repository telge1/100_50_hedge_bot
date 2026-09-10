"""Cautious execute/cancel attribution for book removals."""

from __future__ import annotations

from typing import Any

import pandas as pd


def attribute_removals(
    level_changes: list[dict[str, Any]],
    trades: list[Any],
    *,
    tolerance_ms: int,
) -> list[dict[str, Any]]:
    """Match removals to trades without reusing trade notional."""
    removals = [e for e in level_changes if float(e.get("size_delta") or 0) < 0]
    # remaining trade notional pool
    trade_pool: list[dict[str, Any]] = []
    for t in trades:
        # TradeRow or dict
        if hasattr(t, "trade_ts"):
            trade_pool.append(
                {
                    "trade_ts": pd.to_datetime(t.trade_ts, utc=True),
                    "side": t.side,
                    "price": float(t.price),
                    "notional": float(t.notional),
                    "remaining": float(t.notional),
                    "trade_id": str(t.trade_id),
                }
            )
        else:
            trade_pool.append(
                {
                    "trade_ts": pd.to_datetime(t["event_time"], utc=True),
                    "side": t.get("side") or t.get("trade_side"),
                    "price": float(t["price"]),
                    "notional": float(t.get("notional_delta") or 0),
                    "remaining": float(t.get("notional_delta") or 0),
                    "trade_id": str(t.get("source_event_id")),
                }
            )

    tol = pd.Timedelta(milliseconds=tolerance_ms)
    out: list[dict[str, Any]] = []

    for rem in sorted(removals, key=lambda e: pd.to_datetime(e["event_time"], utc=True)):
        rt = pd.to_datetime(rem["event_time"], utc=True)
        side = str(rem["side"])
        px = float(rem["price"])
        removed_notional = abs(float(rem["notional_delta"]))
        matched = 0.0
        reasons: list[str] = []
        used_ids: list[str] = []

        for tr in trade_pool:
            if tr["remaining"] <= 1e-9:
                continue
            if abs(tr["trade_ts"] - rt) > tol:
                continue
            # Buy consumes ask; Sell consumes bid
            ok_side = (side == "ask" and str(tr["side"]).lower() == "buy") or (
                side == "bid" and str(tr["side"]).lower() == "sell"
            )
            if not ok_side:
                continue
            # price compatibility
            if side == "ask" and tr["price"] + 1e-12 < px:
                continue
            if side == "bid" and tr["price"] - 1e-12 > px:
                continue
            take = min(tr["remaining"], removed_notional - matched)
            if take <= 0:
                continue
            tr["remaining"] -= take
            matched += take
            used_ids.append(tr["trade_id"])
            reasons.append(f"match:{tr['trade_id']}:{take:.4f}")
            if matched >= removed_notional - 1e-6:
                break

        unmatched = max(0.0, removed_notional - matched)
        ratio = matched / removed_notional if removed_notional > 0 else 0.0
        if ratio >= 0.8:
            cls, conf = "EXECUTED_LIKELY", "HIGH"
        elif ratio >= 0.2:
            cls, conf = "MIXED_OR_UNKNOWN", "MEDIUM"
        elif matched <= 1e-9:
            cls, conf = "CANCEL_LIKELY", "MEDIUM"
        else:
            cls, conf = "MIXED_OR_UNKNOWN", "LOW"

        out.append(
            {
                "event_time": rt,
                "side": side,
                "price": px,
                "removed_notional": removed_notional,
                "matched_trade_notional": matched,
                "unmatched_removed_notional": unmatched,
                "attribution_class": cls,
                "attribution_confidence": conf,
                "matching_reason": ";".join(reasons) if reasons else "no_trade_overlap",
                "trade_ids": used_ids,
                "source_event_id": rem.get("source_event_id"),
            }
        )
    return out


def attribution_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_cls: dict[str, dict[str, float]] = {}
    for r in rows:
        c = r["attribution_class"]
        by_cls.setdefault(c, {"count": 0, "notional": 0.0})
        by_cls[c]["count"] += 1
        by_cls[c]["notional"] += float(r["removed_notional"])
    return by_cls
