"""V3 causal pipeline: join → flush → impact → reclaim → chain → controls → outcomes."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.analysis import safe_div, safe_float
from orderbook_analyse.oi_liq_impact_l2.contracts import (
    AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION,
    LIQUIDATION_SIDE_BY_DIRECTION,
)
from orderbook_analyse.ob200_v3_raw_discovery.v3.sources import ms_to_utc

PRE_WINDOWS_S = (1, 3, 5, 10, 30, 60, 180, 300)
POST_WINDOWS_S = (1, 3, 5, 10, 30, 60)
HORIZONS_S = (1, 3, 5, 10, 30, 60, 180, 300, 900, 1800, 3600)
COST_BPS = (0, 11, 15, 20)
IC_RATIOS = (0.90, 0.75, 0.50)
OI_STALENESS_S = 30.0
INTERACTION_WINDOW_S = 30  # post-touch window for first/last impact slices


def _missing() -> None:
    return None


def _ts_index(series: pd.Series) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(series, utc=True))


def mid_at(samples: pd.DataFrame, ts_ms: int) -> float | None:
    """Last mid with sample.ts_ms <= ts_ms (causal)."""
    if samples.empty:
        return None
    # samples sorted by ts_ms
    idx = samples["ts_ms"].searchsorted(ts_ms, side="right") - 1
    if idx < 0:
        return None
    return safe_float(samples.iloc[int(idx)]["mid"])


def oi_asof(oi: pd.DataFrame, when: datetime, *, max_staleness_s: float = OI_STALENESS_S) -> dict[str, Any]:
    if oi.empty or when is None:
        return {"oi": None, "oi_ts": None, "age_s": None, "status": "MISSING"}
    t = pd.Timestamp(when)
    # backward
    sub = oi[oi["bucket_time"] <= t]
    if sub.empty:
        return {"oi": None, "oi_ts": None, "age_s": None, "status": "MISSING"}
    row = sub.iloc[-1]
    age = (t - row["bucket_time"]).total_seconds()
    if age > max_staleness_s:
        return {
            "oi": safe_float(row["open_interest"]),
            "oi_ts": str(row["bucket_time"]),
            "age_s": age,
            "status": "STALE",
        }
    return {
        "oi": safe_float(row["open_interest"]),
        "oi_ts": str(row["bucket_time"]),
        "age_s": age,
        "status": "PRESENT",
    }


def window_trades(
    trades: pd.DataFrame,
    start: datetime,
    end: datetime,
    *,
    direction: str,
) -> dict[str, Any]:
    """Half-open [start, end) on 1s bars."""
    empty = {
        "window_start": start.isoformat().replace("+00:00", "Z") if start else None,
        "window_end": end.isoformat().replace("+00:00", "Z") if end else None,
        "trades_present": False,
        "trade_count": None,
        "aggressive_buy_notional": None,
        "aggressive_sell_notional": None,
        "total_notional": None,
        "net_aggressive_notional": None,
        "aggressive_notional": None,
        "signed_trade_imbalance": None,
    }
    if trades.empty or start is None or end is None:
        return empty
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    sub = trades[(trades["second"] >= s) & (trades["second"] < e)]
    if sub.empty:
        return empty
    buy = float(sub["buy_notional"].sum())
    sell = float(sub["sell_notional"].sum())
    total = buy + sell
    count = int(sub["trade_count"].sum())
    col = AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION[direction]
    aggressive = sell if col == "sell_notional" else buy
    return {
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": end.isoformat().replace("+00:00", "Z"),
        "trades_present": count > 0 and total > 0,
        "trade_count": count,
        "aggressive_buy_notional": buy,
        "aggressive_sell_notional": sell,
        "total_notional": total,
        "net_aggressive_notional": buy - sell,
        "aggressive_notional": aggressive,
        "signed_trade_imbalance": safe_div(buy - sell, total),
    }


def window_liqs(liqs: pd.DataFrame, start: datetime, end: datetime, *, direction: str) -> dict[str, Any]:
    empty = {
        "long_liq_notional": None,
        "short_liq_notional": None,
        "matched_liq_notional": None,
        "liq_count": None,
        "matched_liq_count": None,
        "liqs_present": False,
    }
    if liqs.empty or start is None or end is None:
        return empty
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    sub = liqs[(liqs["event_time"] >= s) & (liqs["event_time"] < e)]
    if sub.empty:
        return empty
    long_n = float(sub.loc[sub["liquidated_position_side"] == "LIQUIDATED_LONG", "notional_estimate"].sum())
    short_n = float(sub.loc[sub["liquidated_position_side"] == "LIQUIDATED_SHORT", "notional_estimate"].sum())
    want = LIQUIDATION_SIDE_BY_DIRECTION[direction]
    matched = sub[sub["liquidated_position_side"] == want]
    return {
        "long_liq_notional": long_n,
        "short_liq_notional": short_n,
        "matched_liq_notional": float(matched["notional_estimate"].sum()) if len(matched) else 0.0,
        "liq_count": int(len(sub)),
        "matched_liq_count": int(len(matched)),
        "liqs_present": True,
    }


def classify_flush(
    *,
    direction: str,
    price_change_bps: float | None,
    oi_delta: float | None,
    oi_status: str,
    trades: dict[str, Any],
    liqs: dict[str, Any],
) -> dict[str, Any]:
    """F1-aligned directional flush at chain pre-touch window."""
    out = {
        "flush_class": "DATA_UNAVAILABLE",
        "flush_direction": direction,
        "price_change_bps": price_change_bps,
        "oi_delta": oi_delta,
        "oi_delta_pct": None,
        "liquidation_notional": liqs.get("matched_liq_notional"),
        "aggressive_notional": trades.get("aggressive_notional"),
        "source_quality": oi_status,
        "failure_reason": "",
    }
    if oi_status == "MISSING" or not trades.get("trades_present"):
        out["flush_class"] = "DATA_UNAVAILABLE"
        out["failure_reason"] = "missing_oi_or_trades"
        return out
    if price_change_bps is None or oi_delta is None:
        out["flush_class"] = "DATA_UNAVAILABLE"
        out["failure_reason"] = "missing_price_or_oi_delta"
        return out

    # side-adjusted adverse: LONG wants down (neg raw), SHORT wants up (pos raw)
    if direction == "LONG":
        adverse = max(0.0, -price_change_bps)
        wrong = price_change_bps > 1.0
    else:
        adverse = max(0.0, price_change_bps)
        wrong = price_change_bps < -1.0
    if wrong and adverse == 0:
        out["flush_class"] = "INVALID_DIRECTION"
        out["failure_reason"] = "price_moved_wrong_way"
        return out

    oi_down = oi_delta < 0
    agg = trades.get("aggressive_notional") or 0.0
    liq_n = liqs.get("matched_liq_notional") or 0.0
    liq_ok = (liqs.get("matched_liq_count") or 0) > 0
    # F1 requires liq_count>0; allow PARTIAL if liq absent but other signals strong
    if adverse > 0 and oi_down and agg > 0 and liq_ok:
        out["flush_class"] = "CONFIRMED_FLUSH"
        return out
    if adverse > 0 and oi_down and agg > 0 and not liq_ok:
        out["flush_class"] = "PARTIAL_FLUSH"
        out["failure_reason"] = "liq_absent_but_oi_flow_ok"
        return out
    if adverse > 0 and (oi_down or agg > 0):
        out["flush_class"] = "PARTIAL_FLUSH"
        out["failure_reason"] = "incomplete_flush_signals"
        return out
    out["flush_class"] = "NO_FLUSH"
    out["failure_reason"] = "signals_not_met"
    return out


def slice_impact(
    trades: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    touch_ms: int,
    direction: str,
    window_s: int = INTERACTION_WINDOW_S,
) -> dict[str, Any]:
    """first5/last5/first10/last10/halves inside [touch, touch+window_s)."""
    start = ms_to_utc(touch_ms)
    end = ms_to_utc(touch_ms + window_s * 1000)
    assert start and end
    base: dict[str, Any] = {"interaction_window_s": window_s}

    def _slice(seconds: list[pd.Timestamp], label: str) -> None:
        if not seconds:
            base[f"{label}_trades_present"] = False
            base[f"{label}_aggressive_notional"] = None
            base[f"{label}_price_impact_bps"] = None
            base[f"{label}_impact_per_notional"] = None
            base[f"{label}_impact_bps_per_1m_usdt"] = None
            return
        t0 = seconds[0].to_pydatetime()
        t1 = (seconds[-1] + pd.Timedelta(seconds=1)).to_pydatetime()
        tw = window_trades(trades, t0, t1, direction=direction)
        mid0 = mid_at(samples, int(seconds[0].timestamp() * 1000))
        mid1 = mid_at(samples, int(seconds[-1].timestamp() * 1000))
        raw_bps = None if mid0 is None or mid1 is None or mid0 <= 0 else (mid1 - mid0) / mid0 * 10000
        if direction == "LONG":
            side_bps = None if raw_bps is None else -raw_bps  # adverse positive when price falls
        else:
            side_bps = raw_bps
        agg = tw.get("aggressive_notional")
        present = bool(tw.get("trades_present"))
        base[f"{label}_trades_present"] = present
        base[f"{label}_aggressive_notional"] = agg if present else None
        base[f"{label}_price_impact_bps"] = abs(side_bps) if side_bps is not None and present else None
        base[f"{label}_raw_price_change_bps"] = raw_bps if present else None
        imp = safe_div(base[f"{label}_price_impact_bps"], agg) if present else None
        base[f"{label}_impact_per_notional"] = imp
        base[f"{label}_impact_bps_per_1m_usdt"] = (
            safe_div(base[f"{label}_price_impact_bps"], safe_div(agg, 1_000_000.0)) if present else None
        )

    if trades.empty:
        for lab in ("first5", "last5", "first10", "last10", "first_half", "second_half"):
            _slice([], lab)
        return base

    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    secs = sorted(trades.loc[(trades["second"] >= s) & (trades["second"] < e), "second"].tolist())
    if not secs:
        for lab in ("first5", "last5", "first10", "last10", "first_half", "second_half"):
            _slice([], lab)
        return base

    _slice(secs[:5], "first5")
    _slice(secs[-5:], "last5")
    _slice(secs[:10], "first10")
    _slice(secs[-10:], "last10")
    mid = len(secs) // 2
    _slice(secs[:mid] if mid else [], "first_half")
    _slice(secs[mid:] if mid else [], "second_half")
    return base


def classify_compression(impact: dict[str, Any], *, ratio_cut: float = 0.75) -> dict[str, Any]:
    f = impact.get("first5_impact_per_notional")
    l = impact.get("last5_impact_per_notional")
    fn = impact.get("first5_aggressive_notional")
    ln = impact.get("last5_aggressive_notional")
    fp = impact.get("first5_trades_present")
    lp = impact.get("last5_trades_present")
    out = {
        "compression_class": "DATA_UNAVAILABLE",
        "compression_ratio": None,
        "flow_persistence_ratio": None,
        "ratio_cut": ratio_cut,
        "failure_reason": "",
    }
    if not fp or not lp or f is None or l is None or fn is None or ln is None:
        out["failure_reason"] = "missing_first_or_last"
        return out
    if fn <= 0 or ln <= 0 or f <= 0:
        out["failure_reason"] = "non_positive_notional_or_impact"
        return out
    ratio = l / f
    flow_pers = ln / fn
    out["compression_ratio"] = ratio
    out["flow_persistence_ratio"] = flow_pers
    if flow_pers < 0.25:
        out["compression_class"] = "FLOW_DIED"
        out["failure_reason"] = "aggressive_flow_collapsed"
        return out
    if ratio < ratio_cut and flow_pers >= 0.5 and ln >= max(500.0, 0.5 * fn):
        out["compression_class"] = "IC_STRICT"
        return out
    if ratio < 1.0 and flow_pers >= 0.25:
        out["compression_class"] = "IC_RELAXED"
        return out
    out["compression_class"] = "NO_COMPRESSION"
    out["failure_reason"] = "ratio_or_flow_not_met"
    return out


def reclaim_and_entry(
    chain: dict[str, Any],
    samples: pd.DataFrame,
) -> dict[str, Any]:
    """Map V2 reclaim_ts to variants; entry = next sample after confirmed_at."""
    touch = int(chain["touch_ts"]) if chain.get("touch_ts") else None
    reclaim = int(chain["reclaim_ts"]) if chain.get("reclaim_ts") else None
    wall = safe_float(chain.get("wall_price"))
    direction = chain["direction"]
    out: dict[str, Any] = {
        "reclaim_variant": None,
        "reclaim_at": reclaim,
        "confirmed_at": None,
        "entry_decision_at": None,
        "entry_at": None,
        "entry_mid": None,
        "entry_source": None,
        "hold_duration_s": None,
        "confirmation_distance_bps": None,
        "retest_held": None,
    }
    if reclaim is None or touch is None or wall is None or samples.empty:
        return out

    # R1: reclaim timestamp from V2 already means cross+hold rule in walls.py (3s)
    # Classify by hold vs touch
    hold_s = (reclaim - touch) / 1000.0
    out["hold_duration_s"] = hold_s
    mid_r = mid_at(samples, reclaim)
    if mid_r is not None and wall > 0:
        out["confirmation_distance_bps"] = abs(mid_r - wall) / wall * 10000
    if hold_s >= 3:
        out["reclaim_variant"] = "R3_HOLD_3S"
    elif hold_s >= 1:
        out["reclaim_variant"] = "R2_HOLD_1S"
    else:
        out["reclaim_variant"] = "R1_CROSS"
    if out["confirmation_distance_bps"] is not None and out["confirmation_distance_bps"] >= 1.0:
        out["reclaim_variant"] = "R5_BPS_CONFIRM"

    confirmed = reclaim
    out["confirmed_at"] = confirmed
    out["entry_decision_at"] = confirmed
    # next sample strictly after confirmed_at
    idx = samples["ts_ms"].searchsorted(confirmed, side="right")
    if idx < len(samples):
        row = samples.iloc[int(idx)]
        out["entry_at"] = int(row["ts_ms"])
        out["entry_mid"] = safe_float(row["mid"])
        out["entry_source"] = "l2_sample_after_confirmed_at"
    return out


def build_full_chain_row(
    chain: dict[str, Any],
    flush: dict[str, Any],
    impact: dict[str, Any],
    compression: dict[str, Any],
    reclaim: dict[str, Any],
) -> dict[str, Any]:
    stages = {
        "STAGE_0_VALID_L2": True,
        "STAGE_1_CONFIRMED_FLUSH": flush.get("flush_class") == "CONFIRMED_FLUSH",
        "STAGE_2_WALL_TOUCH": bool(chain.get("touch_ts")),
        "STAGE_3_WALL_INTERACTION": bool(
            chain.get("absorption_ts") or chain.get("pull_ts") or chain.get("break_ts")
        ),
        "STAGE_4_FLOW_CONFIRMED": bool(
            impact.get("first5_trades_present") and (impact.get("first5_aggressive_notional") or 0) > 0
        ),
        "STAGE_5_IMPACT_COMPRESSION_STRICT": compression.get("compression_class") == "IC_STRICT",
        "STAGE_5_IMPACT_COMPRESSION_RELAXED": compression.get("compression_class") in {
            "IC_STRICT",
            "IC_RELAXED",
        },
        "STAGE_6_RECLAIM_CONFIRMED": reclaim.get("reclaim_variant") is not None,
        "STAGE_7_ENTRY_READY": reclaim.get("entry_at") is not None and reclaim.get("entry_mid") is not None,
    }
    strict = all(
        [
            stages["STAGE_0_VALID_L2"],
            stages["STAGE_1_CONFIRMED_FLUSH"],
            stages["STAGE_2_WALL_TOUCH"],
            stages["STAGE_3_WALL_INTERACTION"],
            stages["STAGE_4_FLOW_CONFIRMED"],
            stages["STAGE_5_IMPACT_COMPRESSION_STRICT"],
            stages["STAGE_6_RECLAIM_CONFIRMED"],
            stages["STAGE_7_ENTRY_READY"],
        ]
    )
    relaxed = all(
        [
            stages["STAGE_0_VALID_L2"],
            stages["STAGE_1_CONFIRMED_FLUSH"] or flush.get("flush_class") == "PARTIAL_FLUSH",
            stages["STAGE_2_WALL_TOUCH"],
            stages["STAGE_3_WALL_INTERACTION"],
            stages["STAGE_4_FLOW_CONFIRMED"],
            stages["STAGE_5_IMPACT_COMPRESSION_RELAXED"],
            stages["STAGE_6_RECLAIM_CONFIRMED"],
            stages["STAGE_7_ENTRY_READY"],
        ]
    )
    if strict:
        completion = "FULL_STRATEGY_CHAIN_STRICT"
    elif relaxed:
        completion = "FULL_STRATEGY_CHAIN_RELAXED"
    elif stages["STAGE_6_RECLAIM_CONFIRMED"] and not stages["STAGE_1_CONFIRMED_FLUSH"]:
        completion = "RECLAIM_WITHOUT_FLUSH"
    elif stages["STAGE_1_CONFIRMED_FLUSH"] and not stages["STAGE_5_IMPACT_COMPRESSION_RELAXED"]:
        completion = "FLUSH_WITHOUT_COMPRESSION"
    elif stages["STAGE_5_IMPACT_COMPRESSION_RELAXED"] and not stages["STAGE_6_RECLAIM_CONFIRMED"]:
        completion = "COMPRESSION_WITHOUT_RECLAIM"
    elif compression.get("compression_class") == "FLOW_DIED":
        completion = "FLOW_DIED"
    elif flush.get("flush_class") == "DATA_UNAVAILABLE" or compression.get("compression_class") == "DATA_UNAVAILABLE":
        completion = "DATA_UNAVAILABLE"
    elif flush.get("flush_class") == "INVALID_DIRECTION":
        completion = "INVALID_DIRECTION"
    elif chain.get("completion_class") == "COMPLETE_PRIMARY":
        completion = "L2_ONLY_COMPLETE"
    else:
        completion = "INVALID_CHAIN"

    row = {
        "chain_id": chain["chain_id"],
        "lifecycle_id": chain["lifecycle_id"],
        "symbol": chain["symbol"],
        "direction": chain["direction"],
        "completion_class_v3": completion,
        "flush_class": flush.get("flush_class"),
        "compression_class": compression.get("compression_class"),
        "reclaim_variant": reclaim.get("reclaim_variant"),
        "entry_at": reclaim.get("entry_at"),
        "entry_mid": reclaim.get("entry_mid"),
        "wall_price": chain.get("wall_price"),
        "touch_ts": chain.get("touch_ts"),
        "reclaim_ts": chain.get("reclaim_ts"),
    }
    row.update(stages)
    return row


def _build_blocked_mask(ts_ms: pd.Series, intervals: list[tuple[int, int]]) -> pd.Series:
    """Vectorized membership in any [a,b] forbidden interval."""
    if ts_ms.empty:
        return pd.Series(False, index=ts_ms.index)
    if not intervals:
        return pd.Series(False, index=ts_ms.index)
    iv = sorted((int(a), int(b)) for a, b in intervals)
    merged: list[list[int]] = []
    for a, b in iv:
        if not merged or a > merged[-1][1] + 1:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    blocked = pd.Series(False, index=ts_ms.index)
    for a, b in merged:
        blocked |= (ts_ms >= a) & (ts_ms <= b)
    return blocked


def match_controls(
    entries: list[dict[str, Any]],
    samples_by_symbol: dict[str, pd.DataFrame],
    *,
    seed: int = 42,
    per_event: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match non-event controls at entry_decision state without outcome look-ahead.

    Exclusion: ±2m around event entry and touch→reclaim±1m. Matching uses UTC hour,
    then nearest spread_bps / imbalance_l10 among free samples (deterministic seed).
    """
    rng = random.Random(seed)
    forbidden: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for e in entries:
        if e.get("entry_at") is None:
            continue
        ea = int(e["entry_at"])
        forbidden[e["symbol"]].append((ea - 120_000, ea + 300_000))
        if e.get("touch_ts"):
            t0 = int(e["touch_ts"])
            t1 = int(e["reclaim_ts"] or ea)
            forbidden[e["symbol"]].append((t0 - 60_000, t1 + 60_000))

    free_by_sym: dict[str, pd.DataFrame] = {}
    for sym, samples in samples_by_symbol.items():
        if samples is None or samples.empty:
            free_by_sym[sym] = pd.DataFrame()
            continue
        warm = samples["warmup"].astype(bool)
        blocked = _build_blocked_mask(samples["ts_ms"].astype(int), forbidden.get(sym, []))
        free = samples.loc[~warm & ~blocked].copy()
        if not free.empty:
            free["utc_hour"] = (free["ts_ms"].astype(int) // 3_600_000) % 24
        free_by_sym[sym] = free

    controls: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    used_ts: dict[str, set[int]] = defaultdict(set)
    cid = 0
    for e in entries:
        if e.get("entry_at") is None or e.get("entry_mid") is None:
            continue
        sym = e["symbol"]
        free = free_by_sym.get(sym)
        if free is None or free.empty:
            quality.append({"event_chain_id": e["chain_id"], "n_controls": 0, "match_quality": "NONE"})
            continue
        hour = (int(e["entry_at"]) // 3_600_000) % 24
        pool = free[free["utc_hour"] == hour]
        match_quality = "HOUR_SPREAD_OK"
        if len(pool) < max(5, per_event):
            pool = free
            match_quality = "FALLBACK_ANY_HOUR"
        # drop already used control timestamps for this symbol (reduce pseudo-replication)
        if used_ts[sym]:
            pool = pool[~pool["ts_ms"].astype(int).isin(used_ts[sym])]
        if pool.empty:
            quality.append({"event_chain_id": e["chain_id"], "n_controls": 0, "match_quality": "NONE"})
            continue

        ev_spread = safe_float(e.get("spread_bps"))
        ev_imb = safe_float(e.get("imbalance_l10"))
        scored = pool.copy()
        if ev_spread is not None and "spread_bps" in scored.columns:
            scored["_d_spread"] = (scored["spread_bps"].astype(float) - ev_spread).abs()
        else:
            scored["_d_spread"] = 0.0
        if ev_imb is not None and "imbalance_l10" in scored.columns:
            scored["_d_imb"] = (scored["imbalance_l10"].astype(float) - ev_imb).abs()
        else:
            scored["_d_imb"] = 0.0
        scored["_dist"] = scored["_d_spread"].fillna(0) + scored["_d_imb"].fillna(0)
        scored = scored.sort_values(["_dist", "ts_ms"])
        # take top-K nearest then sample deterministically among them
        top = scored.head(min(50, len(scored)))
        idxs = list(top.index)
        rng.shuffle(idxs)
        picks_idx = idxs[: min(per_event, len(idxs))]
        picks = top.loc[picks_idx]
        n_ok = 0
        for _, p in picks.iterrows():
            ts = int(p["ts_ms"])
            if ts in used_ts[sym]:
                continue
            used_ts[sym].add(ts)
            cid += 1
            n_ok += 1
            controls.append(
                {
                    "control_id": f"ctrl_v3_{seed}_{cid}",
                    "matched_to_chain_id": e["chain_id"],
                    "symbol": sym,
                    "direction": e["direction"],
                    "entry_at": ts,
                    "entry_mid": safe_float(p["mid"]),
                    "spread_bps": safe_float(p["spread_bps"]),
                    "imbalance_l10": safe_float(p["imbalance_l10"]),
                    "match_distance": safe_float(p["_dist"]),
                    "is_control": True,
                }
            )
        quality.append(
            {
                "event_chain_id": e["chain_id"],
                "n_controls": n_ok,
                "match_quality": match_quality if n_ok else "NONE",
                "pool_size": int(len(pool)),
            }
        )
    return controls, quality


def compute_outcomes(
    rows: list[dict[str, Any]],
    samples_by_symbol: dict[str, pd.DataFrame],
    *,
    is_control: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in rows:
        entry = e.get("entry_at")
        mid0 = e.get("entry_mid")
        if entry is None or mid0 is None or mid0 <= 0:
            continue
        samples = samples_by_symbol.get(e["symbol"])
        if samples is None or samples.empty:
            continue
        direction = e["direction"]
        eid = e.get("chain_id") or e.get("control_id")
        for h in HORIZONS_S:
            t1 = int(entry) + h * 1000
            # path mids in (entry, t1]
            sub = samples[(samples["ts_ms"] > int(entry)) & (samples["ts_ms"] <= t1)]
            complete = len(sub) >= max(1, h // 2)  # rough completeness
            if sub.empty:
                out.append(
                    {
                        "event_id": eid,
                        "symbol": e["symbol"],
                        "direction": direction,
                        "is_control": is_control,
                        "horizon_s": h,
                        "forward_return_bps": None,
                        "mfe_bps": None,
                        "mae_bps": None,
                        "horizon_complete": False,
                        "mid0": mid0,
                    }
                )
                continue
            mids = [safe_float(x) for x in sub["mid"].tolist()]
            mids = [m for m in mids if m is not None]
            if not mids:
                continue
            end = mids[-1]
            if direction == "LONG":
                rets = [(m - mid0) / mid0 * 10000 for m in mids]
                fwd = (end - mid0) / mid0 * 10000
            else:
                rets = [(mid0 - m) / mid0 * 10000 for m in mids]
                fwd = (mid0 - end) / mid0 * 10000
            out.append(
                {
                    "event_id": eid,
                    "symbol": e["symbol"],
                    "direction": direction,
                    "is_control": is_control,
                    "horizon_s": h,
                    "forward_return_bps": fwd,
                    "mfe_bps": max(rets),
                    "mae_bps": min(rets),
                    "horizon_complete": bool(complete and sub["ts_ms"].max() >= t1 - 1500),
                    "mid0": mid0,
                }
            )
    return out


def cost_summary(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[float]] = defaultdict(list)
    for r in outcomes:
        if not r.get("horizon_complete"):
            continue
        if r.get("forward_return_bps") is None:
            continue
        if r["horizon_s"] not in (60, 300, 900):
            continue
        key = (r["symbol"], r["direction"], r["horizon_s"], r["is_control"])
        groups[key].append(float(r["forward_return_bps"]))
    rows = []
    for key, vals in sorted(groups.items()):
        for cost in COST_BPS:
            nets = [v - cost for v in vals]
            rows.append(
                {
                    "symbol": key[0],
                    "direction": key[1],
                    "horizon_s": key[2],
                    "is_control": key[3],
                    "cost_bps": cost,
                    "n": len(vals),
                    "mean_gross_bps": sum(vals) / len(vals),
                    "median_gross_bps": sorted(vals)[len(vals) // 2],
                    "mean_net_bps": sum(nets) / len(nets),
                    "hit_rate_net_gt0": sum(1 for x in nets if x > 0) / len(nets),
                }
            )
    return rows
