"""Causal feature extractors at T0–T3 (aggregate OB proxy + trades + OI/liq)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import T3_WINDOWS_1M, T3_WINDOWS_SEC


def _slice(df: pd.DataFrame, tcol: str, a, b) -> pd.DataFrame:
    if df is None or df.empty or pd.isna(a) or pd.isna(b):
        return df.iloc[0:0] if df is not None else pd.DataFrame()
    return df[(df[tcol] >= a) & (df[tcol] < b)]


def _safe_mean(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def _safe_sum(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.sum()) if len(s) else None


def _safe_last(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def pool_side_depth(ob: pd.DataFrame, side: str) -> pd.Series:
    """BID pool → bid depth is pool-side; ASK → ask depth."""
    if ob.empty:
        return pd.Series(dtype=float)
    if side == "BID":
        return ob["bid_qty_l50"]
    return ob["ask_qty_l50"]


def opposite_depth(ob: pd.DataFrame, side: str) -> pd.Series:
    if ob.empty:
        return pd.Series(dtype=float)
    if side == "BID":
        return ob["ask_qty_l50"]
    return ob["bid_qty_l50"]


def aggression_toward_pool(trades: pd.DataFrame, side: str) -> tuple[float | None, float | None]:
    """Aggressive flow hitting the pool: BID←sells, ASK←buys."""
    if trades.empty:
        return None, None
    if side == "BID":
        return _safe_sum(trades["sell_notional"]), _safe_sum(trades["buy_notional"])
    return _safe_sum(trades["buy_notional"]), _safe_sum(trades["sell_notional"])


def extract_ob_features(
    ob: pd.DataFrame,
    *,
    side: str,
    lower: float,
    upper: float,
    t_pre_a,
    t_pre_b,
    t_touch_a,
    t_touch_b,
    t_post_a,
    t_post_b,
) -> dict[str, Any]:
    """Aggregate-proxy OB features — never claim per-level L2."""
    out: dict[str, Any] = {
        "ob_source_kind": "AGGREGATE_PROXY",
        "ob_per_level_raw": False,
    }
    windows = {
        "pre": (t_pre_a, t_pre_b),
        "touch": (t_touch_a, t_touch_b),
        "post": (t_post_a, t_post_b),
    }
    for wname, (a, b) in windows.items():
        sl = _slice(ob, "bucket_start", a, b)
        if sl.empty:
            out[f"{wname}_ob_status"] = "MISSING"
            continue
        out[f"{wname}_ob_status"] = "VALID"
        ps = pool_side_depth(sl, side)
        opp = opposite_depth(sl, side)
        out[f"{wname}_pool_depth_mean"] = _safe_mean(ps)
        out[f"{wname}_opp_depth_mean"] = _safe_mean(opp)
        out[f"{wname}_imbalance_l50_mean"] = _safe_mean(sl["imbalance_l50"])
        out[f"{wname}_spread_bps_mean"] = _safe_mean(sl["spread_bps"])
        # replenishment / depletion via adds/removes on pool side
        if side == "BID":
            add, rem = sl["bid_qty_added"], sl["bid_qty_removed"]
            wall_px, wall_qty, wall_dist = sl["bid_wall_price"], sl["bid_wall_qty"], sl["bid_wall_bps_dist"]
        else:
            add, rem = sl["ask_qty_added"], sl["ask_qty_removed"]
            wall_px, wall_qty, wall_dist = sl["ask_wall_price"], sl["ask_wall_qty"], sl["ask_wall_bps_dist"]
        add_s, rem_s = _safe_sum(add), _safe_sum(rem)
        out[f"{wname}_depth_added"] = add_s
        out[f"{wname}_depth_removed"] = rem_s
        if add_s is not None and rem_s is not None and (add_s + rem_s) > 0:
            out[f"{wname}_cancel_to_add"] = rem_s / (add_s + 1e-12)
            out[f"{wname}_net_replenishment"] = add_s - rem_s
        else:
            out[f"{wname}_cancel_to_add"] = None
            out[f"{wname}_net_replenishment"] = None
        out[f"{wname}_wall_qty_mean"] = _safe_mean(wall_qty)
        out[f"{wname}_wall_bps_dist_mean"] = _safe_mean(wall_dist)
        # wall near pool edge
        mid = (lower + upper) / 2.0
        if wall_px.notna().any() and mid > 0:
            dist_frac = (wall_px.astype(float) - mid).abs() / mid
            out[f"{wname}_wall_near_pool"] = bool((dist_frac < 0.002).any())
        else:
            out[f"{wname}_wall_near_pool"] = None
        out[f"{wname}_ofi_sum"] = _safe_sum(sl["ofi"])
        out[f"{wname}_mid_change_sum"] = _safe_sum(sl["mid_price_change"])

    # derived causal flags using pre vs post only (both must exist)
    if out.get("pre_pool_depth_mean") and out.get("post_pool_depth_mean"):
        out["depth_replenishment_flag"] = out["post_pool_depth_mean"] > out["pre_pool_depth_mean"] * 1.05
        out["depth_depletion_flag"] = out["post_pool_depth_mean"] < out["pre_pool_depth_mean"] * 0.90
    else:
        out["depth_replenishment_flag"] = None
        out["depth_depletion_flag"] = None
    if out.get("pre_imbalance_l50_mean") is not None and out.get("post_imbalance_l50_mean") is not None:
        # book flip: imbalance sign change favoring pool defense
        pre_i, post_i = out["pre_imbalance_l50_mean"], out["post_imbalance_l50_mean"]
        if side == "BID":
            out["book_flip_toward_defense"] = pre_i < 0 and post_i > pre_i
        else:
            out["book_flip_toward_defense"] = pre_i > 0 and post_i < pre_i
    else:
        out["book_flip_toward_defense"] = None
    if out.get("post_net_replenishment") is not None:
        out["wall_persistence_proxy"] = out["post_net_replenishment"] >= 0 and not bool(
            out.get("depth_depletion_flag")
        )
    else:
        out["wall_persistence_proxy"] = None
    return out


def extract_trade_features(
    trades: pd.DataFrame,
    candles: pd.DataFrame,
    *,
    side: str,
    t_pre_a,
    t_pre_b,
    t_touch_a,
    t_touch_b,
    t_post_a,
    t_post_b,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    windows = {
        "pre": (t_pre_a, t_pre_b),
        "touch": (t_touch_a, t_touch_b),
        "post": (t_post_a, t_post_b),
    }
    for wname, (a, b) in windows.items():
        sl = _slice(trades, "second", a, b)
        if sl.empty:
            out[f"{wname}_trades_status"] = "MISSING"
            continue
        out[f"{wname}_trades_status"] = "VALID"
        hit, counter = aggression_toward_pool(sl, side)
        out[f"{wname}_agg_hit_notional"] = hit
        out[f"{wname}_agg_counter_notional"] = counter
        out[f"{wname}_delta_notional"] = _safe_sum(sl["delta_notional"])
        out[f"{wname}_trades_per_sec"] = (
            float(len(sl)) / max((b - a).total_seconds(), 1.0) if pd.notna(a) and pd.notna(b) else None
        )
        out[f"{wname}_notional_per_sec"] = (
            ((_safe_sum(sl["buy_notional"]) or 0) + (_safe_sum(sl["sell_notional"]) or 0))
            / max((b - a).total_seconds(), 1.0)
            if pd.notna(a) and pd.notna(b)
            else None
        )
        out[f"{wname}_avg_trade_notional"] = _safe_mean(sl["avg_trade_notional"])
        # large trade share vs p95 within window
        if "p95_trade_notional" in sl and sl["avg_trade_notional"].notna().any():
            thr = float(sl["p95_trade_notional"].median())
            # approximate: seconds with avg above thr
            out[f"{wname}_large_trade_sec_share"] = float((sl["avg_trade_notional"] >= thr).mean()) if thr == thr else None
        else:
            out[f"{wname}_large_trade_sec_share"] = None

        # impact: price continuation vs aggressive notional
        csl = _slice(candles, "open_time", a, b + pd.Timedelta(minutes=1))
        if not csl.empty and hit and hit > 0:
            if side == "BID":
                # downside continuation = drop in low/close
                px0 = float(csl.iloc[0]["open"])
                px1 = float(csl.iloc[-1]["close"])
                continuation = max(0.0, (px0 - px1) / px0)
            else:
                px0 = float(csl.iloc[0]["open"])
                px1 = float(csl.iloc[-1]["close"])
                continuation = max(0.0, (px1 - px0) / px0)
            out[f"{wname}_impact_per_agg"] = continuation / (hit / 1e6)  # per $1m
            out[f"{wname}_price_continuation"] = continuation
        else:
            out[f"{wname}_impact_per_agg"] = None
            out[f"{wname}_price_continuation"] = None

    # absorption / compression / flip
    if out.get("touch_agg_hit_notional") and out.get("touch_price_continuation") is not None:
        out["absorption_flag"] = (
            out["touch_agg_hit_notional"] > 0
            and (out["touch_price_continuation"] or 0) < 0.0005
        )
    else:
        out["absorption_flag"] = None

    if out.get("pre_impact_per_agg") is not None and out.get("post_impact_per_agg") is not None:
        out["impact_compression_flag"] = out["post_impact_per_agg"] < out["pre_impact_per_agg"] * 0.7
    else:
        out["impact_compression_flag"] = None

    if out.get("touch_delta_notional") is not None and out.get("post_delta_notional") is not None:
        # flow flip: hit aggression slows / reverses after touch
        if side == "BID":
            # sell-heavy delta negative; flip toward positive
            out["flow_flip_flag"] = out["touch_delta_notional"] < 0 and out["post_delta_notional"] > out["touch_delta_notional"]
        else:
            out["flow_flip_flag"] = out["touch_delta_notional"] > 0 and out["post_delta_notional"] < out["touch_delta_notional"]
    else:
        out["flow_flip_flag"] = None

    if out.get("pre_agg_hit_notional") and out.get("post_agg_hit_notional") is not None:
        out["flow_deceleration_flag"] = out["post_agg_hit_notional"] < out["pre_agg_hit_notional"] * 0.7
    else:
        out["flow_deceleration_flag"] = None

    return out


def extract_oi_liq_features(
    oi: pd.DataFrame,
    liq: pd.DataFrame,
    *,
    side: str,
    t_pre_a,
    t_pre_b,
    t_touch_a,
    t_post_b,
) -> dict[str, Any]:
    out: dict[str, Any] = {"oi_status": "MISSING", "liq_status": "MISSING"}
    oi_pre = _slice(oi, "bucket_time", t_pre_a, t_pre_b)
    oi_post = _slice(oi, "bucket_time", t_touch_a, t_post_b)
    if not oi_pre.empty and not oi_post.empty:
        out["oi_status"] = "VALID"
        oi0 = _safe_mean(oi_pre["open_interest"])
        oi1 = _safe_mean(oi_post["open_interest"])
        out["oi_pre_mean"] = oi0
        out["oi_post_mean"] = oi1
        if oi0 and oi1:
            out["oi_change_frac"] = (oi1 - oi0) / oi0
            out["oi_rise_on_approach"] = oi0 > 0 and (oi1 > oi0)  # weak; approach vs post
            out["oi_drop_on_sweep"] = oi1 < oi0 * 0.999
        else:
            out["oi_change_frac"] = None
            out["oi_rise_on_approach"] = None
            out["oi_drop_on_sweep"] = None
    elif oi is not None and not oi.empty:
        out["oi_status"] = "OUT_OF_RANGE"
        out["oi_change_frac"] = None
        out["oi_note"] = "OI rows exist for symbol but not in episode windows — not interpreted as zero"
    else:
        out["oi_change_frac"] = None
        out["oi_note"] = "OI_MISSING — not interpreted as zero"

    liq_w = _slice(liq, "event_time", t_pre_a, t_post_b)
    if liq_w.empty:
        out["liq_status"] = "EMPTY_SLICE"
        out["liq_note"] = "EMPTY_TABLE_SLICE_IN_WINDOW — not proof of zero liquidations"
        out["liq_burst_flag"] = None
        out["liq_flush_toward_pool"] = None
        out["liq_notional_hit"] = None
    else:
        out["liq_status"] = "VALID"
        # BID defense: long liquidations hit bids; ASK: short liquidations
        if side == "BID":
            hit = liq_w[liq_w["side"].astype(str).str.lower().isin(["long", "buy", "longs"])]
        else:
            hit = liq_w[liq_w["side"].astype(str).str.lower().isin(["short", "sell", "shorts"])]
        # fallback: use all if side labels differ
        if hit.empty:
            hit = liq_w
        notional = _safe_sum(hit["notional"])
        out["liq_notional_hit"] = notional
        out["liq_burst_flag"] = bool(notional is not None and notional > 0 and len(hit) >= 3)
        out["liq_flush_toward_pool"] = bool(notional is not None and notional > 0)
    return out


def edge_reclaim_features(
    candles: pd.DataFrame,
    *,
    side: str,
    lower: float,
    upper: float,
    t2,
    t3,
) -> dict[str, Any]:
    """Near/far edge reclaim using closed 1m bars only up to t3."""
    out = {
        "near_edge_reclaim": None,
        "far_edge_reclaim": None,
        "edge_reclaim_status": "MISSING",
    }
    if pd.isna(t2) or pd.isna(t3):
        return out
    sl = _slice(candles, "open_time", t2, t3)
    if sl.empty:
        return out
    out["edge_reclaim_status"] = "VALID"
    near = upper if side == "BID" else lower
    far = lower if side == "BID" else upper
    closes = sl["close"].astype(float)
    if side == "BID":
        out["near_edge_reclaim"] = bool((closes > near).any())
        out["far_edge_reclaim"] = bool((closes > far).any())  # fully back through pool
    else:
        out["near_edge_reclaim"] = bool((closes < near).any())
        out["far_edge_reclaim"] = bool((closes < far).any())
    return out


def build_checkpoint_row(ep: pd.Series, t3_sec: int | None, t3_1m: int | None) -> dict[str, Any]:
    t0 = pd.Timestamp(ep["known_at"])
    t1 = pd.Timestamp(ep["approach_at"]) if pd.notna(ep.get("approach_at")) else pd.NaT
    t2 = pd.Timestamp(ep["first_touch_at"]) if pd.notna(ep.get("first_touch_at")) else pd.NaT
    if t3_sec is not None and pd.notna(t2):
        t3 = t2 + pd.Timedelta(seconds=int(t3_sec))
        decision_label = f"T3_{t3_sec}s"
    elif t3_1m is not None and pd.notna(t2):
        # closed 1m bars: decision after N completed minutes
        t3 = (t2.floor("min") + pd.Timedelta(minutes=int(t3_1m) + 1))
        decision_label = f"T3_{t3_1m}m_closed"
    else:
        t3 = pd.NaT
        decision_label = "T3_undefined"
    return {
        "episode_id": ep["episode_id"],
        "symbol": ep["symbol"],
        "side": ep["side"],
        "timeframe": ep["timeframe"],
        "T0_known_at": t0.isoformat() if pd.notna(t0) else None,
        "T1_approach_at": None if pd.isna(t1) else t1.isoformat(),
        "T2_first_touch_at": None if pd.isna(t2) else t2.isoformat(),
        "T3_decision_at": None if pd.isna(t3) else t3.isoformat(),
        "T3_window": decision_label,
        "T4_label": ep.get("label_primary"),
    }
