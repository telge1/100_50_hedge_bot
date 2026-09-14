"""Read-only live metrics for Wall Decision V1.

Reuses research_charts orderbook_profile + trade_bubbles loaders.
No new WebSockets, collectors, or private APIs. No order execution.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RULE_VERSION, V1_PROVISIONAL

# Max lookback after trigger for metrics (seconds). Keep small — no full archives.
MAX_WINDOW_S = 180
PREROLL_S = 30
STALE_WALL_S = 8.0
WALL_PRICE_TOL_TICKS = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # noqa: PLR0124 — NaN check


def _match_wall(bars: list[dict[str, Any]], target: dict[str, Any], tick: float) -> dict[str, Any] | None:
    side = str(target.get("side") or "").upper()
    price = _f(target.get("price"))
    if not side or price is None:
        return None
    tol = max(tick * WALL_PRICE_TOL_TICKS, tick)
    best = None
    best_d = None
    for b in bars or []:
        if str(b.get("side") or "").upper() != side:
            continue
        bp = _f(b.get("price"))
        if bp is None:
            continue
        d = abs(bp - price)
        if d > tol:
            continue
        if best_d is None or d < best_d:
            best = b
            best_d = d
    return best


def _zone(target: dict[str, Any], tick: float) -> tuple[float, float]:
    lo = _f(target.get("zone_lo"))
    hi = _f(target.get("zone_hi"))
    price = _f(target.get("price")) or 0.0
    band = max(tick * WALL_PRICE_TOL_TICKS, tick)
    if lo is None:
        lo = price - band
    if hi is None:
        hi = price + band
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _load_trades_in_zone(
    symbol: str,
    start: datetime,
    end: datetime,
    lo: float,
    hi: float,
) -> list[dict[str, Any]]:
    """Bounded zone query — avoids full-range trade_bubbles memory pressure."""
    import clickhouse_connect
    from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.trade_bubbles import CANONICAL_FQN

    client = clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())
    settings = {
        "max_execution_time": 8,
        "max_memory_usage": 80_000_000,
        "max_threads": 1,
    }
    try:
        rows = client.query(
            f"""
            SELECT
              trade_ts, trade_id, side,
              toFloat64(price) AS price,
              toFloat64(size) AS size,
              toFloat64(notional) AS notional
            FROM {CANONICAL_FQN}
            PREWHERE symbol = {{s:String}}
            WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
              AND trade_ts < {{b:DateTime64(3,'UTC')}}
              AND price >= {{lo:Float64}}
              AND price <= {{hi:Float64}}
            ORDER BY trade_ts, trade_id
            LIMIT 5000
            """,
            parameters={"s": symbol, "a": start, "b": end, "lo": lo, "hi": hi},
            settings=settings,
        ).result_rows
    except (DatabaseError, OperationalError) as exc:
        raise RuntimeError(f"query_failed:{exc}") from exc
    out: list[dict[str, Any]] = []
    for ts, tid, side, price, size, notional in rows:
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(
            {
                "trade_id": str(tid),
                "trade_ts": ts,
                "side": str(side),
                "price": float(price),
                "size": float(size),
                "notional": float(notional if notional is not None else price * size),
            }
        )
    return out


def compute_live_metrics(
    *,
    symbol: str,
    breakpoint: float,
    target_wall: dict[str, Any] | None,
    baseline_qty: float | None,
    baseline_notional: float | None,
    triggered_at: datetime | None,
    trigger_price: float | None,
    live_price: float | None,
    accepted_above_sec: float = 0.0,
    accepted_below_sec: float = 0.0,
    min_qty_seen: float | None = None,
) -> dict[str, Any]:
    """Synchronous metrics assembly using existing CH/archive loaders."""
    from research_charts.orderbook_profile import load_orderbook_profile
    from research_charts.trade_bubbles import tick_size

    sym = str(symbol or "").strip().upper()
    now = _utc_now()
    available_at = now.isoformat().replace("+00:00", "Z")
    tick = float(tick_size(sym))
    out: dict[str, Any] = {
        "success": True,
        "rule_version": RULE_VERSION,
        "thresholds_mark": "V1_PROVISIONAL",
        "symbol": sym,
        "breakpoint": breakpoint,
        "event_time": None,
        "available_at": available_at,
        "data_gap": False,
        "stale": False,
        "wall_lost": False,
        "incomplete_trades": False,
        "epoch_boundary": False,
        "wall_side": str((target_wall or {}).get("side") or "").upper() or None,
        "wall_baseline_qty": _f(baseline_qty),
        "wall_current_qty": None,
        "wall_reduce_pct": None,
        "trade_explained_pct": None,
        "pull_pct": None,
        "replenish_pct": None,
        "aggressor_buy_notional": None,
        "aggressor_sell_notional": None,
        "aggressor_buy_share": None,
        "aggressor_sell_share": None,
        "price_response_bps": None,
        "speed_bps_s": None,
        "efficiency_bps_per_mio": None,
        "avr_state": None,
        "oi_delta": None,
        "accepted_above_sec": float(accepted_above_sec or 0),
        "accepted_below_sec": float(accepted_below_sec or 0),
        "coverage": {},
        "adapters": {},
        "errors": [],
    }

    if not target_wall:
        out["wall_lost"] = True
        out["adapters"]["target_wall"] = "DATA_UNAVAILABLE"
        out["adapters"]["avr_state"] = "DATA_UNAVAILABLE"
        out["adapters"]["oi_delta"] = "DATA_UNAVAILABLE"
        return out

    # --- Wall snapshot (OBP) ---
    end = now + timedelta(seconds=1)
    start = now - timedelta(seconds=5)
    try:
        obp = load_orderbook_profile(
            symbol=sym,
            start=start,
            end=end,
            at=now,
            mode="snapshot_at",
            max_bars_per_side=12,
        )
        bars = list(obp.get("bars") or [])
        as_of = obp.get("as_of") or obp.get("timestamp")
        out["event_time"] = as_of
        out["coverage"]["orderbook_profile"] = {
            "bar_count": len(bars),
            "source": obp.get("source") or obp.get("mode"),
            "warning": obp.get("warning"),
        }
        matched = _match_wall(bars, target_wall, tick)
        if matched is None:
            out["wall_lost"] = True
            out["adapters"]["wall_current"] = "WALL_LOST"
        else:
            qty = _f(matched.get("qty"))
            out["wall_current_qty"] = qty
            out["adapters"]["wall_current"] = "ok"
            if matched.get("carried_forward") and float(matched.get("samples") or 0) <= 1:
                out["stale"] = True
            base = _f(baseline_qty)
            if base is not None and base > 0 and qty is not None:
                reduce = max(0.0, (base - qty) / base)
                out["wall_reduce_pct"] = reduce
                out["adapters"]["wall_reduce_pct"] = "ok"
            else:
                out["adapters"]["wall_reduce_pct"] = "DATA_UNAVAILABLE"
            floor = _f(min_qty_seen)
            if floor is not None and qty is not None and floor > 0 and qty > floor:
                out["replenish_pct"] = (qty - floor) / floor
                out["adapters"]["replenish_pct"] = "ok"
            else:
                out["replenish_pct"] = 0.0 if qty is not None else None
                out["adapters"]["replenish_pct"] = "ok" if qty is not None else "DATA_UNAVAILABLE"
    except Exception as exc:  # noqa: BLE001 — isolate analysis failures
        out["data_gap"] = True
        out["errors"].append(f"orderbook_profile:{exc}")
        out["adapters"]["wall_current"] = "DATA_GAP"

    # --- Public trades in wall zone (bounded window) ---
    t0 = triggered_at or (now - timedelta(seconds=PREROLL_S))
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    win_start = t0 - timedelta(seconds=PREROLL_S)
    win_end = now
    if (win_end - win_start).total_seconds() > MAX_WINDOW_S + PREROLL_S:
        win_start = win_end - timedelta(seconds=MAX_WINDOW_S + PREROLL_S)
    lo, hi = _zone(target_wall, tick)
    try:
        trades = _load_trades_in_zone(sym, win_start, win_end, lo, hi)
        buy_n = 0.0
        sell_n = 0.0
        buy_qty = 0.0
        sell_qty = 0.0
        for tr in trades:
            px = _f(tr.get("price"))
            if px is None or px < lo or px > hi:
                continue
            notion = _f(tr.get("notional")) or 0.0
            qty = _f(tr.get("size")) or 0.0
            side = str(tr.get("side") or "").strip()
            if side.lower() == "buy":
                buy_n += notion
                buy_qty += qty
            elif side.lower() == "sell":
                sell_n += notion
                sell_qty += qty
        total_n = buy_n + sell_n
        out["aggressor_buy_notional"] = buy_n
        out["aggressor_sell_notional"] = sell_n
        if total_n > 0:
            out["aggressor_buy_share"] = buy_n / total_n
            out["aggressor_sell_share"] = sell_n / total_n
            out["adapters"]["aggressor"] = "ok"
        else:
            out["adapters"]["aggressor"] = "DATA_UNAVAILABLE"
        out["coverage"]["public_trades"] = {
            "count": len(trades),
            "window_start": win_start.isoformat().replace("+00:00", "Z"),
            "window_end": win_end.isoformat().replace("+00:00", "Z"),
            "zone_lo": lo,
            "zone_hi": hi,
        }
        wall_side = str(target_wall.get("side") or "").upper()
        attack_qty = buy_qty if wall_side == "ASK" else sell_qty if wall_side == "BID" else 0.0
        base = _f(baseline_qty)
        cur = _f(out.get("wall_current_qty"))
        if base is not None and cur is not None and base > cur:
            removed = base - cur
            if removed > 0:
                explained = min(1.0, max(0.0, attack_qty / removed))
                out["trade_explained_pct"] = explained
                reduce = out.get("wall_reduce_pct")
                if reduce is not None:
                    out["pull_pct"] = max(0.0, float(reduce) - explained)
                out["adapters"]["trade_explained_pct"] = "ok"
                out["adapters"]["pull_pct"] = "ok"
            else:
                out["adapters"]["trade_explained_pct"] = "DATA_UNAVAILABLE"
                out["incomplete_trades"] = True
        else:
            if not trades:
                out["incomplete_trades"] = True
            out["adapters"]["trade_explained_pct"] = "DATA_UNAVAILABLE"
            out["adapters"]["pull_pct"] = "DATA_UNAVAILABLE"
    except Exception as exc:  # noqa: BLE001
        out["incomplete_trades"] = True
        out["errors"].append(f"public_trades:{exc}")
        out["adapters"]["trade_explained_pct"] = "DATA_UNAVAILABLE"
        out["adapters"]["aggressor"] = "DATA_GAP"

    tp = _f(trigger_price)
    lp = _f(live_price)
    if tp is not None and lp is not None and tp > 0:
        bps = ((lp - tp) / tp) * 10000.0
        out["price_response_bps"] = bps
        out["adapters"]["price_response_bps"] = "ok"
        elapsed = max(0.001, (now - t0).total_seconds()) if triggered_at else None
        if elapsed:
            out["speed_bps_s"] = bps / elapsed
            out["adapters"]["speed_bps_s"] = "ok"
        attack_n = out.get("aggressor_buy_notional") if out.get("wall_side") == "ASK" else out.get("aggressor_sell_notional")
        if attack_n and attack_n > 0:
            out["efficiency_bps_per_mio"] = bps / (attack_n / 1_000_000.0)
            out["adapters"]["efficiency_bps_per_mio"] = "ok"
        else:
            out["adapters"]["efficiency_bps_per_mio"] = "DATA_UNAVAILABLE"
    else:
        out["adapters"]["price_response_bps"] = "DATA_UNAVAILABLE"
        out["adapters"]["speed_bps_s"] = "DATA_UNAVAILABLE"
        out["adapters"]["efficiency_bps_per_mio"] = "DATA_UNAVAILABLE"

    out["avr_state"] = None
    out["oi_delta"] = None
    out["adapters"]["avr_state"] = "DATA_UNAVAILABLE"
    out["adapters"]["oi_delta"] = "DATA_UNAVAILABLE"
    out["adapters"]["note"] = (
        "AVR/OI require frontend context from existing footprint/OI panes; "
        "not synthesized server-side."
    )
    return out


async def compute_live_metrics_async(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(compute_live_metrics, **kwargs)
