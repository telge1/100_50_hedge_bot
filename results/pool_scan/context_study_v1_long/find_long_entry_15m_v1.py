"""15m long entry baseline V1 — mirrored from find_short_entry_15m_v1 (research only)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]

# Same constants as short baseline (not optimized).
MIN_GAP_PCT = 0.8
MIN_BRIDGE_GAP_PCT = 0.5
MIN_TP_PCT = 0.8
STOP_PAD = 0.002
ATTACH_PCT = 1.0
HISTORY_WEEKS = 12
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def last_closed_index(bars: list[dict], moment: datetime) -> int | None:
    chosen = None
    for index, bar in enumerate(bars):
        if bar["close_time"] <= moment:
            chosen = index
        elif chosen is not None:
            break
    return chosen


def up_4h(bars: list[dict], index: int | None) -> bool:
    """Mirror of ``down_4h`` (find_short_entry_15m_v1)."""
    if index is None or index < 3:
        return False
    bar = bars[index]
    if None in (bar["ema9"], bar["ema20"], bar["ema59"], bar["ema200"]):
        return False
    depth = (bar["ema59"] - bar["ema200"]) / bar["ema200"] * 100.0
    return (
        bar["close"] > bar["ema20"] > bar["ema200"]
        and bar["ema59"] > bar["ema200"]
        and bar["ema9"] > bar["ema200"]
        and depth >= 2.0
    )


def attached_lower(pool: dict, bar: dict) -> bool:
    """Mirror of ``attached()`` for lower pools (reject from above)."""
    if bar["close"] <= pool["top"]:
        return False
    if pool["top"] <= 0:
        return False
    if min(bar["open"], bar["close"]) <= pool["top"]:
        return False
    if bar["low"] - 1e-9 > pool["top"]:
        return False
    distance = (bar["close"] - pool["top"]) / pool["top"] * 100.0
    return distance <= ATTACH_PCT


def open_pools(
    pools: list[dict],
    side: str,
    moment: datetime,
    *,
    max_top: float | None = None,
) -> list[dict]:
    rows = [
        pool
        for pool in pools
        if pool["side"] == side
        and pool["known"] <= moment
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if max_top is not None and side == "lower":
        rows = [pool for pool in rows if pool["top"] < max_top]
    return rows


def next_lower_bridge(pools: list[dict], current: dict, moment: datetime) -> dict | None:
    """Mirror of ``next_upper()`` — next lower pool below entry pool with bridge gap."""
    choices = [
        pool
        for pool in pools
        if pool["side"] == "lower"
        and pool["pool_id"] != current["pool_id"]
        and pool["known"] <= moment
        and pool["top"] < current["bottom"]
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (-pool["top"], pool["known"]))
    for pool in choices:
        bridge_gap = (current["bottom"] - pool["top"]) / current["bottom"] * 100.0
        if bridge_gap >= MIN_BRIDGE_GAP_PCT:
            return pool
    return None


def next_upper_tp(pools: list[dict], close: float, moment: datetime, min_gap_pct: float) -> dict | None:
    """Mirror of ``next_lower()`` — first upper target above entry."""
    choices = [
        pool
        for pool in pools
        if pool["side"] == "upper"
        and pool["known"] <= moment
        and pool["bottom"] > close
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (pool["bottom"], pool["known"]))
    for pool in choices:
        gap_pct = (pool["bottom"] - close) / close * 100.0
        if gap_pct >= min_gap_pct:
            return pool
    return None


def gap_pct_bridge(current: dict, nxt: dict | None) -> float | None:
    if nxt is None or current["bottom"] <= 0:
        return None
    return (current["bottom"] - nxt["top"]) / current["bottom"] * 100.0


def entry_setup_long(pools: list[dict], bar: dict, moment: datetime) -> tuple[dict, dict, dict, float] | None:
    touched = [
        pool
        for pool in open_pools(pools, "lower", moment, max_top=bar["close"])
        if bar["close"] < bar["open"] and attached_lower(pool, bar)
    ]
    if not touched:
        return None
    # Highest top among touched lowers = closest support under price (mirror: min bottom on uppers).
    pool = max(touched, key=lambda item: (item["top"], item["known"]))
    bridge_lower = next_lower_bridge(pools, pool, moment)
    gap = gap_pct_bridge(pool, bridge_lower)
    if bridge_lower is None or gap is None or gap < MIN_GAP_PCT:
        return None
    upper = next_upper_tp(pools, bar["close"], moment, MIN_TP_PCT)
    if upper is None:
        return None
    return pool, bridge_lower, upper, gap


def row_for_long(
    symbol: str,
    pool: dict,
    birth: dict,
    entry: dict,
    h4: dict,
    bridge_lower: dict,
    upper: dict,
    gap: float,
) -> dict:
    tp_gap = ""
    if upper is not None and entry["close"] > 0:
        tp_gap = round((upper["bottom"] - entry["close"]) / entry["close"] * 100.0, 4)
    return {
        "symbol": symbol,
        "mode": "long_baseline_v1",
        "entry_open": entry["open_time"].isoformat(),
        "pool_open": birth["open_time"].isoformat(),
        "known_at": pool["known"].isoformat(),
        "pool_id": pool["pool_id"],
        "bottom_price": pool["bottom"],
        "top_price": pool["top"],
        "stop_price": round(pool["bottom"] * (1.0 - STOP_PAD), 8),
        "close": entry["close"],
        "bridge_lower_id": bridge_lower["pool_id"],
        "bridge_lower_top": bridge_lower["top"],
        "gap_pct": round(gap, 4),
        "tp_pool_id": upper["pool_id"],
        "tp_bottom": upper["bottom"],
        "tp_gap_pct": tp_gap,
        "trend_4h": "up",
        "ema9_4h": round(h4["ema9"], 8),
        "ema20_4h": round(h4["ema20"], 8),
        "ema59_4h": round(h4["ema59"], 8),
        "ema200_4h": round(h4["ema200"], 8),
    }


def collect_long_signals(
    symbol: str,
    pane_from: datetime,
    pane_to: datetime,
    report_from: datetime,
    report_to: datetime,
    *,
    require_up_4h: bool = True,
) -> tuple[list[dict], list[dict]]:
    _ensure_paths()
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from find_short_entry_15m_v1 import build_15m_bars, load_market

    market = load_market(symbol)
    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(pane_from.timestamp()),
        to_unix=int(pane_to.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    lld_cfg = ui_lld_config("15m")
    bars15 = build_15m_bars(candles)
    by_open = {bar["open_time"]: bar for bar in bars15}
    cache: dict[int, tuple] = {}
    seen_pools: set[str] = set()
    rows: list[dict] = []

    for bar in bars15:
        moment = bar["close_time"]
        if moment < report_from or moment > report_to:
            continue
        idx = bar["candle_index"]
        pools, _ui, _proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup_long(pools, bar, moment)
        if setup is None:
            continue
        pool, bridge_lower, upper, gap = setup
        if pool["pool_id"] in seen_pools:
            continue
        h4_index = last_closed_index(market["4h"]["bars"], moment)
        if h4_index is None:
            continue
        if require_up_4h and not up_4h(market["4h"]["bars"], h4_index):
            continue
        h4 = market["4h"]["bars"][h4_index]
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        rows.append(
            row_for_long(symbol, pool, birth, bar, h4, bridge_lower, upper, gap)
        )
        seen_pools.add(pool["pool_id"])

    return rows, bars15
