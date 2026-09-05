"""Forward directional outcomes and post-reclaim wall-follow analysis."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim import (
    HORIZONS_S,
    MISSING,
    WALL_FOLLOW_S,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.rich_samples import RichSample
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import (
    band_around,
    in_band,
    median,
    pctile,
    side_endpoint_pct,
    side_mfe_pct,
    spearman,
)


def _path_mids(samples: list[RichSample], start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    return [(s.ts_ms, s.mid) for s in samples if start_ms < s.ts_ms <= end_ms]


def compute_forward_outcomes(
    events: list[dict[str, Any]],
    samples_by: dict[str, list[RichSample]],
    *,
    data_end_ms: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in events:
        samples = samples_by.get(ev["symbol"], [])
        entry_at = int(ev["entry_at"])
        entry_px = float(ev["entry_price"])
        direction = ev["direction"]
        for h in HORIZONS_S:
            t1 = entry_at + h * 1000
            closed = t1 <= data_end_ms
            if not closed:
                rows.append(
                    {
                        "event_id": ev["event_id"],
                        "symbol": ev["symbol"],
                        "direction": direction,
                        "horizon": h,
                        "entry_at": entry_at,
                        "entry_price": entry_px,
                        "mfe_pct": MISSING,
                        "endpoint_return_pct": MISSING,
                        "time_to_mfe_seconds": MISSING,
                        "horizon_closed": False,
                        "outcome_source": MISSING,
                        "outcome_missing_reason": "horizon_beyond_available_data",
                    }
                )
                continue
            path = _path_mids(samples, entry_at, t1)
            if not path:
                rows.append(
                    {
                        "event_id": ev["event_id"],
                        "symbol": ev["symbol"],
                        "direction": direction,
                        "horizon": h,
                        "entry_at": entry_at,
                        "entry_price": entry_px,
                        "mfe_pct": MISSING,
                        "endpoint_return_pct": MISSING,
                        "time_to_mfe_seconds": MISSING,
                        "horizon_closed": True,
                        "outcome_source": MISSING,
                        "outcome_missing_reason": "no_samples_in_horizon",
                    }
                )
                continue
            mids = [m for _, m in path]
            mfe, idx = side_mfe_pct(entry_px, mids, direction)
            ttm = None if idx is None else (path[idx][0] - entry_at) / 1000.0
            ep = side_endpoint_pct(entry_px, mids[-1], direction)
            rows.append(
                {
                    "event_id": ev["event_id"],
                    "symbol": ev["symbol"],
                    "direction": direction,
                    "horizon": h,
                    "entry_at": entry_at,
                    "entry_price": entry_px,
                    "mfe_pct": mfe,
                    "endpoint_return_pct": ep,
                    "time_to_mfe_seconds": ttm,
                    "horizon_closed": True,
                    "outcome_source": "rich_l2_mid",
                    "outcome_missing_reason": None,
                }
            )
    return rows


def summarize_forward(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for h in HORIZONS_S:
        for direction in ("LONG", "SHORT", "ALL"):
            sub = [
                r
                for r in rows
                if r["horizon"] == h
                and r.get("horizon_closed") is True
                and r.get("mfe_pct") not in (None, MISSING)
                and (direction == "ALL" or r["direction"] == direction)
            ]
            if not sub:
                out.append(
                    {
                        "horizon": h,
                        "direction": direction,
                        "n": 0,
                        "mean_mfe_pct": MISSING,
                        "median_mfe_pct": MISSING,
                        "mean_endpoint_return_pct": MISSING,
                        "median_endpoint_return_pct": MISSING,
                        "positive_endpoint_rate": MISSING,
                    }
                )
                continue
            mfes = [float(r["mfe_pct"]) for r in sub]
            eps = [float(r["endpoint_return_pct"]) for r in sub if r.get("endpoint_return_pct") not in (None, MISSING)]
            out.append(
                {
                    "horizon": h,
                    "direction": direction,
                    "n": len(sub),
                    "mean_mfe_pct": sum(mfes) / len(mfes),
                    "median_mfe_pct": sorted(mfes)[len(mfes) // 2],
                    "p25_mfe_pct": pctile(mfes, 0.25),
                    "p75_mfe_pct": pctile(mfes, 0.75),
                    "mean_endpoint_return_pct": (sum(eps) / len(eps)) if eps else MISSING,
                    "median_endpoint_return_pct": sorted(eps)[len(eps) // 2] if eps else MISSING,
                    "positive_endpoint_rate": (sum(1 for x in eps if x > 0) / len(eps)) if eps else MISSING,
                }
            )
    return out


def _major_from_sample(s: RichSample, side: str) -> dict[str, Any]:
    w = s.bid_wall if side == "BID" else s.ask_wall
    if w is None:
        return {
            "price": None,
            "notional": None,
            "relative_size": None,
            "percentile": None,
            "distance_bps": None,
            "present": False,
        }
    dist = abs(w.price - s.mid) / s.mid * 10000.0 if s.mid > 0 else None
    return {
        "price": w.price,
        "notional": w.notional,
        "relative_size": w.relative_size,
        "percentile": None,  # filled only at entry context if needed
        "distance_bps": dist,
        "present": True,
    }


def build_wall_follow(
    events: list[dict[str, Any]],
    samples_by: dict[str, list[RichSample]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """1s dynamics over entry→+60s and feature row per event."""
    dynamics: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []

    for ev in events:
        samples = samples_by.get(ev["symbol"], [])
        entry_at = int(ev["entry_at"])
        end_at = entry_at + WALL_FOLLOW_S * 1000
        direction = ev["direction"]
        path = [s for s in samples if entry_at <= s.ts_ms <= end_at]
        if not path:
            features.append(
                {
                    "event_id": ev["event_id"],
                    "symbol": ev["symbol"],
                    "direction": direction,
                    "wall_follow_decision_at": end_at,
                    "quality_status": "MISSING_FOLLOW_PATH",
                }
            )
            continue

        # entry snapshot
        s0 = path[0]
        bid0 = _major_from_sample(s0, "BID")
        ask0 = _major_from_sample(s0, "ASK")

        # per ~1s (use samples near 1s grid)
        last_bid_p = bid0["price"]
        last_ask_p = ask0["price"]
        bid_seen = bid0["present"]
        ask_seen = ask0["present"]
        bid_series_p: list[float] = []
        ask_series_p: list[float] = []
        bid_series_n: list[float] = []
        ask_series_n: list[float] = []
        bid_series_rel: list[float] = []
        ask_series_rel: list[float] = []
        bid_series_dist: list[float] = []
        ask_series_dist: list[float] = []

        for s in path:
            # emit at most one row per second bucket
            if dynamics and dynamics[-1]["event_id"] == ev["event_id"] and dynamics[-1]["ts"] // 1000 == s.ts_ms // 1000:
                continue
            b = _major_from_sample(s, "BID")
            a = _major_from_sample(s, "ASK")
            bid_pulled = bid_seen and not b["present"]
            ask_pulled = ask_seen and not a["present"]
            bid_reapp = (not bid_seen) and b["present"]
            ask_reapp = (not ask_seen) and a["present"]
            if b["present"]:
                bid_seen = True
                last_bid_p = b["price"]
                bid_series_p.append(float(b["price"]))
                bid_series_n.append(float(b["notional"]))
                if b["relative_size"] is not None:
                    bid_series_rel.append(float(b["relative_size"]))
                if b["distance_bps"] is not None:
                    bid_series_dist.append(float(b["distance_bps"]))
            if a["present"]:
                ask_seen = True
                last_ask_p = a["price"]
                ask_series_p.append(float(a["price"]))
                ask_series_n.append(float(a["notional"]))
                if a["relative_size"] is not None:
                    ask_series_rel.append(float(a["relative_size"]))
                if a["distance_bps"] is not None:
                    ask_series_dist.append(float(a["distance_bps"]))

            dynamics.append(
                {
                    "event_id": ev["event_id"],
                    "ts": s.ts_ms,
                    "mid_price": s.mid,
                    "best_bid": s.best_bid,
                    "best_ask": s.best_ask,
                    "major_bid_wall_price": b["price"] if b["present"] else MISSING,
                    "major_bid_wall_notional": b["notional"] if b["present"] else MISSING,
                    "major_bid_wall_relative_size": b["relative_size"] if b["present"] else MISSING,
                    "major_bid_wall_size_percentile": MISSING,
                    "major_bid_wall_distance_bps": b["distance_bps"] if b["present"] else MISSING,
                    "major_ask_wall_price": a["price"] if a["present"] else MISSING,
                    "major_ask_wall_notional": a["notional"] if a["present"] else MISSING,
                    "major_ask_wall_relative_size": a["relative_size"] if a["present"] else MISSING,
                    "major_ask_wall_size_percentile": MISSING,
                    "major_ask_wall_distance_bps": a["distance_bps"] if a["present"] else MISSING,
                    "bid_wall_present": b["present"],
                    "ask_wall_present": a["present"],
                    "bid_wall_pulled": bid_pulled,
                    "ask_wall_pulled": ask_pulled,
                    "bid_wall_reappeared": bid_reapp,
                    "ask_wall_reappeared": ask_reapp,
                    "source_quality": "per_level_mutable_book",
                    "carried_forward": s.carried_forward,
                }
            )

        # time-weighted median of last 10 genuine seconds of the 60s window vs entry
        def _tw_med(xs: list[float]) -> float | None:
            if not xs:
                return None
            tail = xs[-10:] if len(xs) >= 10 else xs
            return median(tail)

        bid_p_med = _tw_med(bid_series_p)
        ask_p_med = _tw_med(ask_series_p)
        bid_n_med = _tw_med(bid_series_n)
        ask_n_med = _tw_med(ask_series_n)
        bid_rel_med = _tw_med(bid_series_rel)
        ask_rel_med = _tw_med(ask_series_rel)
        bid_d_med = _tw_med(bid_series_dist)
        ask_d_med = _tw_med(ask_series_dist)

        def _chg_pct(a: float | None, b: float | None) -> Any:
            if a is None or b is None or a == 0:
                return MISSING
            return (b / a - 1.0) * 100.0

        def _mig_bps(p0: float | None, p1: float | None, mid0: float) -> Any:
            if p0 is None or p1 is None or mid0 <= 0:
                return MISSING
            return (p1 - p0) / mid0 * 10000.0

        bid_mig = _mig_bps(bid0["price"], bid_p_med, s0.mid)
        ask_mig = _mig_bps(ask0["price"], ask_p_med, s0.mid)
        corridor0 = None
        if bid0["price"] is not None and ask0["price"] is not None and s0.mid > 0:
            corridor0 = (ask0["price"] - bid0["price"]) / s0.mid * 10000.0
        corridor1 = None
        if bid_p_med is not None and ask_p_med is not None and s0.mid > 0:
            corridor1 = (ask_p_med - bid_p_med) / s0.mid * 10000.0

        support_side = "BID" if direction == "LONG" else "ASK"
        if support_side == "BID":
            sup0_n, opp0_n = bid0["notional"], ask0["notional"]
            sup1_n, opp1_n = bid_n_med, ask_n_med
            # Long: upward migration positive (raw)
            sup_mig = bid_mig
            opp_mig = ask_mig
            both_follow = (
                bid_mig not in (None, MISSING)
                and ask_mig not in (None, MISSING)
                and float(bid_mig) > 0
                and float(ask_mig) > 0
            )
            sup_present = bid_p_med is not None
            opp_present = ask_p_med is not None
        else:
            sup0_n, opp0_n = ask0["notional"], bid0["notional"]
            sup1_n, opp1_n = ask_n_med, bid_n_med
            # Short: downward migration positive
            sup_mig = MISSING if ask_mig in (None, MISSING) else -float(ask_mig)
            opp_mig = MISSING if bid_mig in (None, MISSING) else -float(bid_mig)
            both_follow = (
                bid_mig not in (None, MISSING)
                and ask_mig not in (None, MISSING)
                and float(bid_mig) < 0
                and float(ask_mig) < 0
            )
            sup_present = ask_p_med is not None
            opp_present = bid_p_med is not None

        strength_delta = MISSING
        if (
            sup0_n not in (None, 0)
            and opp0_n not in (None, 0)
            and sup1_n not in (None, MISSING)
            and opp1_n not in (None, MISSING)
            and float(sup0_n) > 0
            and float(opp0_n) > 0
            and float(sup1_n) > 0
            and float(opp1_n) > 0
        ):
            strength_delta = math.log(float(sup1_n) / float(sup0_n)) - math.log(
                float(opp1_n) / float(opp0_n)
            )

        corridor_trans = MISSING
        if corridor0 is not None and corridor1 is not None:
            # midpoint of corridor migration in direction
            if bid_p_med is not None and ask_p_med is not None and bid0["price"] is not None and ask0["price"] is not None:
                mid0c = 0.5 * (float(bid0["price"]) + float(ask0["price"]))
                mid1c = 0.5 * (bid_p_med + ask_p_med)
                raw = (mid1c - mid0c) / s0.mid * 10000.0
                corridor_trans = raw if direction == "LONG" else -raw

        features.append(
            {
                "event_id": ev["event_id"],
                "symbol": ev["symbol"],
                "direction": direction,
                "wall_follow_decision_at": end_at,
                "bid_wall_migration_bps_60s": bid_mig,
                "ask_wall_migration_bps_60s": ask_mig,
                "bid_wall_notional_change_pct_60s": _chg_pct(bid0["notional"], bid_n_med),
                "ask_wall_notional_change_pct_60s": _chg_pct(ask0["notional"], ask_n_med),
                "bid_wall_relative_strength_change_60s": _chg_pct(bid0["relative_size"], bid_rel_med)
                if bid0["relative_size"] is not None
                else MISSING,
                "ask_wall_relative_strength_change_60s": _chg_pct(ask0["relative_size"], ask_rel_med)
                if ask0["relative_size"] is not None
                else MISSING,
                "bid_wall_distance_change_bps_60s": (
                    MISSING
                    if bid0["distance_bps"] is None or bid_d_med is None
                    else bid_d_med - float(bid0["distance_bps"])
                ),
                "ask_wall_distance_change_bps_60s": (
                    MISSING
                    if ask0["distance_bps"] is None or ask_d_med is None
                    else ask_d_med - float(ask0["distance_bps"])
                ),
                "corridor_width_at_entry_bps": corridor0 if corridor0 is not None else MISSING,
                "corridor_width_after_60s_bps": corridor1 if corridor1 is not None else MISSING,
                "corridor_width_change_bps": (
                    MISSING if corridor0 is None or corridor1 is None else corridor1 - corridor0
                ),
                "both_walls_follow_price_direction": both_follow,
                "support_wall_present_after_60s": sup_present,
                "opposing_wall_present_after_60s": opp_present,
                "directional_wall_strength_delta": strength_delta,
                "directional_support_migration_bps": sup_mig,
                "directional_opposing_migration_bps": opp_mig,
                "directional_corridor_translation_bps": corridor_trans,
                "entry_support_wall_notional": sup0_n if sup0_n is not None else MISSING,
                "entry_opposing_wall_notional": opp0_n if opp0_n is not None else MISSING,
                "post_support_wall_notional": sup1_n if sup1_n is not None else MISSING,
                "post_opposing_wall_notional": opp1_n if opp1_n is not None else MISSING,
            }
        )
    return dynamics, features


def compute_wall_follow_outcomes(
    events: list[dict[str, Any]],
    features: list[dict[str, Any]],
    samples_by: dict[str, list[RichSample]],
    *,
    data_end_ms: int,
) -> list[dict[str, Any]]:
    feat_by = {f["event_id"]: f for f in features}
    rows = []
    for ev in events:
        f = feat_by.get(ev["event_id"], {})
        decision = f.get("wall_follow_decision_at")
        if decision in (None, MISSING):
            continue
        decision = int(decision)
        samples = samples_by.get(ev["symbol"], [])
        # first price after decision
        entry_px = None
        entry_at = None
        for s in samples:
            if s.ts_ms > decision:
                entry_px = s.mid
                entry_at = s.ts_ms
                break
        if entry_px is None:
            continue
        direction = ev["direction"]
        for h in HORIZONS_S:
            t1 = entry_at + h * 1000
            closed = t1 <= data_end_ms
            if not closed:
                rows.append(
                    {
                        "event_id": ev["event_id"],
                        "symbol": ev["symbol"],
                        "direction": direction,
                        "horizon": h,
                        "wall_follow_decision_at": decision,
                        "post_obs_entry_at": entry_at,
                        "post_obs_entry_price": entry_px,
                        "post_observation_mfe_pct": MISSING,
                        "post_observation_endpoint_return_pct": MISSING,
                        "horizon_closed": False,
                        "directional_wall_strength_delta": f.get("directional_wall_strength_delta", MISSING),
                        "both_walls_follow_price_direction": f.get("both_walls_follow_price_direction"),
                    }
                )
                continue
            path = _path_mids(samples, entry_at, t1)
            mids = [m for _, m in path]
            mfe, _ = side_mfe_pct(entry_px, mids, direction) if mids else (None, None)
            ep = side_endpoint_pct(entry_px, mids[-1], direction) if mids else None
            rows.append(
                {
                    "event_id": ev["event_id"],
                    "symbol": ev["symbol"],
                    "direction": direction,
                    "horizon": h,
                    "wall_follow_decision_at": decision,
                    "post_obs_entry_at": entry_at,
                    "post_obs_entry_price": entry_px,
                    "post_observation_mfe_pct": mfe if mfe is not None else MISSING,
                    "post_observation_endpoint_return_pct": ep if ep is not None else MISSING,
                    "horizon_closed": True,
                    "directional_wall_strength_delta": f.get("directional_wall_strength_delta", MISSING),
                    "both_walls_follow_price_direction": f.get("both_walls_follow_price_direction"),
                    "support_wall_present_after_60s": f.get("support_wall_present_after_60s"),
                    "opposing_wall_present_after_60s": f.get("opposing_wall_present_after_60s"),
                }
            )
    return rows


def summarize_wall_follow(
    features: list[dict[str, Any]],
    follow_out: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    feat_by = {f["event_id"]: f for f in features}

    def group_id(f: dict[str, Any]) -> str:
        d = f.get("directional_wall_strength_delta")
        both = f.get("both_walls_follow_price_direction")
        sup = f.get("support_wall_present_after_60s")
        opp = f.get("opposing_wall_present_after_60s")
        if not sup or not opp:
            return "one_or_both_walls_missing"
        if d not in (None, MISSING) and float(d) > 0:
            primary = "strength_delta_gt0"
        elif d not in (None, MISSING) and float(d) < 0:
            primary = "strength_delta_lt0"
        else:
            primary = "strength_delta_missing_or_zero"
        if both is True:
            return f"{primary}|both_follow"
        return primary

    # also explicit buckets requested
    def buckets(f: dict[str, Any]) -> list[str]:
        out = []
        d = f.get("directional_wall_strength_delta")
        if d not in (None, MISSING):
            if float(d) > 0:
                out.append("directional_wall_strength_delta_gt0")
            elif float(d) < 0:
                out.append("directional_wall_strength_delta_lt0")
        if f.get("both_walls_follow_price_direction") is True:
            out.append("both_walls_follow_price_direction")
        # support grows & follows only — approximate via strength>0 and not both
        if d not in (None, MISSING) and float(d) > 0 and f.get("both_walls_follow_price_direction") is not True:
            out.append("support_strengthens_more")
        if d not in (None, MISSING) and float(d) < 0:
            out.append("opposing_strengthens_more")
        # both weaker: both notional change negative
        bn = f.get("bid_wall_notional_change_pct_60s")
        an = f.get("ask_wall_notional_change_pct_60s")
        if bn not in (None, MISSING) and an not in (None, MISSING) and float(bn) < 0 and float(an) < 0:
            out.append("both_sides_weaker")
        if not f.get("support_wall_present_after_60s") or not f.get("opposing_wall_present_after_60s"):
            out.append("one_or_both_walls_missing")
        return out or ["ungrouped"]

    rows = []
    for h in HORIZONS_S:
        # collect by bucket
        by_b: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in follow_out:
            if r["horizon"] != h or not r.get("horizon_closed"):
                continue
            if r.get("post_observation_mfe_pct") in (None, MISSING):
                continue
            f = feat_by.get(r["event_id"], {})
            for b in buckets(f):
                by_b[b].append(r)
        for b, sub in sorted(by_b.items()):
            mfes = [float(x["post_observation_mfe_pct"]) for x in sub]
            eps = [
                float(x["post_observation_endpoint_return_pct"])
                for x in sub
                if x.get("post_observation_endpoint_return_pct") not in (None, MISSING)
            ]
            rows.append(
                {
                    "group": b,
                    "horizon": h,
                    "n": len(sub),
                    "mean_mfe_pct": sum(mfes) / len(mfes),
                    "median_mfe_pct": sorted(mfes)[len(mfes) // 2],
                    "p25_mfe_pct": pctile(mfes, 0.25),
                    "p75_mfe_pct": pctile(mfes, 0.75),
                    "mean_endpoint_return_pct": (sum(eps) / len(eps)) if eps else MISSING,
                    "median_endpoint_return_pct": sorted(eps)[len(eps) // 2] if eps else MISSING,
                    "positive_endpoint_rate": (sum(1 for x in eps if x > 0) / len(eps)) if eps else MISSING,
                }
            )

    # spearman descriptive
    for direction in ("LONG", "SHORT", "ALL"):
        pairs_delta = []
        pairs_mig = []
        for r in follow_out:
            if r["horizon"] != 3600 or not r.get("horizon_closed"):
                continue
            if direction != "ALL" and r["direction"] != direction:
                continue
            f = feat_by.get(r["event_id"], {})
            ep = r.get("post_observation_endpoint_return_pct")
            d = f.get("directional_wall_strength_delta")
            sm = f.get("directional_support_migration_bps")
            if ep not in (None, MISSING) and d not in (None, MISSING):
                pairs_delta.append((float(d), float(ep)))
            if ep not in (None, MISSING) and sm not in (None, MISSING):
                pairs_mig.append((float(sm), float(ep)))
        rows.append(
            {
                "group": f"spearman_strength_delta_vs_1h_endpoint|{direction}",
                "horizon": 3600,
                "n": len(pairs_delta),
                "mean_mfe_pct": MISSING,
                "median_mfe_pct": MISSING,
                "p25_mfe_pct": MISSING,
                "p75_mfe_pct": MISSING,
                "mean_endpoint_return_pct": spearman([a for a, _ in pairs_delta], [b for _, b in pairs_delta])
                if pairs_delta
                else MISSING,
                "median_endpoint_return_pct": MISSING,
                "positive_endpoint_rate": MISSING,
                "note": "value_in_mean_endpoint_return_pct_is_spearman_rho",
            }
        )
        rows.append(
            {
                "group": f"spearman_support_migration_vs_1h_endpoint|{direction}",
                "horizon": 3600,
                "n": len(pairs_mig),
                "mean_mfe_pct": MISSING,
                "median_mfe_pct": MISSING,
                "p25_mfe_pct": MISSING,
                "p75_mfe_pct": MISSING,
                "mean_endpoint_return_pct": spearman([a for a, _ in pairs_mig], [b for _, b in pairs_mig])
                if pairs_mig
                else MISSING,
                "median_endpoint_return_pct": MISSING,
                "positive_endpoint_rate": MISSING,
                "note": "value_in_mean_endpoint_return_pct_is_spearman_rho",
            }
        )
    return rows
