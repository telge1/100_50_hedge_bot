"""Frozen find_short_entry_15m_v1 signals on Original-LMV2 CH SSOT (no trades, no guard)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
    load_historical_lmv2_backtester_input_at_close,
)
from research_charts.historical_lmv2_frozen_short_paths_v1 import (
    _FREEZE_COMMIT,
    _FREEZE_TAG,
    load_frozen_entry_module,
    verify_frozen_entry_hash,
)
from research_charts.historical_lmv2_ssot_legacy_bridge_v1 import legacy_pools_from_bridge_state

SIGNAL_VERSION = "historical_lmv2_frozen_short_signal_v1"
FLOOR_GUARD_IN_PILOT = False
E1R_IN_PILOT = False


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_decision_close(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(text))


def decision_close_grid(first: datetime, last: datetime) -> list[datetime]:
    fc = parse_decision_close(first)
    lc = parse_decision_close(last)
    if lc < fc:
        raise ValueError("last before first")
    if fc.minute % 15 or fc.second or fc.microsecond:
        raise ValueError("first decision_close not on 15m grid")
    out: list[datetime] = []
    cur = fc
    while cur <= lc:
        out.append(cur)
        cur = cur + timedelta(minutes=15)
    return out


@dataclass
class DecisionTrace:
    decision_close: str
    candle_open_utc: str
    pool_detail: int
    bridge_input_state_hash: str
    snapshot_id: str
    active_member_count: int
    pool_ids: list[str]
    bar: dict[str, Any] | None
    down_4h: bool | None
    h4_close_time: str | None
    entry_setup: bool
    block_reason: str | None
    signal: bool
    entry_pool_id: str | None
    stop_price: float | None
    tp_top: float | None
    gap_pct: float | None
    seen_pool_would_skip: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_close": self.decision_close,
            "candle_open_utc": self.candle_open_utc,
            "pool_detail": self.pool_detail,
            "bridge_input_state_hash": self.bridge_input_state_hash,
            "snapshot_id": self.snapshot_id,
            "active_member_count": self.active_member_count,
            "pool_ids": self.pool_ids,
            "bar": self.bar,
            "down_4h": self.down_4h,
            "h4_close_time": self.h4_close_time,
            "entry_setup": self.entry_setup,
            "block_reason": self.block_reason,
            "signal": self.signal,
            "entry_pool_id": self.entry_pool_id,
            "stop_price": self.stop_price,
            "tp_top": self.tp_top,
            "gap_pct": self.gap_pct,
            "seen_pool_would_skip": self.seen_pool_would_skip,
            "extra": self.extra,
        }


def _bar_dict_for_trace(bar: dict) -> dict[str, Any]:
    return {
        "open_time": _iso_z(bar["open_time"]),
        "close_time": _iso_z(bar["close_time"]),
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "close": bar["close"],
        "bullish_close_gt_open": bar["close"] > bar["open"],
    }


def evaluate_decision(
    *,
    symbol: str,
    bar: dict,
    market: dict,
    pool_detail: int,
    client=None,
    database: str | None = None,
    seen_pool_ids: set[str] | None = None,
) -> DecisionTrace:
    """One closed 15m bar: SSOT pools + frozen entry rules (no floor guard)."""
    fe = load_frozen_entry_module()
    moment = _utc(bar["close_time"])
    dc = moment
    open_time = _utc(bar["open_time"])
    db = database
    if db is None and client is not None:
        db = getattr(client, "database", None)

    bridge = load_historical_lmv2_backtester_input_at_close(
        dc,
        symbol=symbol,
        timeframe="15m",
        pool_detail=int(pool_detail),
        client=client,
        database=db,
    )
    pools = legacy_pools_from_bridge_state(bridge, decision_close=dc)
    pool_ids = [p["pool_id"] for p in pools]

    h4_index = fe.last_closed_index(market["4h"]["bars"], moment)
    down = False
    h4_ct: str | None = None
    if h4_index is not None:
        down = bool(fe.down_4h(market["4h"]["bars"], h4_index))
        h4_ct = _iso_z(market["4h"]["bars"][h4_index]["close_time"])

    setup = fe.entry_setup(pools, bar, moment)
    block: str | None = None
    signal = False
    entry_pool_id = None
    stop_price = None
    tp_top = None
    gap_val = None
    seen_skip = False

    if setup is None:
        block = "NO_ENTRY_SETUP"
    elif not down:
        block = "DOWN_4H_FALSE"
    else:
        pool, upper, lower, gap = setup
        entry_pool_id = pool["pool_id"]
        stop_price = round(pool["top"] * (1.0 + fe.STOP_PAD), 8)
        tp_top = float(lower["top"])
        gap_val = round(gap, 4)
        if seen_pool_ids is not None and entry_pool_id in seen_pool_ids:
            seen_skip = True
            block = "EPISODE_ALREADY_SEEN"
        else:
            signal = True
            if seen_pool_ids is not None:
                seen_pool_ids.add(entry_pool_id)

    return DecisionTrace(
        decision_close=_iso_z(dc),
        candle_open_utc=_iso_z(open_time),
        pool_detail=int(pool_detail),
        bridge_input_state_hash=bridge.input_state_hash,
        snapshot_id=bridge.snapshot_id,
        active_member_count=bridge.active_member_count,
        pool_ids=pool_ids,
        bar=_bar_dict_for_trace(bar),
        down_4h=down if h4_index is not None else None,
        h4_close_time=h4_ct,
        entry_setup=setup is not None,
        block_reason=block,
        signal=signal,
        entry_pool_id=entry_pool_id,
        stop_price=stop_price,
        tp_top=tp_top,
        gap_pct=gap_val,
        seen_pool_would_skip=seen_skip,
        extra={
            "next_upper_id": (setup[1]["pool_id"] if setup else None),
            "tp_pool_id": (setup[2]["pool_id"] if setup else None),
            "freeze_tag": _FREEZE_TAG,
            "freeze_commit": _FREEZE_COMMIT,
        },
    )


def load_market_and_bars(
    symbol: str,
    *,
    pane_from: datetime,
    pane_to: datetime,
    client=None,
) -> tuple[dict, list[dict], dict[str, Any]]:
    """15m bars + multi-TF market for down_4h (frozen load_market + pane candles)."""
    fe = load_frozen_entry_module()
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    meta: dict[str, Any] = {"symbol": symbol}
    try:
        market = fe.load_market(symbol)
    except SystemExit as exc:
        raise RuntimeError(f"load_market failed: {exc}") from exc
    meta["load_market_end"] = fe.LOAD_END.isoformat()

    _packed, candles15 = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(_utc(pane_from).timestamp()),
        to_unix=int(_utc(pane_to).timestamp()),
        history_weeks=fe.HISTORY_WEEKS,
    )
    if not _packed.get("strict_complete_buckets"):
        raise RuntimeError("15m candles not strict_complete_buckets")
    bars15 = fe.build_15m_bars(candles15)
    meta["bars15_count"] = len(bars15)
    meta["history_weeks"] = fe.HISTORY_WEEKS
    return market, bars15, meta


def run_signal_pilot(
    *,
    symbol: str = "DOGEUSDT",
    first_decision_close: str,
    last_decision_close: str,
    pool_details: tuple[int, ...] = (10,),
    client=None,
    database: str | None = None,
) -> dict[str, Any]:
    """Signal-only pilot over consecutive 15m closes (no fills, no guard)."""
    verify = verify_frozen_entry_hash()
    if not verify["match"]:
        return {"verdict": "FAIL", "reason": "FROZEN_HASH_MISMATCH", "verify": verify}

    fe = load_frozen_entry_module()
    grid = decision_close_grid(
        parse_decision_close(first_decision_close),
        parse_decision_close(last_decision_close),
    )
    if len(grid) > 20:
        grid = grid[:20]

    warmup_start = parse_decision_close(first_decision_close) - timedelta(days=fe.HISTORY_WEEKS * 7)
    pane_to = parse_decision_close(last_decision_close) + timedelta(minutes=15)

    market, bars15, load_meta = load_market_and_bars(
        symbol,
        pane_from=warmup_start,
        pane_to=pane_to,
        client=client,
    )
    by_close = {_iso_z(_utc(b["close_time"])): b for b in bars15}

    results: dict[str, Any] = {
        "signal_version": SIGNAL_VERSION,
        "symbol": symbol,
        "first_decision_close": first_decision_close,
        "last_decision_close": last_decision_close,
        "decision_count": len(grid),
        "floor_guard_enabled": FLOOR_GUARD_IN_PILOT,
        "e1r_enabled": E1R_IN_PILOT,
        "freeze_verification": verify,
        "frozen_constants": {
            "MIN_GAP_PCT": fe.MIN_GAP_PCT,
            "MIN_BRIDGE_GAP_PCT": fe.MIN_BRIDGE_GAP_PCT,
            "MIN_TP_PCT": fe.MIN_TP_PCT,
            "STOP_PAD": fe.STOP_PAD,
            "ATTACH_PCT": fe.ATTACH_PCT,
            "entry_setup_requires_close_gt_open": True,
        },
        "load_meta": load_meta,
        "pool_source": "clickhouse_ssot_phase_6b_6c",
        "by_pool_detail": {},
    }

    for pd in pool_details:
        seen: set[str] = set()
        traces: list[dict[str, Any]] = []
        signals = 0
        for dc in grid:
            dc_iso = _iso_z(dc)
            bar = by_close.get(dc_iso)
            if bar is None:
                traces.append(
                    {
                        "decision_close": dc_iso,
                        "block_reason": "MISSING_15M_BAR",
                        "signal": False,
                    }
                )
                continue
            tr = evaluate_decision(
                symbol=symbol,
                bar=bar,
                market=market,
                pool_detail=pd,
                client=client,
                database=database,
                seen_pool_ids=seen,
            )
            traces.append(tr.to_dict())
            if tr.signal:
                signals += 1

        results["by_pool_detail"][str(pd)] = {
            "pool_detail": pd,
            "traces": traces,
            "signals_emitted": signals,
            "verdict": "PASS" if traces else "FAIL",
        }

    any_signal = any(
        results["by_pool_detail"][k]["signals_emitted"] > 0 for k in results["by_pool_detail"]
    )
    results["pilot_status"] = "NO_SIGNAL" if not any_signal else "SIGNAL_PRESENT"
    results["verdict"] = "PASS"
    return results


def frozen_rules_contract() -> dict[str, Any]:
    fe = load_frozen_entry_module()
    return {
        "module": "find_short_entry_15m_v1",
        "freeze_verification": verify_frozen_entry_hash(),
        "constants": {
            "MIN_GAP_PCT": fe.MIN_GAP_PCT,
            "MIN_BRIDGE_GAP_PCT": fe.MIN_BRIDGE_GAP_PCT,
            "MIN_TP_PCT": fe.MIN_TP_PCT,
            "STOP_PAD": fe.STOP_PAD,
            "ATTACH_PCT": fe.ATTACH_PCT,
        },
        "down_4h_depth_pct_min": 2.0,
        "entry_setup": {
            "requires_close_gt_open": True,
            "note": "Frozen code uses close > open (not bearish); attached() does not check candle color.",
        },
        "stop": "pool['top'] * (1 + STOP_PAD)",
        "tp": "next lower pool top (next_lower)",
        "floor_guard": "not applied in this signal module",
        "e1r": "not applied in this signal module",
    }
