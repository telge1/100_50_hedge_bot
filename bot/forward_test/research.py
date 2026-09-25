"""Research helper for timestamp-based regime / trade location snapshots.

This is intentionally read-only research code.

Usage:
    python -m bot.forward_test.research --symbol LINKUSDT --ts 2026-09-25T12:00:00Z

It reports:
    - 1h / 4h regime
    - whether short signals are allowed
    - per-timeframe lower/upper pool context
    - suggested entry / stop / target zones
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.forward_test.paths import ensure_import_paths

ensure_import_paths()

from bot.forward_test.regime import market_regime


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    ts = datetime.fromisoformat(text)
    return _utc(ts)


def _fmt(v: float | None, digits: int = 6) -> str:
    if v is None:
        return "—"
    return f"{float(v):.{digits}f}".rstrip("0").rstrip(".")


def _pool_dict(pool: Any) -> dict[str, Any]:
    return {
        "bottom": float(pool.bottom),
        "top": float(pool.top),
        "strength": float(pool.strength),
        "pool_id": str(getattr(pool, "pool_id", "") or ""),
    }


def _pool_matches(a: dict[str, Any] | None, b: Any) -> bool:
    if a is None or b is None:
        return False
    a_id = str(a.get("pool_id") or "")
    if isinstance(b, dict):
        b_id = str(b.get("pool_id") or "")
        b_bottom = float(b.get("bottom") or 0.0)
        b_top = float(b.get("top") or 0.0)
    else:
        b_id = str(getattr(b, "pool_id", "") or "")
        b_bottom = float(getattr(b, "bottom", 0.0) or 0.0)
        b_top = float(getattr(b, "top", 0.0) or 0.0)
    if a_id and b_id:
        return a_id == b_id
    return (
        float(a.get("bottom") or 0.0) == b_bottom
        and float(a.get("top") or 0.0) == b_top
    )


def _pool_tier(pool: Any | None) -> str:
    """Classify pool size for research output."""
    if pool is None:
        return "none"
    strength = float(getattr(pool, "strength", 0.0) or 0.0)
    if strength < 2.0:
        return "micro_risky_reclaim_only"
    if strength < 4.0:
        return "normal_support"
    return "thick_support"


def _pool_ladder(
    price: float,
    pools: list[Any],
    *,
    side: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    if not pools or price != price:
        return []

    if side == "lower":
        cands = [p for p in pools if float(p.top) <= price or float(p.bottom) <= price <= float(p.top)]
        cands.sort(key=lambda p: (float(p.top), float(p.bottom), float(p.strength)), reverse=True)
    else:
        cands = [p for p in pools if float(p.bottom) >= price or float(p.bottom) <= price <= float(p.top)]
        cands.sort(key=lambda p: (float(p.bottom), float(p.top), float(p.strength)))

    out: list[dict[str, Any]] = []
    for pool in cands[:limit]:
        if side == "lower":
            dist_pct = ((price - float(pool.top)) / price * 100.0) if price > 0 else None
        else:
            dist_pct = ((float(pool.bottom) - price) / price * 100.0) if price > 0 else None
        out.append(
            {
                **_pool_dict(pool),
                "tier": _pool_tier(pool),
                "distance_pct": float(dist_pct) if dist_pct is not None else None,
            }
        )
    return out


def load_active_pools(symbol: str, as_of: datetime, *, timeframe: str, side: str) -> list[Any]:
    """Generic active pool loader for research.

    We do not restrict this to 1m / 5m because the user wants to compare
    multiple timeframes at the same timestamp.
    """
    from dashboard.research_charts.service import liquidity_location_overlay_bundle
    from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import PoolSnap

    from bot.forward_test.config import REGIME_LOOKBACK_DAYS

    as_of = _utc(as_of)
    start = int((as_of - timedelta(days=REGIME_LOOKBACK_DAYS)).timestamp())
    end = int(as_of.timestamp())
    payload = liquidity_location_overlay_bundle(
        symbol=symbol.upper(),
        timeframe=timeframe,
        start=start,
        end=end,
        allow_stale=True,
        liquidity_location_as_of=as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out: list[Any] = []
    for ov in (payload.get("liquidity") or {}).get("overlays") or []:
        md = ov.get("metadata") or {}
        if str(md.get("side") or "") != side:
            continue
        if str(md.get("pool_status") or "").upper() != "ACTIVE":
            continue
        bottom = ov.get("bottom_price")
        top = ov.get("top_price")
        if bottom is None or top is None:
            continue
        out.append(
            PoolSnap(
                bottom=float(bottom),
                top=float(top),
                strength=float(md.get("strength") or 0.0),
                pool_id=str(md.get("pool_id") or ov.get("id") or ""),
            )
        )
    if side == "lower":
        out.sort(key=lambda p: (p.top, p.bottom), reverse=True)
    else:
        out.sort(key=lambda p: (p.bottom, p.top))
    return out


def _nearest_support(price: float, pools: list[Any]) -> Any | None:
    if not pools:
        return None
    inside = [p for p in pools if float(p.bottom) <= price <= float(p.top)]
    if inside:
        return max(inside, key=lambda p: (float(p.strength), float(p.top)))
    below = [p for p in pools if float(p.top) <= price]
    if below:
        return max(below, key=lambda p: (float(p.top), float(p.strength)))
    return max(pools, key=lambda p: (float(p.bottom), float(p.strength)))


def _nearest_resistance(price: float, pools: list[Any]) -> Any | None:
    if not pools:
        return None
    inside = [p for p in pools if float(p.bottom) <= price <= float(p.top)]
    if inside:
        return min(inside, key=lambda p: (float(p.bottom), -float(p.strength)))
    above = [p for p in pools if float(p.bottom) >= price]
    if above:
        return min(above, key=lambda p: (float(p.bottom), -float(p.strength)))
    return min(pools, key=lambda p: (float(p.top), -float(p.strength)))


def _entry_stop_from_support(pool: Any) -> tuple[float, float]:
    entry = float(pool.top) * 1.002
    stop = float(pool.bottom) * 0.998
    return entry, stop


def _plan_hint(row: dict[str, Any], regime: str) -> str:
    """Human label for what price is likely doing now."""
    chosen = row.get("chosen_pool") or {}
    price = row.get("price")
    entry = row.get("entry")
    stop = row.get("stop")
    tier = row.get("pool_tier") or "none"
    if price is None or chosen is None or price != price:
        return "no_plan"

    price = float(price)
    if regime == "bullish":
        if tier == "micro_risky_reclaim_only":
            return "reclaim_only_long_watch"
        if entry is not None and price >= float(entry):
            return "long_open_or_hold_toward_target"
        watch_start = float(chosen["top"]) * 1.003
        if price >= float(chosen["top"]) and price <= watch_start:
            return "micro_pool_reclaim_long_watch"
        if stop is not None and price < float(stop):
            return "support_failed_wait"
        return "wait_for_pullback_to_micro_pool"

    if regime == "bearish":
        if entry is not None and price <= float(entry):
            return "short_open_or_hold_toward_target"
        watch_start = float(chosen["bottom"]) * 0.997
        if price <= float(chosen["bottom"]) and price >= watch_start:
            return "micro_pool_reclaim_short_watch"
        if stop is not None and price > float(stop):
            return "resistance_failed_wait"
        return "wait_for_pullback_to_micro_pool"

    return "neutral_no_trade"


def _suggestion_for_tf(
    *,
    symbol: str,
    as_of: datetime,
    timeframe: str,
    regime: str,
) -> dict[str, Any]:
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    price = None
    try:
        bars = load_5m_bars(symbol.upper(), as_of - timedelta(days=7), as_of)
        price = float(bars[-1].close) if bars else None
    except Exception:
        price = None

    if price is None:
        price = float("nan")

    side = "lower" if regime == "bullish" else "upper"
    pools = load_active_pools(symbol, as_of, timeframe=timeframe, side=side)
    opposite = load_active_pools(symbol, as_of, timeframe=timeframe, side="upper" if side == "lower" else "lower")

    chosen = None
    if side == "lower":
        chosen = _nearest_support(price, pools)
    else:
        chosen = _nearest_resistance(price, pools)

    result: dict[str, Any] = {
        "timeframe": timeframe,
        "price": price,
        "side": side,
        "active_pools": len(pools),
        "opposite_pools": len(opposite),
        "chosen_pool": _pool_dict(chosen) if chosen is not None else None,
        "pool_tier": _pool_tier(chosen),
        "pool_ladder": _pool_ladder(price, pools, side=side, limit=3),
        "entry": None,
        "stop": None,
        "target1": None,
        "target2": None,
        "plan_hint": None,
    }

    if chosen is not None:
        entry, stop = _entry_stop_from_support(chosen) if side == "lower" else (float(chosen.bottom) * 0.998, float(chosen.top) * 1.002)
        result["entry"] = entry
        result["stop"] = stop
        if regime == "bullish":
            uppers = load_active_pools(symbol, as_of, timeframe=timeframe, side="upper")
            uppers = [p for p in uppers if float(p.bottom) > entry]
            if uppers:
                uppers.sort(key=lambda p: (float(p.bottom), -float(p.strength)))
                result["target1"] = _pool_dict(uppers[0])
                if len(uppers) > 1:
                    result["target2"] = _pool_dict(uppers[1])
        elif regime == "bearish":
            lowers = load_active_pools(symbol, as_of, timeframe=timeframe, side="lower")
            lowers = [p for p in lowers if float(p.top) < entry]
            if lowers:
                lowers.sort(key=lambda p: (float(p.top), float(p.strength)), reverse=True)
                result["target1"] = _pool_dict(lowers[0])
                if len(lowers) > 1:
                    result["target2"] = _pool_dict(lowers[1])

    result["plan_hint"] = _plan_hint(result, regime)

    return result


def snapshot(symbol: str, ts: datetime, timeframes: list[str]) -> dict[str, Any]:
    reg = market_regime(symbol, now=ts)
    regime = str(reg.get("regime") or "unknown")
    direction = "long" if regime == "bullish" else "short" if regime == "bearish" else "none"
    tf_rows = [
        _suggestion_for_tf(symbol=symbol, as_of=ts, timeframe=tf, regime=regime)
        for tf in timeframes
    ]
    primary = next((r for r in tf_rows if r["timeframe"] == "1m"), tf_rows[0] if tf_rows else None)
    market_plan = primary["plan_hint"] if primary else "no_plan"
    return {
        "symbol": symbol.upper(),
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regime": reg,
        "direction": direction,
        "plan": market_plan,
        "timeframes": tf_rows,
    }


def render_md(data: dict[str, Any]) -> str:
    reg = data["regime"]
    out = [
        f"# Research Snapshot {data['symbol']}",
        "",
        f"- Timestamp: `{data['ts']}`",
        f"- Regime: `{reg['regime']}`",
        f"- Direction: `{data['direction']}`",
        f"- Plan: `{data['plan']}`",
        f"- 1h: `{reg['h1']['label']}` | ema9={_fmt(reg['h1']['ema9'])} ema20={_fmt(reg['h1']['ema20'])} ema59={_fmt(reg['h1']['ema59'])}",
        f"- 4h: `{reg['h4']['label']}` | ema9={_fmt(reg['h4']['ema9'])} ema20={_fmt(reg['h4']['ema20'])} ema59={_fmt(reg['h4']['ema59'])}",
        "",
        "| TF | Price | Tier | Pool Side | Chosen Pool | Entry | Stop | Target 1 | Target 2 | Plan |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for row in data["timeframes"]:
        chosen = row["chosen_pool"]
        target1 = row["target1"]
        target2 = row["target2"]
        out.append(
            "| {tf} | {price} | {tier} | {side} | {pool} | {entry} | {stop} | {t1} | {t2} | {plan} |".format(
                tf=row["timeframe"],
                price=_fmt(row["price"], 4),
                tier=row["pool_tier"],
                side=row["side"],
                pool=(
                    "—"
                    if chosen is None
                    else f"{_fmt(chosen['bottom'],4)}..{_fmt(chosen['top'],4)} (s={_fmt(chosen['strength'],2)})"
                ),
                entry=_fmt(row["entry"], 4),
                stop=_fmt(row["stop"], 4),
                t1=(
                    "—"
                    if target1 is None
                    else f"{_fmt(target1['bottom'],4)}..{_fmt(target1['top'],4)}"
                ),
                t2=(
                    "—"
                    if target2 is None
                    else f"{_fmt(target2['bottom'],4)}..{_fmt(target2['top'],4)}"
                ),
                plan=row["plan_hint"],
            )
        )

    out.append("")
    out.append("### Relevant Pool Ladder")
    out.append("")
    out.append("| TF | # | Pool | Tier | Dist. | Selected |")
    out.append("| --- | ---: | --- | --- | ---: | --- |")
    for row in data["timeframes"]:
        ladder = row.get("pool_ladder") or []
        chosen = row.get("chosen_pool")
        if not ladder:
            out.append(f"| {row['timeframe']} | — | — | — | — | — |")
            continue
        for idx, pool in enumerate(ladder, 1):
            selected = "yes" if _pool_matches(chosen, pool) else "no"
            dist = pool.get("distance_pct")
            out.append(
                "| {tf} | {idx} | {pool} | {tier} | {dist} | {sel} |".format(
                    tf=row["timeframe"] if idx == 1 else "",
                    idx=idx,
                    pool=f"{_fmt(pool['bottom'],4)}..{_fmt(pool['top'],4)} (s={_fmt(pool['strength'],2)})",
                    tier=pool["tier"],
                    dist=_fmt(dist, 3) if dist is not None else "—",
                    sel=selected,
                )
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Timestamp-based regime / trade research snapshot.")
    parser.add_argument("--symbol", required=True, help="Symbol, e.g. LINKUSDT")
    parser.add_argument(
        "--ts",
        help="Timestamp in UTC, e.g. 2026-09-25T12:00:00Z. Defaults to now if omitted.",
    )
    parser.add_argument(
        "--timeframes",
        default="1m,5m,15m,1h,4h",
        help="Comma-separated timeframes to inspect",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    args = parser.parse_args(argv)

    ts = _parse_ts(args.ts)
    timeframes = [tf.strip() for tf in str(args.timeframes).split(",") if tf.strip()]
    data = snapshot(args.symbol, ts, timeframes)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(render_md(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
