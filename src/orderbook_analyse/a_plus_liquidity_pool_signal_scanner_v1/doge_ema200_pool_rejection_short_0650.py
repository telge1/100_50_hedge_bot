"""Causal audit: DOGE EMA200 / Ask-pool rejection short ~06:40–07:10 UTC."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import fetch_candles_1m
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import zone_half_width
from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import (
    DEFAULT_OUT_DIR,
    MIN_GROSS_RR,
    MIN_TARGET_DISTANCE_ATR,
    STOP_ATR_BUFFER,
    TF_CONFIRM,
    TF_ENTRY_POOL,
    TF_LIQUIDITY,
    TF_MACRO,
    TF_STRUCTURE,
)
from .gates import estimated_net_rr, gross_rr
from .models import PoolRecord, _utc_naive
from .pools import load_engine_pools_at, pool_from_engine, pool_valid_at
from .runner import build_candles_by_tf
from .setups import _select_target_below, _stop_target_levels, atr_available

SYMBOL = "DOGEUSDT"
WARMUP_START = datetime(2026, 8, 25, 0, 0, 0)
WINDOW_START = datetime(2026, 8, 28, 6, 30, 0)
WINDOW_END = datetime(2026, 8, 28, 7, 15, 0)

ENTRY_POOL_ID = "lld:DOGEUSDT:15m:upper:1787886900"
OLD_PULLBACK_EPISODE = "A_PLUS_PULLBACK_SHORT:lld:DOGEUSDT:15m:upper:1787886900"

VERDICT_CONFIRMED = "CAUSAL_EMA200_POOL_REJECTION_SHORT_CONFIRMED"
VERDICT_LATE = "REJECTION_VISIBLE_BUT_CONFIRMATION_TOO_LATE"
VERDICT_EMA_NOT_TOUCHED = "EMA200_NOT_CAUSALLY_TOUCHED"
VERDICT_POOL_INACTIVE = "ASK_POOL_NOT_ACTIVE_AT_REJECTION"
VERDICT_NO_TARGET = "NO_CAUSAL_TARGET_POOL"
VERDICT_MANUAL = "MANUAL_REJECTION_NOT_MACHINE_REPRODUCIBLE"


def _iso(ts: Any) -> str:
    if ts is None:
        return ""
    return _utc_naive(pd.Timestamp(ts).to_pydatetime()).isoformat()


def _bar_close(open_time: datetime, tf_minutes: int = 5) -> datetime:
    return _utc_naive(pd.Timestamp(open_time).to_pydatetime()) + timedelta(minutes=tf_minutes)


def _ema200_band(ema200: float, atr: float, tick: float) -> tuple[float, float, float]:
    hw = zone_half_width(atr, tick=tick)
    return ema200 - hw, ema200 + hw, hw


def _candle_metrics(row: pd.Series) -> dict[str, Any]:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = abs(c - o)
    rng = h - l
    direction = "bullish" if c > o else ("bearish" if c < o else "doji")
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "candle_direction": direction,
        "range": rng,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "atr_14": row.get("atr_14"),
        "volume": row.get("volume"),
    }


def _touch_zone(high: float, low: float, z_lo: float, z_hi: float) -> bool:
    return high >= z_lo and low <= z_hi


def _pool_map(candles_by_tf: dict[str, pd.DataFrame], as_of: datetime) -> dict[str, PoolRecord]:
    out: dict[str, PoolRecord] = {}
    for tf, pools in load_engine_pools_at(
        candles_by_tf, symbol=SYMBOL, as_of=as_of, timeframes=(TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO)
    ).items():
        for p in pools:
            out[str(p.pool_id)] = pool_from_engine(p)
    return out


def _active_ask_pools(pmap: dict[str, PoolRecord], as_of: datetime, price: float) -> list[PoolRecord]:
    return sorted(
        [
            p
            for p in pmap.values()
            if p.side == "ASK" and pool_valid_at(p, as_of) and p.lower_edge <= price * 1.002
        ],
        key=lambda p: abs(p.midpoint - price),
    )


def _rejection_sl(
    *,
    rejection_high: float,
    pool_upper: float,
    ema_band_high: float,
    atr: float,
    tick: float,
) -> tuple[float, float, float, float, float]:
    stop_reference = max(rejection_high, pool_upper, ema_band_high)
    buf = max(tick * 2, (atr if atr_available(atr) else 0) * STOP_ATR_BUFFER)
    return stop_reference, buf, rejection_high, pool_upper, ema_band_high


def _rank_targets(entry: float, pools: list[PoolRecord], entry_pool: PoolRecord, atr: float, as_of: datetime) -> list[dict]:
    combined = pools
    rows: list[dict[str, Any]] = []
    for p in combined:
        if p.side != "BID" or not pool_valid_at(p, as_of) or p.midpoint >= entry:
            continue
        dist_atr = abs(entry - p.near_edge) / atr if atr_available(atr) else float("inf")
        if dist_atr < MIN_TARGET_DISTANCE_ATR:
            continue
        stop, target = _stop_target_levels(
            direction="SHORT",
            symbol=SYMBOL,
            entry=entry,
            entry_pool=entry_pool,
            target_pool=p,
            atr=atr,
            sweep_high=None,
            sweep_low=None,
        )
        grr = gross_rr("SHORT", entry, stop, target)
        rows.append(
            {
                "target_pool_id": p.pool_id,
                "timeframe": p.timeframe,
                "available_at": _iso(p.available_at),
                "invalidated_at": _iso(p.invalidated_at),
                "lower_edge": p.lower_edge,
                "upper_edge": p.upper_edge,
                "midpoint": p.midpoint,
                "strength": p.strength,
                "distance": entry - p.midpoint,
                "gross_rr": grr,
                "net_rr": estimated_net_rr(grr),
            }
        )
    rows.sort(key=lambda r: r["distance"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["selected_by_contract"] = i == 1
    return rows


def _outcome_audit(
    df1: pd.DataFrame,
    *,
    decision_at: datetime,
    entry: float,
    stop: float,
    target: float,
    horizon_end: datetime,
) -> dict[str, Any]:
    sub = df1[(df1["open_time"] > decision_at) & (df1["open_time"] <= horizon_end)].copy()
    mfe, mae = 0.0, 0.0
    result = "neither"
    tp_at, sl_at = None, None
    ambiguous = False
    for _, r in sub.iterrows():
        h, l = float(r["high"]), float(r["low"])
        mfe = max(mfe, entry - l)
        mae = max(mae, h - entry)
        hit_tp = l <= target
        hit_sl = h >= stop
        if hit_tp and hit_sl:
            ambiguous = True
            result = "ambiguous"
            break
        if hit_sl and result == "neither":
            result = "sl_first"
            sl_at = _iso(r["open_time"] + timedelta(minutes=1))
            break
        if hit_tp and result == "neither":
            result = "tp_first"
            tp_at = _iso(r["open_time"] + timedelta(minutes=1))
            break
    return {
        "outcome": result,
        "ambiguous_same_bar": ambiguous,
        "mfe": mfe,
        "mae": mae,
        "tp_at": tp_at,
        "sl_at": sl_at,
        "horizon_end": _iso(horizon_end),
    }


@dataclass
class AuditState:
    candles_5m: list[dict[str, Any]] = field(default_factory=list)
    ema_timeline: list[dict[str, Any]] = field(default_factory=list)
    ask_pool_timeline: list[dict[str, Any]] = field(default_factory=list)
    confluence: list[dict[str, Any]] = field(default_factory=list)
    rejection_defs: list[dict[str, Any]] = field(default_factory=list)
    entry_variants: list[dict[str, Any]] = field(default_factory=list)
    structure_events: list[dict[str, Any]] = field(default_factory=list)


def run_audit() -> dict[str, Any]:
    run_id = int(time.time())
    out_dir = Path(DEFAULT_OUT_DIR) / f"doge_ema200_pool_rejection_short_0650_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = get_clickhouse_client()
    candles = build_candles_by_tf(SYMBOL, WARMUP_START, WINDOW_END + timedelta(hours=2), client=client)
    df5 = attach_context(candles[TF_STRUCTURE].sort_values("open_time").reset_index(drop=True))
    df1 = attach_context(candles[TF_CONFIRM].sort_values("open_time").reset_index(drop=True))
    tick = tick_size(SYMBOL)

    state = AuditState()
    tick_rows: list[dict[str, Any]] = []

    # --- 5m candle table in window ---
    win5 = df5[(df5["open_time"] >= WINDOW_START) & (df5["open_time"] <= WINDOW_END)].copy()
    indices = win5.index.tolist()
    pullback_high = 0.0
    pullback_high_bar = None
    last_up_bar = None

    for idx in indices:
        row = df5.loc[idx]
        prev_idx = idx - 1
        prev = df5.loc[prev_idx] if prev_idx in df5.index else None
        bar_open = _utc_naive(row["open_time"].to_pydatetime())
        bar_close = _bar_close(bar_open)
        m = _candle_metrics(row)

        ema200_prior = prev["ema_200"] if prev is not None else None
        atr_prior = prev["atr_14"] if prev is not None else None
        ema200_at_close = row.get("ema_200")
        atr_at_close = row.get("atr_14")
        prior_bar_end = _bar_close(_utc_naive(prev["open_time"].to_pydatetime())) if prev is not None else None

        band_lo, band_hi, hw = (None, None, None)
        if ema200_prior is not None and atr_prior is not None and not pd.isna(ema200_prior) and not pd.isna(atr_prior):
            band_lo, band_hi, hw = _ema200_band(float(ema200_prior), float(atr_prior), tick)

        pool_at = _pool_map(candles, bar_close)
        entry_pool = pool_at.get(ENTRY_POOL_ID)
        pool_status = entry_pool.status_at(bar_close) if entry_pool else "ABSENT"

        if m["candle_direction"] == "bullish":
            last_up_bar = {"bar_open": _iso(bar_open), "bar_close": _iso(bar_close), "high": m["high"]}
        if m["high"] > pullback_high:
            pullback_high = m["high"]
            pullback_high_bar = {"bar_open": _iso(bar_open), "bar_close": _iso(bar_close), "high": m["high"]}

        touch_ema = False
        close_vs_ema = "unknown"
        if band_lo is not None and band_hi is not None:
            touch_ema = _touch_zone(m["high"], m["low"], band_lo, band_hi)
            if m["close"] > band_hi:
                close_vs_ema = "above"
            elif m["close"] < band_lo:
                close_vs_ema = "below"
            else:
                close_vs_ema = "inside"

        pool_touch = False
        pool_penetration = 0.0
        pool_half = ""
        sweep_over = False
        close_under_pool = False
        if entry_pool and pool_status == "ACTIVE":
            pool_touch = m["high"] >= entry_pool.lower_edge
            pool_penetration = max(0.0, m["high"] - entry_pool.upper_edge)
            sweep_over = m["high"] > entry_pool.upper_edge
            close_under_pool = m["close"] < entry_pool.lower_edge
            mid = entry_pool.midpoint
            pool_half = "upper" if m["close"] >= mid else "lower"

        dist_pool_ema_atr = None
        dist_pool_ema_pct = None
        if entry_pool and ema200_prior is not None and not pd.isna(ema200_prior) and atr_prior and not pd.isna(atr_prior):
            dist_pool_ema_atr = abs(entry_pool.midpoint - float(ema200_prior)) / float(atr_prior)
            dist_pool_ema_pct = abs(entry_pool.midpoint - float(ema200_prior)) / float(ema200_prior) * 100

        c5 = {
            "bar_open": _iso(bar_open),
            "bar_close": _iso(bar_close),
            **m,
            "bar_open_time": _iso(bar_open),
            "bar_close_time": _iso(bar_close),
        }
        state.candles_5m.append(c5)

        ema_row = {
            "bar_open": _iso(bar_open),
            "bar_close": _iso(bar_close),
            "ema9": row.get("ema_9"),
            "ema20": row.get("ema_20"),
            "ema59": row.get("ema_59"),
            "ema200_at_open": ema200_prior,
            "ema200_at_close": ema200_at_close,
            "ema200_slope_at_open": prev.get("ema_200_slope_1") if prev is not None else None,
            "ema200_source_bar_end": _iso(prior_bar_end),
            "ema200_band_low_at_open": band_lo,
            "ema200_band_high_at_open": band_hi,
            "ema200_half_width": hw,
            "touch_ema200_band": touch_ema,
            "close_vs_ema200_band": close_vs_ema,
            "ema200_slope_sign": (
                "rising" if prev is not None and float(prev.get("ema_200_slope_1") or 0) > 0
                else ("falling" if prev is not None and float(prev.get("ema_200_slope_1") or 0) < 0 else "flat")
            ),
            "dist_pool_ema200_atr": dist_pool_ema_atr,
            "dist_pool_ema200_pct": dist_pool_ema_pct,
        }
        state.ema_timeline.append(ema_row)

        ask_row = {
            "bar_close": _iso(bar_close),
            "entry_pool_id": ENTRY_POOL_ID,
            "status": pool_status,
            "lower_edge": entry_pool.lower_edge if entry_pool else None,
            "upper_edge": entry_pool.upper_edge if entry_pool else None,
            "pool_touch": pool_touch,
            "pool_half_at_close": pool_half,
            "max_penetration_above_upper": pool_penetration,
            "sweep_over_upper": sweep_over,
            "close_below_lower": close_under_pool,
            "price_high": m["high"],
            "dist_high_to_pool_upper": (m["high"] - entry_pool.upper_edge) if entry_pool else None,
        }
        state.ask_pool_timeline.append(ask_row)

        conf = {
            "bar_close": _iso(bar_close),
            "active_ask_pool": pool_status == "ACTIVE",
            "pool_overlaps_ema200": (
                entry_pool is not None
                and band_lo is not None
                and band_hi is not None
                and entry_pool.lower_edge <= band_hi
                and entry_pool.upper_edge >= band_lo
            ),
            "dist_pool_ema200_atr": dist_pool_ema_atr,
            "touch_pool_and_ema": pool_touch and touch_ema,
            "close_not_sustained_above": close_vs_ema in ("below", "inside") and (not entry_pool or m["close"] <= entry_pool.upper_edge),
        }
        state.confluence.append(conf)

        tick_rows.append(
            {
                "as_of": _iso(bar_close),
                "price": m["close"],
                "active_asks_near": [
                    {
                        "pool_id": p.pool_id,
                        "tf": p.timeframe,
                        "available_at": _iso(p.available_at),
                        "lower": p.lower_edge,
                        "upper": p.upper_edge,
                        "strength": p.strength,
                        "components": p.component_count,
                    }
                    for p in _active_ask_pools(pool_at, bar_close, m["high"])[:5]
                ],
            }
        )

    # --- Rejection definitions ---
    r1_candidates: list[dict[str, Any]] = []
    first_r1: dict[str, Any] | None = None
    for ema_r, c5, ask in zip(state.ema_timeline, state.candles_5m, state.ask_pool_timeline):
        band_lo = ema_r["ema200_band_low_at_open"]
        pool_lo = ask["lower_edge"]
        close = c5["close"]
        r1_pass = (
            ema_r["touch_ema200_band"]
            and ask["pool_touch"]
            and ask["status"] == "ACTIVE"
            and band_lo is not None
            and pool_lo is not None
            and (close < band_lo or close < pool_lo)
        )
        state.rejection_defs.append(
            {
                "definition": "R1_rejection_close",
                "bar_open": c5["bar_open"],
                "bar_close": c5["bar_close"],
                "condition_first_true_at": c5["bar_close"] if r1_pass else "",
                "pass": r1_pass,
                "high": c5["high"],
                "close": close,
                "ema_band_low": band_lo,
                "pool_lower": pool_lo,
            }
        )
        if r1_pass:
            cand = {**c5, **ema_r, **ask, "reaction_low": c5["low"], "reaction_high": c5["high"]}
            r1_candidates.append(cand)
            if first_r1 is None:
                first_r1 = cand

    # Primary rejection = highest wick among R1 candidates (manual ~06:50 refers to open 06:50 / close 06:55)
    rejection_candle = max(r1_candidates, key=lambda c: c["high"]) if r1_candidates else None

    # R2/R3/R4 after first R1
    r1_at = rejection_candle["bar_close"] if rejection_candle else None
    r2_at, r3_at, r4_at = None, None, None
    candles_1m_conf: list[dict[str, Any]] = []

    if rejection_candle:
        react_low = rejection_candle["reaction_low"]
        react_high = rejection_candle["reaction_high"]
        r1_close_ts = pd.Timestamp(rejection_candle["bar_close"])
        after_1m = df1[df1["open_time"] > r1_close_ts].copy()
        for _, r1 in after_1m.iterrows():
            c1_close = _utc_naive((r1["open_time"] + timedelta(minutes=1)).to_pydatetime())
            if float(r1["close"]) < react_low and r2_at is None:
                r2_at = _iso(c1_close)
                candles_1m_conf.append(
                    {
                        "confirmation_type": "R2_break_reaction_low",
                        "bar_open": _iso(r1["open_time"]),
                        "bar_close": _iso(c1_close),
                        "close": float(r1["close"]),
                        "reaction_low": react_low,
                    }
                )
                break

        after_r1_5m = [c for c in state.candles_5m if c["bar_open"] > rejection_candle["bar_open"]]
        for c in after_r1_5m:
            if c["candle_direction"] == "bearish" and c["close"] < react_low and r3_at is None:
                r3_at = c["bar_close"]
            if r4_at is None:
                ema_match = next(e for e in state.ema_timeline if e["bar_close"] == c["bar_close"])
                if ema_match["touch_ema200_band"] and c["close"] < (ema_match["ema200_band_low_at_open"] or 0):
                    r4_at = c["bar_close"]

    for name, ts, policy in [
        ("R2_break_reaction_low", r2_at, "1m_close_below_reaction_low"),
        ("R3_bearish_displacement", r3_at, "5m_bearish_close_below_reaction_low"),
        ("R4_failed_ema200_reclaim", r4_at, "5m_close_back_below_ema200"),
    ]:
        state.rejection_defs.append(
            {
                "definition": name,
                "condition_first_true_at": ts or "",
                "earliest_decision_at": ts or "",
                "entry_policy": policy,
                "lookahead_safe": bool(ts),
                "pass": bool(ts),
            }
        )

    # --- Entry variants ---
    primary_decision = None
    eligible_targets: list[dict] = []
    if rejection_candle:
        bar_close_ts = pd.Timestamp(rejection_candle["bar_close"])
        pool_at_dec = _pool_map(candles, bar_close_ts.to_pydatetime())
        entry_pool = pool_at_dec.get(ENTRY_POOL_ID)
        atr_dec = float(rejection_candle.get("atr_14") or 0)
        ema_hi = rejection_candle.get("ema200_band_high_at_open") or 0
        pool_upper = entry_pool.upper_edge if entry_pool else 0
        stop_ref, buf, *_ = _rejection_sl(
            rejection_high=rejection_candle["reaction_high"],
            pool_upper=pool_upper,
            ema_band_high=float(ema_hi),
            atr=atr_dec,
            tick=tick,
        )
        stop_loss = stop_ref + buf

        next_rows = df5[df5["open_time"] == bar_close_ts]
        next_open = float(next_rows.iloc[0]["open"]) if not next_rows.empty else None
        next_open_ts = _utc_naive(next_rows.iloc[0]["open_time"].to_pydatetime()) if not next_rows.empty else None

        variants = [
            ("A_market_on_rejection_close", bar_close_ts.to_pydatetime(), float(rejection_candle["close"])),
            ("B_next_5m_open", next_open_ts, next_open),
            ("C_1m_confirmation", None, None),
        ]
        if r2_at:
            r2_row = df1[df1["open_time"] + timedelta(minutes=1) == pd.Timestamp(r2_at)]
            if not r2_row.empty:
                variants[2] = ("C_1m_confirmation", pd.Timestamp(r2_at).to_pydatetime(), float(r2_row.iloc[0]["close"]))

        all_bids = [
            p
            for p in pool_at_dec.values()
            if p.side == "BID"
        ]
        for label, ent_at, ent_px in variants:
            if ent_at is None or ent_px is None or entry_pool is None:
                state.entry_variants.append({"variant": label, "executable": False, "reason": "missing_inputs"})
                continue
            tgt_rows = _rank_targets(ent_px, all_bids, entry_pool, atr_dec, ent_at)
            if not eligible_targets:
                eligible_targets = [{**t, "decision_context": label} for t in tgt_rows]
            sel = tgt_rows[0] if tgt_rows else None
            grr = sel["gross_rr"] if sel else None
            nrr = sel["net_rr"] if sel else None
            tp = sel and _stop_target_levels(
                direction="SHORT", symbol=SYMBOL, entry=ent_px, entry_pool=entry_pool,
                target_pool=next(p for p in all_bids if p.pool_id == sel["target_pool_id"]),
                atr=atr_dec, sweep_high=rejection_candle["reaction_high"], sweep_low=None,
            )[1]
            state.entry_variants.append(
                {
                    "variant": label,
                    "entry_at": _iso(ent_at),
                    "entry_price": ent_px,
                    "stop_reference": stop_ref,
                    "stop_buffer": buf,
                    "stop_loss": stop_loss,
                    "take_profit": tp,
                    "target_pool_id": sel["target_pool_id"] if sel else None,
                    "gross_rr": grr,
                    "net_rr": nrr,
                    "executable": bool(sel and grr and grr >= MIN_GROSS_RR),
                    "same_bar_ambiguity": label == "A_market_on_rejection_close",
                    "max_feature_timestamp": rejection_candle["bar_close"],
                    "lookahead_safe": True,
                }
            )
            if primary_decision is None and sel and grr and grr >= MIN_GROSS_RR:
                primary_decision = state.entry_variants[-1]

    # --- Structure change timeline ---
    if rejection_candle:
        r1_ts = pd.Timestamp(rejection_candle["bar_close"])
        sub5 = df5[df5["open_time"] >= r1_ts - timedelta(minutes=30)]
        prev_e9_slope = None
        for _, row in sub5.iterrows():
            bo = _iso(row["open_time"])
            bc = _iso(_bar_close(_utc_naive(row["open_time"].to_pydatetime())))
            e9s = row.get("ema_9_slope_1")
            events = []
            if float(row["close"]) < float(row.get("ema_200") or 1e9):
                events.append("close_below_ema200")
            if float(row["close"]) < float(row.get("ema_59") or 1e9):
                events.append("below_ema59")
            if prev_e9_slope is not None and e9s is not None and float(e9s) < 0 <= float(prev_e9_slope):
                events.append("ema9_slope_turned_negative")
            if float(row.get("ema_9") or 0) < float(row.get("ema_20") or 1e9):
                events.append("ema9_below_ema20")
            if float(row["close"]) < rejection_candle["reaction_low"]:
                events.append("break_reaction_low")
            if events:
                state.structure_events.append({"bar_close": bc, "events": events})
            prev_e9_slope = e9s

    # --- Verdict ---
    any_ema_touch = any(e["touch_ema200_band"] for e in state.ema_timeline)
    pool_active_at_rej = rejection_candle and rejection_candle.get("status") == "ACTIVE"
    verdict = VERDICT_MANUAL
    if not any_ema_touch:
        verdict = VERDICT_EMA_NOT_TOUCHED
    elif rejection_candle and not pool_active_at_rej:
        verdict = VERDICT_POOL_INACTIVE
    elif rejection_candle and not primary_decision:
        verdict = VERDICT_NO_TARGET if eligible_targets else VERDICT_NO_TARGET
    elif primary_decision:
        verdict = VERDICT_CONFIRMED
    elif rejection_candle:
        verdict = VERDICT_LATE if r2_at else VERDICT_MANUAL

    # --- Outcome (frozen plan) ---
    outcome = {}
    if primary_decision:
        outcome = _outcome_audit(
            df1,
            decision_at=pd.Timestamp(primary_decision["entry_at"]).to_pydatetime(),
            entry=float(primary_decision["entry_price"]),
            stop=float(primary_decision["stop_loss"]),
            target=float(primary_decision["take_profit"]),
            horizon_end=WINDOW_END + timedelta(hours=1),
        )

    episode_id = (
        f"A_PLUS_EMA200_POOL_REJECTION_SHORT:{ENTRY_POOL_ID}:{rejection_candle['bar_close']}"
        if rejection_candle
        else None
    )
    signal_id = hashlib.sha256(f"{SYMBOL}|{episode_id}|{primary_decision}".encode()).hexdigest()[:24] if episode_id and primary_decision else None

    decision_result = {
        "verdict": verdict,
        "rejection_candle_primary": rejection_candle,
        "rejection_candle_first_r1": first_r1,
        "r1_candidate_count": len(r1_candidates),
        "pullback_high_bar": pullback_high_bar,
        "last_upward_impulse_before_peak": last_up_bar,
        "manual_0650_bar_interpretation": {
            "bar_open_0645_close_0650": next((c for c in state.candles_5m if c["bar_open"] == "2026-08-28T06:45:00"), None),
            "bar_open_0650_close_0655": next((c for c in state.candles_5m if c["bar_open"] == "2026-08-28T06:50:00"), None),
            "note": "Manual '06:50 candle' matches bar_open 06:50 / bar_close 06:55 (highest pullback wick 0.08831)",
        },
        "primary_decision": primary_decision,
        "episode_id": episode_id,
        "signal_id": signal_id,
        "old_pullback_episode": OLD_PULLBACK_EPISODE,
        "old_pullback_revived": False,
        "setup_type": "A_PLUS_EMA200_POOL_REJECTION_SHORT",
        "outcome": outcome,
        "orderflow": {"status": "UNAVAILABLE", "reason": "no_causal_ob_feed_in_scope"},
    }

    manifest = {
        "run_id": run_id,
        "symbol": SYMBOL,
        "window_start": _iso(WINDOW_START),
        "window_end": _iso(WINDOW_END),
        "verdict": verdict,
        "entry_pool_id": ENTRY_POOL_ID,
        "pool_time_semantics": "closed_confirmation_bar_v2",
        "decision_result_summary": {
            "rejection_bar_close_primary": rejection_candle["bar_close"] if rejection_candle else None,
            "rejection_bar_open_primary": rejection_candle["bar_open"] if rejection_candle else None,
            "first_r1_bar_close": first_r1["bar_close"] if first_r1 else None,
            "primary_entry_at": primary_decision["entry_at"] if primary_decision else None,
            "episode_id": episode_id,
        },
    }

    _write_csv(out_dir / "candles_5m.csv", state.candles_5m)
    _write_csv(out_dir / "candles_1m_confirmation.csv", candles_1m_conf)
    _write_csv(out_dir / "ema200_timeline.csv", state.ema_timeline)
    _write_csv(out_dir / "ask_pool_timeline.csv", state.ask_pool_timeline)
    _write_csv(out_dir / "confluence_timeline.csv", state.confluence)
    _write_csv(out_dir / "rejection_definitions.csv", state.rejection_defs)
    _write_csv(out_dir / "eligible_targets.csv", eligible_targets)
    _write_csv(out_dir / "entry_sl_tp_variants.csv", state.entry_variants)
    _write_csv(out_dir / "optional_orderflow_context.csv", [{"status": "UNAVAILABLE"}])
    _write_csv(out_dir / "outcome_audit.csv", [outcome] if outcome else [])
    _write_csv(out_dir / "structure_change_timeline.csv", state.structure_events)
    (out_dir / "decision_result.json").write_text(json.dumps(decision_result, indent=2, default=str), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out_dir / "methodology.md").write_text(_methodology(), encoding="utf-8")
    (out_dir / "report.md").write_text(_report(decision_result, manifest), encoding="utf-8")

    return {"run_id": run_id, "out_dir": str(out_dir), "verdict": verdict, "decision_result": decision_result}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def _methodology() -> str:
    return """# DOGE EMA200 / Ask-pool rejection audit

- Window: 2026-08-28 06:30–07:15 UTC; warmup from 2025-08-25.
- Pool semantics: v2 closed confirmation bar; `available_at <= decision_at`.
- EMA200 band at bar open: prior closed 5m bar (`ema200_source_bar_end <= bar_open`).
- Band half-width: `max(0.15*ATR, 5*tick)` (EZM default, not DOGE-tuned).
- Rejection definitions R1–R4 tested separately; no outcome-based thresholds.
- SL: `max(rejection_high, pool_upper, ema200_band_high) + buffer`.
- TP: unchanged `_select_target_below` contract.
- Separate setup type from `A_PLUS_PULLBACK_SHORT`; old 04:15 plan not revived.
"""


def _report(decision: dict[str, Any], manifest: dict[str, Any]) -> str:
    rc = decision.get("rejection_candle_primary") or {}
    pd_ = decision.get("primary_decision") or {}
    lines = [
        f"# {decision.get('verdict')}",
        "",
        "## Primary rejection candle (max-high R1)",
        f"- bar_open: {rc.get('bar_open')}",
        f"- bar_close: {rc.get('bar_close')}",
        f"- high: {rc.get('high')} / close: {rc.get('close')}",
        "",
        "## Manual 06:50 interpretation",
        json.dumps(decision.get("manual_0650_bar_interpretation") or {}, indent=2, default=str),
        "",
        "## Primary decision",
        json.dumps(pd_, indent=2, default=str),
        "",
        "## Outcome",
        json.dumps(decision.get("outcome") or {}, indent=2, default=str),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps(run_audit(), indent=2, default=str))
