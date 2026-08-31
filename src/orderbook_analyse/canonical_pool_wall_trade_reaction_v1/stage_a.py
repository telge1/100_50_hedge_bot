"""Stage A — CLEAR_POOL_SELECTION_RULE_V1 candidate gate.

A1–A6 from canonical episode reactions; A7 from raw OB200 zone depth
(distributed fill inside [lower, upper]) — NOT the 1s dominant-wall proxy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.selection_rule_v1 import (
    RULE_ID,
    STAGE_A_BOOK_FILL_SOT,
    STAGE_A_MIN_P,
    STAGE_A_SYMBOL,
    STAGE_A_TIMEFRAMES,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
    strongest_inside,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    iter_ob_1s,
)

# A7 thresholds (frozen rule: ≥2 resting levels + material zone qty)
A7_MIN_ZONE_LEVELS = 2
A7_MIN_ZONE_QTY = 0.0  # any positive size; empty = fail
A7_LOOKAROUND_S = 2


def _utc(ts: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def zone_fill_from_levels(
    levels: list[tuple[float, float]],
    *,
    lower: float,
    upper: float,
) -> dict[str, Any]:
    """Distributed resting liquidity inside [lower, upper] on one side."""
    lo, hi = float(lower), float(upper)
    inside = [(p, q) for p, q in levels if lo <= p <= hi and q > 0]
    qty = sum(q for _, q in inside)
    notional = sum(p * q for p, q in inside)
    max_lvl = max(inside, key=lambda x: x[1]) if inside else None
    ranked = side_levels_ranked_full(levels)
    wall = strongest_inside(ranked, lo, hi)
    return {
        "zone_level_count": len(inside),
        "zone_qty": float(qty),
        "zone_notional": float(notional),
        "zone_max_level_price": float(max_lvl[0]) if max_lvl else None,
        "zone_max_level_qty": float(max_lvl[1]) if max_lvl else None,
        "strongest_in_zone_price": float(wall["price"]) if wall else None,
        "strongest_in_zone_notional": float(wall["notional"]) if wall else None,
        "strongest_in_zone_full_side_rank": int(wall["full_side_rank"]) if wall else None,
        "a7_pass": len(inside) >= A7_MIN_ZONE_LEVELS and qty > A7_MIN_ZONE_QTY,
    }


def measure_zone_fill_at(
    *,
    raw_root,
    side: str,
    lower: float,
    upper: float,
    at: datetime,
    lookaround_s: int = A7_LOOKAROUND_S,
) -> dict[str, Any]:
    """Raw OB200 zone fill nearest to `at` (prefer exact second)."""
    at = _utc(at)
    side = str(side).upper()
    rows = list(iter_ob_1s(raw_root, at - timedelta(seconds=lookaround_s), at + timedelta(seconds=lookaround_s)))
    out: dict[str, Any] = {
        "raw_ok": False,
        "a7_pass": False,
        "a7_fail_reason": "no_raw_book",
        "book_fill_sot": STAGE_A_BOOK_FILL_SOT,
    }
    if not rows:
        return out
    target_ms = (int(at.timestamp() * 1000) // 1000) * 1000
    best = None
    best_dist = 10**18
    for bucket, genuine, _bb, _ba, mid, bids, asks in rows:
        if not genuine:
            continue
        dist = abs(bucket - target_ms)
        if dist < best_dist:
            best_dist = dist
            best = (bids, asks, mid, bucket)
    if best is None:
        out["a7_fail_reason"] = "no_genuine_book"
        return out
    bids, asks, mid, bucket = best
    levels = asks if side == "ASK" else bids
    fill = zone_fill_from_levels(levels, lower=lower, upper=upper)
    out.update(fill)
    out["raw_ok"] = True
    out["mid"] = float(mid)
    out["book_bucket_ms"] = int(bucket)
    out["a7_fail_reason"] = None if fill["a7_pass"] else (
        "empty_or_thin_zone" if fill["zone_level_count"] < A7_MIN_ZONE_LEVELS else "zero_zone_qty"
    )
    return out


def filter_a1_a6(episode_reactions: pd.DataFrame) -> pd.DataFrame:
    """Structural + touch gate only — does NOT use 1s wall_in_pool."""
    df = episode_reactions.copy()
    if "touched" in df.columns:
        df = df[df["touched"] == True]  # noqa: E712
    # Symbol from pool_id prefix (canonical LLD ids)
    if "pool_id" in df.columns:
        df = df[df["pool_id"].astype(str).str.contains(f":{STAGE_A_SYMBOL}:", regex=False)]
    df = df[df["timeframe"].isin(list(STAGE_A_TIMEFRAMES))]
    df = df[df["maximum_P"].fillna(0).astype(int) >= STAGE_A_MIN_P]
    df = df[df["first_touch_ts"].notna()]
    return df.reset_index(drop=True)


def select_stage_a_candidates(
    episode_reactions: pd.DataFrame,
    *,
    raw_root,
    raw_start: str | None = None,
    raw_end: str | None = None,
    limit: int = 0,
    progress: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply A1–A7. Returns (pass_df, reject_df). Never uses 1s wall as A7 SoT."""
    base = filter_a1_a6(episode_reactions)
    base["touch_dt"] = pd.to_datetime(base["first_touch_ts"], utc=True)
    if raw_start:
        base = base[base["touch_dt"] >= pd.Timestamp(raw_start)]
    if raw_end:
        base = base[base["touch_dt"] <= pd.Timestamp(raw_end)]
    base = base.sort_values("first_touch_ts").reset_index(drop=True)
    if limit and limit > 0:
        base = base.head(limit)

    passes: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []

    for i, row in enumerate(base.to_dict(orient="records"), start=1):
        if progress:
            print(
                f"stage_a {i}/{len(base)} {row['pool_id']} {row['first_touch_ts']}…",
                flush=True,
            )
        fill = measure_zone_fill_at(
            raw_root=raw_root,
            side=str(row["side"]),
            lower=float(row["lower"]),
            upper=float(row["upper"]),
            at=_utc(row["first_touch_ts"]),
        )
        rec = {
            **row,
            "rule_id": RULE_ID,
            "stage_a_a1_a6": True,
            "wall_in_pool_1s_proxy": row.get("wall_in_pool"),  # diagnostic only — not A7 SoT
            "a7_pass": bool(fill.get("a7_pass")),
            "a7_raw_ok": bool(fill.get("raw_ok")),
            "a7_fail_reason": fill.get("a7_fail_reason"),
            "a7_zone_level_count": fill.get("zone_level_count"),
            "a7_zone_qty": fill.get("zone_qty"),
            "a7_zone_notional": fill.get("zone_notional"),
            "a7_zone_max_level_price": fill.get("zone_max_level_price"),
            "a7_zone_max_level_qty": fill.get("zone_max_level_qty"),
            "a7_strongest_in_zone_price": fill.get("strongest_in_zone_price"),
            "a7_strongest_in_zone_notional": fill.get("strongest_in_zone_notional"),
            "a7_strongest_in_zone_full_side_rank": fill.get("strongest_in_zone_full_side_rank"),
            "a7_book_bucket_ms": fill.get("book_bucket_ms"),
            "a7_mid": fill.get("mid"),
            "book_fill_sot": STAGE_A_BOOK_FILL_SOT,
        }

        if rec["a7_pass"]:
            passes.append(rec)
        else:
            rejects.append(rec)

    return pd.DataFrame(passes), pd.DataFrame(rejects)
