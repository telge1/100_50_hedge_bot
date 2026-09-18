"""Read-only live metrics for Wall Decision V1.

Preferred sources (no new collectors / websockets):
- Wall qty: OB1000 on-demand (depth 1000 first; full depth 0 only if needed)
- Fallback major walls: research_charts.orderbook_profile (OB200/features)
- Trades: ClickHouse orderbook_analysis.public_trades_canonical (bounded zone)
- AVR / OI Δ: client-supplied from existing Footprint / OI panes (never invented)

Diagnosis (session wd1_BTCUSDT_1789377952013000000):
- Wall aktuell DATA UNAVAILABLE / WALL_LOST was set when OBP major-bar match
  missed the locked tick even though a book-ready ladder covered the price →
  correct value is 0, not unavailable.
- INCOMPLETE_PUBLIC_TRADES was forced by UI whenever trade_explained_pct was
  DATA_UNAVAILABLE, including the common case wall_reduce_pct==0 (wall grew /
  no reduction). Trades were present (count>=1); incompleteness was wrong.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RULE_VERSION

MAX_WINDOW_S = 180
PREROLL_S = 30
# Rolling window for Kontrolle buy/sell % (near real-time).
AGGRESSOR_LIVE_S = 20
WALL_PRICE_TOL_TICKS = 5
BOOK_STALE_MS = 180_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aggressor_from_trades(
    trades: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    live_s: float = AGGRESSOR_LIVE_S,
) -> dict[str, Any]:
    """Session totals for explained/pull; live shares for Kontrolle UI."""
    buy_n = sell_n = buy_qty = sell_qty = 0.0
    live_buy_n = live_sell_n = 0.0
    live_count = 0
    cutoff = None
    if now is not None and live_s and live_s > 0:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(seconds=float(live_s))
    for tr in trades:
        notion = _f(tr.get("notional")) or 0.0
        q = _f(tr.get("size")) or 0.0
        side = str(tr.get("side") or "").strip().lower()
        ts = tr.get("trade_ts")
        in_live = True
        if cutoff is not None and ts is not None:
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            in_live = ts >= cutoff
        if side == "buy":
            buy_n += notion
            buy_qty += q
            if in_live:
                live_buy_n += notion
                live_count += 1
        elif side == "sell":
            sell_n += notion
            sell_qty += q
            if in_live:
                live_sell_n += notion
                live_count += 1
    total_n = buy_n + sell_n
    live_total = live_buy_n + live_sell_n
    # Kontrolle uses the short live window; fall back to session if live empty
    # but session has tape (startup / quiet tape).
    ctrl_buy = live_buy_n
    ctrl_sell = live_sell_n
    ctrl_total = live_total
    ctrl_note = "live_window"
    if ctrl_total <= 0 and total_n > 0:
        ctrl_buy = buy_n
        ctrl_sell = sell_n
        ctrl_total = total_n
        ctrl_note = "session_fallback"
    elif ctrl_total <= 0:
        ctrl_note = "zero_trades_in_zone"
    return {
        "buy_n": buy_n,
        "sell_n": sell_n,
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "live_buy_n": live_buy_n,
        "live_sell_n": live_sell_n,
        "live_count": live_count,
        "ctrl_buy_n": ctrl_buy,
        "ctrl_sell_n": ctrl_sell,
        "ctrl_total": ctrl_total,
        "ctrl_note": ctrl_note,
        "buy_share": (ctrl_buy / ctrl_total) if ctrl_total > 0 else 0.0,
        "sell_share": (ctrl_sell / ctrl_total) if ctrl_total > 0 else 0.0,
        "session_buy_share": (buy_n / total_n) if total_n > 0 else 0.0,
        "session_sell_share": (sell_n / total_n) if total_n > 0 else 0.0,
    }

def _f(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # noqa: PLR0124


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


def _level_price_size(level: Any) -> tuple[float, float] | None:
    if isinstance(level, dict):
        p = _f(level.get("price"))
        s = _f(level.get("size") if level.get("size") is not None else level.get("qty"))
        if p is None or s is None:
            return None
        return p, s
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        p = _f(level[0])
        s = _f(level[1])
        if p is None or s is None:
            return None
        return p, s
    return None


def sum_zone_qty(levels: list[Any], lo: float, hi: float) -> float:
    """Sum size for levels with price inside [lo, hi] inclusive."""
    total = 0.0
    for level in levels or []:
        ps = _level_price_size(level)
        if ps is None:
            continue
        p, s = ps
        if lo - 1e-12 <= p <= hi + 1e-12:
            total += max(0.0, s)
    return total


def book_side_range(levels: list[Any]) -> tuple[float | None, float | None]:
    prices: list[float] = []
    for level in levels or []:
        ps = _level_price_size(level)
        if ps is not None:
            prices.append(ps[0])
    if not prices:
        return None, None
    return min(prices), max(prices)


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


def _load_trades_in_zone(
    symbol: str,
    start: datetime,
    end: datetime,
    lo: float,
    hi: float,
) -> list[dict[str, Any]]:
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


def _load_live_book_candidates(
    symbol: str,
    *,
    preferred_depth: int | None = None,
    lease_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load OB ladders with proven coverage first.

    Depth 1000 is the primary wall source (dense near mid, low latency).
    Depth 0 (full on-demand) is only a fallback — it can be sparse and falsely
    report qty=0 for ticks that still exist on the OB1000 ladder.
    """
    from research_charts.ob1000_on_demand import freshness_from_payload, load_ob1000_levels

    depths: list[int] = []
    if preferred_depth in (0, 1000):
        depths.append(int(preferred_depth))
    for d in (1000, 0):
        if d not in depths:
            depths.append(d)

    out: list[dict[str, Any]] = []
    last_err: Exception | None = None
    for depth in depths:
        try:
            payload = freshness_from_payload(
                load_ob1000_levels(symbol, depth=depth, lease_id=lease_id)
            )
            payload["_requested_depth"] = depth
            out.append(payload)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    if not out and last_err is not None:
        raise last_err
    return out


def _book_is_ready(book: dict[str, Any]) -> bool:
    status = str(book.get("data_status") or "").lower()
    if status in {"no_data", "error", ""} and not (book.get("bids") or book.get("asks")):
        return False
    fresh = str(book.get("freshness_state") or "").lower()
    if fresh == "stale":
        return False
    ms = _f(book.get("freshness_ms"))
    if ms is not None and ms > BOOK_STALE_MS:
        return False
    return bool(book.get("bids") or book.get("asks"))


def resolve_wall_current_qty(
    *,
    symbol: str,
    target_wall: dict[str, Any],
    tick: float,
    preferred_depth: int | None = None,
    lease_id: str | None = None,
    client_wall_qty: float | None = None,
) -> dict[str, Any]:
    """Return wall qty at locked zone from live book, else OBP fallback.

    If the book is ready and covers the locked price but the level is empty,
    qty is 0 (not DATA_UNAVAILABLE).
    """
    lo, hi = _zone(target_wall, tick)
    side = str(target_wall.get("side") or "").upper()
    result: dict[str, Any] = {
        "qty": None,
        "adapter": "DATA_UNAVAILABLE",
        "source": None,
        "book_ready": False,
        "wall_absent": False,
        "stale": False,
        "event_time": None,
        "coverage": {},
        "error": None,
    }
    price = _f(target_wall.get("price")) or lo

    try:
        books = _load_live_book_candidates(
            symbol, preferred_depth=preferred_depth, lease_id=lease_id
        )
        result["coverage"]["live_book_attempts"] = [
            {
                "depth": b.get("_requested_depth"),
                "data_status": b.get("data_status"),
                "freshness_state": b.get("freshness_state"),
                "freshness_ms": b.get("freshness_ms"),
                "bid_levels": len(b.get("bids") or []),
                "ask_levels": len(b.get("asks") or []),
                "timestamp_utc": b.get("timestamp_utc"),
                "source": b.get("source"),
            }
            for b in books
        ]
        chosen: dict[str, Any] | None = None
        chosen_qty: float | None = None
        chosen_absent = False
        for book in books:
            ready = _book_is_ready(book)
            if not ready:
                result["stale"] = result["stale"] or str(book.get("freshness_state") or "").lower() == "stale"
                continue
            levels = book.get("bids") if side == "BID" else book.get("asks") if side == "ASK" else []
            mn, mx = book_side_range(levels or [])
            if mn is None or mx is None:
                continue
            in_span = mn - 1e-9 <= price <= mx + 1e-9
            covers = mn - 1e-9 <= lo and hi <= mx + 1e-9
            if not (covers or in_span):
                continue
            qty = float(sum_zone_qty(list(levels or []), lo, hi))
            # Prefer a book that still shows size at the locked zone; otherwise keep
            # the first covering ladder (qty 0 = confirmed absence on that ladder).
            if chosen is None or (qty > 0 and (chosen_qty or 0) <= 0):
                chosen = book
                chosen_qty = qty
                chosen_absent = qty <= 0.0
                if qty > 0:
                    break
        if chosen is not None and chosen_qty is not None:
            result["coverage"]["live_book"] = {
                "depth": chosen.get("_requested_depth"),
                "data_status": chosen.get("data_status"),
                "freshness_state": chosen.get("freshness_state"),
                "freshness_ms": chosen.get("freshness_ms"),
                "bid_levels": len(chosen.get("bids") or []),
                "ask_levels": len(chosen.get("asks") or []),
                "timestamp_utc": chosen.get("timestamp_utc"),
                "source": chosen.get("source"),
            }
            result["event_time"] = chosen.get("timestamp_utc")
            result["book_ready"] = True
            result["qty"] = float(chosen_qty)
            result["wall_absent"] = chosen_absent
            result["adapter"] = "ok"
            result["source"] = (
                "ob_full_on_demand"
                if chosen.get("_requested_depth") == 0
                else "ob1000_on_demand"
            )
            return result
        if books:
            book = books[0]
            result["coverage"]["live_book"] = result["coverage"]["live_book_attempts"][0]
            result["event_time"] = book.get("timestamp_utc")
            result["book_ready"] = _book_is_ready(book)
            if result["stale"]:
                result["adapter"] = "STALE_DATA"
                result["error"] = "book_not_ready"
            else:
                result["adapter"] = "DATA_UNAVAILABLE"
                result["error"] = "price_outside_book_coverage"
        else:
            result["adapter"] = "DATA_UNAVAILABLE"
            result["error"] = "book_not_ready"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"live_book:{exc}"
        result["adapter"] = "DATA_GAP"

    # Client chart qty (OBP / Levels already painted) — prefer over slow/failed OBP.
    cq = _f(client_wall_qty)
    if cq is not None and cq >= 0:
        result["qty"] = float(cq)
        result["wall_absent"] = cq <= 0.0
        result["adapter"] = "ok"
        result["source"] = "client_chart_wall"
        if result.get("error"):
            result["error"] = str(result["error"]) + "|client_wall_qty_fallback"
        else:
            result["error"] = "client_wall_qty_fallback"
        return result

    # Fallback: major-wall OBP (cannot prove absence → only positive matches)
    try:
        from research_charts.orderbook_profile import load_orderbook_profile

        now = _utc_now()
        obp = load_orderbook_profile(
            symbol=symbol,
            start=now - timedelta(seconds=5),
            end=now + timedelta(seconds=1),
            at=now,
            mode="snapshot_at",
            max_bars_per_side=24,
        )
        bars = list(obp.get("bars") or [])
        result["coverage"]["orderbook_profile"] = {
            "bar_count": len(bars),
            "source": obp.get("source") or obp.get("mode"),
            "warning": obp.get("warning"),
        }
        if result["event_time"] is None:
            result["event_time"] = obp.get("as_of") or obp.get("timestamp")
        matched = _match_wall(bars, target_wall, tick)
        if matched is not None:
            qty = _f(matched.get("qty"))
            result["qty"] = qty
            result["adapter"] = "ok"
            result["source"] = "orderbook_profile_major"
            result["wall_absent"] = bool(qty is not None and qty <= 0)
            return result
        # OBP miss after live-book failure: still unavailable (major list ≠ full book)
        if result["adapter"] in {"DATA_GAP", "DATA_UNAVAILABLE", "STALE_DATA"}:
            return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = (result.get("error") or "") + f"|obp:{exc}"
        if result["adapter"] == "ok":
            pass
        else:
            result["adapter"] = "DATA_GAP"
    return result


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
    avr_state: str | None = None,
    oi_at_trigger: float | None = None,
    oi_current: float | None = None,
    preferred_depth: int | None = None,
    lease_id: str | None = None,
    client_wall_qty: float | None = None,
) -> dict[str, Any]:
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
        out["adapters"]["avr_state"] = "DATA_UNAVAILABLE" if not avr_state else "ok"
        out["adapters"]["oi_delta"] = "DATA_UNAVAILABLE"
        if avr_state:
            out["avr_state"] = str(avr_state)
        return out

    wall = resolve_wall_current_qty(
        symbol=sym,
        target_wall=target_wall,
        tick=tick,
        preferred_depth=preferred_depth,
        lease_id=str(lease_id) if lease_id else None,
        client_wall_qty=client_wall_qty,
    )
    out["coverage"].update(wall.get("coverage") or {})
    out["event_time"] = wall.get("event_time")
    out["adapters"]["wall_current"] = wall.get("adapter")
    out["adapters"]["wall_source"] = wall.get("source")
    if wall.get("error"):
        out["errors"].append(str(wall["error"]))
    if wall.get("stale"):
        out["stale"] = True
    if wall.get("adapter") == "DATA_GAP":
        out["data_gap"] = True

    qty = _f(wall.get("qty"))
    if qty is not None:
        out["wall_current_qty"] = qty
        # Absent on a ready book ⇒ decision WALL_LOST, but qty is 0 (displayable).
        if wall.get("wall_absent"):
            out["wall_lost"] = True
        base = _f(baseline_qty)
        if base is not None and base > 0:
            out["wall_reduce_pct"] = max(0.0, (base - qty) / base)
            out["adapters"]["wall_reduce_pct"] = "ok"
        else:
            out["adapters"]["wall_reduce_pct"] = "DATA_UNAVAILABLE"
        floor = _f(min_qty_seen)
        if floor is not None and floor > 0 and qty > floor:
            out["replenish_pct"] = (qty - floor) / floor
        else:
            out["replenish_pct"] = 0.0
        out["adapters"]["replenish_pct"] = "ok"
    else:
        # Unreadable book — do not invent 0
        out["adapters"]["wall_reduce_pct"] = "DATA_UNAVAILABLE"
        out["adapters"]["replenish_pct"] = "DATA_UNAVAILABLE"
        if out["stale"]:
            out["wall_lost"] = False  # STALE_DATA path preferred over WALL_LOST
        elif out["data_gap"]:
            pass
        else:
            out["wall_lost"] = True

    # --- Public trades ---
    t0 = triggered_at or (now - timedelta(seconds=PREROLL_S))
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    win_start = t0 - timedelta(seconds=PREROLL_S)
    win_end = now
    if (win_end - win_start).total_seconds() > MAX_WINDOW_S + PREROLL_S:
        win_start = win_end - timedelta(seconds=MAX_WINDOW_S + PREROLL_S)
    lo, hi = _zone(target_wall, tick)
    trades_ok = False
    buy_n = sell_n = buy_qty = sell_qty = 0.0
    try:
        trades = _load_trades_in_zone(sym, win_start, win_end, lo, hi)
        trades_ok = True
        agg = _aggressor_from_trades(trades, now=now, live_s=AGGRESSOR_LIVE_S)
        buy_n = float(agg["buy_n"])
        sell_n = float(agg["sell_n"])
        buy_qty = float(agg["buy_qty"])
        sell_qty = float(agg["sell_qty"])
        # Kontrolle / UI shares = rolling live window (fallback session).
        out["aggressor_buy_notional"] = float(agg["ctrl_buy_n"])
        out["aggressor_sell_notional"] = float(agg["ctrl_sell_n"])
        out["aggressor_buy_share"] = float(agg["buy_share"])
        out["aggressor_sell_share"] = float(agg["sell_share"])
        out["aggressor_window_s"] = float(AGGRESSOR_LIVE_S)
        out["adapters"]["aggressor"] = "ok"
        out["adapters"]["aggressor_note"] = agg["ctrl_note"]
        out["adapters"]["aggressor_session_buy_share"] = agg["session_buy_share"]
        out["adapters"]["aggressor_session_sell_share"] = agg["session_sell_share"]
        out["coverage"]["public_trades"] = {
            "count": len(trades),
            "live_count": agg["live_count"],
            "window_start": win_start.isoformat().replace("+00:00", "Z"),
            "window_end": win_end.isoformat().replace("+00:00", "Z"),
            "live_window_s": AGGRESSOR_LIVE_S,
            "zone_lo": lo,
            "zone_hi": hi,
            "query_ok": True,
        }
    except Exception as exc:  # noqa: BLE001
        out["incomplete_trades"] = True
        out["errors"].append(f"public_trades:{exc}")
        out["adapters"]["trade_explained_pct"] = "DATA_UNAVAILABLE"
        out["adapters"]["aggressor"] = "DATA_GAP"
        out["adapters"]["pull_pct"] = "DATA_UNAVAILABLE"
        out["coverage"]["public_trades"] = {"query_ok": False, "error": str(exc)}

    if trades_ok:
        wall_side = str(target_wall.get("side") or "").upper()
        attack_qty = buy_qty if wall_side == "ASK" else sell_qty if wall_side == "BID" else 0.0
        base = _f(baseline_qty)
        cur = _f(out.get("wall_current_qty"))
        reduce = _f(out.get("wall_reduce_pct"))
        if cur is None or base is None:
            out["adapters"]["trade_explained_pct"] = "DATA_UNAVAILABLE"
            out["adapters"]["pull_pct"] = "DATA_UNAVAILABLE"
            out["adapters"]["trade_explained_note"] = "missing_baseline_or_current"
        elif reduce is not None and reduce <= 0:
            # Wall did not shrink — explained/pull are 0, not incomplete.
            out["trade_explained_pct"] = 0.0
            out["pull_pct"] = 0.0
            out["adapters"]["trade_explained_pct"] = "ok"
            out["adapters"]["pull_pct"] = "ok"
            out["adapters"]["trade_explained_note"] = "no_wall_reduction"
        else:
            removed = max(0.0, (base or 0.0) - (cur or 0.0))
            if removed > 0:
                explained = min(1.0, max(0.0, attack_qty / removed))
                out["trade_explained_pct"] = explained
                out["pull_pct"] = max(0.0, float(reduce or 0.0) - explained)
                out["adapters"]["trade_explained_pct"] = "ok"
                out["adapters"]["pull_pct"] = "ok"
            else:
                out["trade_explained_pct"] = 0.0
                out["pull_pct"] = 0.0
                out["adapters"]["trade_explained_pct"] = "ok"
                out["adapters"]["pull_pct"] = "ok"

    # Price reaction
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

    # AVR / OI from client panes only
    if avr_state:
        out["avr_state"] = str(avr_state)
        out["adapters"]["avr_state"] = "ok"
    else:
        out["avr_state"] = None
        out["adapters"]["avr_state"] = "DATA_UNAVAILABLE"

    oi0 = _f(oi_at_trigger)
    oi1 = _f(oi_current)
    if oi0 is not None and oi1 is not None:
        out["oi_delta"] = oi1 - oi0
        out["adapters"]["oi_delta"] = "ok"
    else:
        out["oi_delta"] = None
        out["adapters"]["oi_delta"] = "DATA_UNAVAILABLE"

    return out


async def compute_live_metrics_async(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(compute_live_metrics, **kwargs)
