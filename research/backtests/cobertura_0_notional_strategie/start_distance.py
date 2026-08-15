"""Causal start-distance and post-add short-average distance helpers.

Research-only math for Cobertura safety-distance audits.
Percentages are decimal fractions (0.05 == 5%).
"""

from __future__ import annotations

import math
from typing import Any, Literal

PostAddPolicy = Literal["disabled", "skip", "scale_down"]


def projected_short_avg_after_neutralization(
    *,
    existing_short_qty: float,
    existing_short_avg: float,
    neutralization_qty: float,
    neutralization_fill_price: float,
) -> float:
    sq = float(existing_short_qty)
    sa = float(existing_short_avg)
    nq = float(neutralization_qty)
    px = float(neutralization_fill_price)
    if nq < 0 or sq < 0:
        raise ValueError("quantities must be non-negative")
    if px <= 0 or (sq > 0 and sa <= 0):
        raise ValueError("prices/averages must be positive")
    total = sq + nq
    if total <= 0:
        raise ValueError("projected short qty must be positive")
    return (sq * sa + nq * px) / total


def projected_start_distance_pct(
    *,
    projected_short_avg: float,
    current_price: float,
) -> float:
    avg = float(projected_short_avg)
    px = float(current_price)
    if avg <= 0:
        raise ValueError("projected_short_avg must be positive")
    return (avg - px) / avg


def projected_total_short_avg_after_add(
    *,
    current_total_short_qty: float,
    current_total_short_avg: float,
    candidate_add_qty: float,
    candidate_fill_price: float,
) -> float:
    q = float(current_total_short_qty)
    a = float(current_total_short_avg)
    add_q = float(candidate_add_qty)
    px = float(candidate_fill_price)
    if add_q <= 0:
        raise ValueError("candidate_add_qty must be positive")
    if px <= 0:
        raise ValueError("candidate_fill_price must be positive")
    if q < 0:
        raise ValueError("current_total_short_qty must be non-negative")
    if q == 0:
        return px
    if a <= 0:
        raise ValueError("current_total_short_avg must be positive when qty > 0")
    return (q * a + add_q * px) / (q + add_q)


def projected_post_add_distance_pct(
    *,
    projected_total_short_avg: float,
    current_price: float,
) -> float:
    avg = float(projected_total_short_avg)
    px = float(current_price)
    if avg <= 0:
        raise ValueError("projected_total_short_avg must be positive")
    return (avg - px) / avg


def minimum_allowed_short_avg(
    *,
    current_price: float,
    minimum_post_add_distance_pct: float,
) -> float:
    d = float(minimum_post_add_distance_pct)
    px = float(current_price)
    if px <= 0:
        raise ValueError("current_price must be positive")
    if not (0.0 <= d < 1.0):
        raise ValueError("minimum_post_add_distance_pct must be in [0, 1)")
    return px / (1.0 - d)


def max_allowed_add_qty(
    *,
    current_total_short_qty: float,
    current_total_short_avg: float,
    candidate_fill_price: float,
    minimum_post_add_distance_pct: float,
    current_price: float | None = None,
) -> float | None:
    """Largest add qty that keeps post-add distance >= minimum.

    Returns None when no positive finite qty can satisfy the guard at this fill
    (e.g. fill price already at/above the minimum allowed average).
    """
    q = float(current_total_short_qty)
    a = float(current_total_short_avg)
    px = float(candidate_fill_price)
    mark = float(current_price) if current_price is not None else px
    min_avg = minimum_allowed_short_avg(
        current_price=mark,
        minimum_post_add_distance_pct=float(minimum_post_add_distance_pct),
    )
    if q <= 0:
        # Pure overlay open: projected avg equals fill price.
        dist = (px - mark) / px if px > 0 else -1.0
        if dist + 1e-15 >= float(minimum_post_add_distance_pct):
            return math.inf
        return None
    if a + 1e-15 < min_avg:
        # Already below required average; cannot fix by adding at lower/equal fill.
        return None
    denom = min_avg - px
    if denom <= 1e-15:
        # Fill at/above min_avg cannot pull average down while keeping min_avg.
        # If current avg already ok and fill >= min_avg, any add keeps avg >= min_avg.
        if px + 1e-15 >= min_avg and a + 1e-15 >= min_avg:
            return math.inf
        return None
    return q * (a - min_avg) / denom


def floor_qty_to_step(qty: float, qty_step: float) -> float:
    step = float(qty_step)
    if step <= 0:
        raise ValueError("qty_step must be > 0")
    if qty <= 0:
        return 0.0
    steps = math.floor(float(qty) / step + 1e-12)
    return max(0.0, steps * step)


def remaining_overlay_capacity(
    *,
    core_qty: float,
    current_overlay_qty: float,
    max_overlay_qty_multiple: float | None,
) -> float:
    if max_overlay_qty_multiple is None:
        return math.inf
    cap = float(core_qty) * float(max_overlay_qty_multiple)
    return max(0.0, cap - float(current_overlay_qty))


def resolve_post_add_qty(
    *,
    configured_candidate_add_qty: float,
    current_total_short_qty: float,
    current_total_short_avg: float,
    current_overlay_qty: float,
    core_qty: float,
    candidate_fill_price: float,
    current_price: float,
    minimum_post_add_distance_pct: float | None,
    post_add_distance_policy: PostAddPolicy,
    max_overlay_qty_multiple: float | None,
    qty_step: float,
    min_notional: float,
) -> dict[str, Any]:
    """Apply post-add distance policy; return actual qty and projection audit."""
    configured = float(configured_candidate_add_qty)
    policy = post_add_distance_policy
    capacity = remaining_overlay_capacity(
        core_qty=core_qty,
        current_overlay_qty=current_overlay_qty,
        max_overlay_qty_multiple=max_overlay_qty_multiple,
    )
    base_cap = min(configured, capacity)

    out: dict[str, Any] = {
        "policy": policy,
        "configured_candidate_add_qty": configured,
        "capacity_limited_qty": base_cap,
        "max_allowed_add_qty": None,
        "actual_add_qty": 0.0,
        "action": "fill",
        "projected_total_short_avg": None,
        "projected_post_add_distance_pct": None,
        "reason": None,
    }

    if minimum_post_add_distance_pct is None or policy == "disabled":
        qty = floor_qty_to_step(configured, qty_step)
        if qty <= 0:
            out.update(action="skip", reason="step", actual_add_qty=0.0)
            return out
        # Leave min_notional / overlay-cap enforcement to the engine exposure check
        # so the disabled path stays fingerprint-identical.
        proj_avg = projected_total_short_avg_after_add(
            current_total_short_qty=current_total_short_qty,
            current_total_short_avg=(
                current_total_short_avg if current_total_short_qty > 0 else candidate_fill_price
            ),
            candidate_add_qty=qty,
            candidate_fill_price=candidate_fill_price,
        )
        dist = projected_post_add_distance_pct(
            projected_total_short_avg=proj_avg, current_price=current_price
        )
        out.update(
            actual_add_qty=qty,
            projected_total_short_avg=proj_avg,
            projected_post_add_distance_pct=dist,
            action="fill",
        )
        return out

    min_d = float(minimum_post_add_distance_pct)
    # Full configured (capacity-capped) projection first.
    trial = floor_qty_to_step(base_cap, qty_step)
    if trial <= 0:
        out.update(action="skip", reason="capacity_or_step", actual_add_qty=0.0)
        return out

    full_avg = projected_total_short_avg_after_add(
        current_total_short_qty=current_total_short_qty,
        current_total_short_avg=current_total_short_avg,
        candidate_add_qty=trial,
        candidate_fill_price=candidate_fill_price,
    )
    full_dist = projected_post_add_distance_pct(
        projected_total_short_avg=full_avg, current_price=current_price
    )
    out["projected_total_short_avg_full"] = full_avg
    out["projected_post_add_distance_pct_full"] = full_dist

    if full_dist + 1e-15 >= min_d:
        if trial * float(candidate_fill_price) + 1e-12 < float(min_notional):
            out.update(action="skip", reason="min_notional", actual_add_qty=0.0)
            return out
        out.update(
            actual_add_qty=trial,
            projected_total_short_avg=full_avg,
            projected_post_add_distance_pct=full_dist,
            action="fill",
        )
        return out

    if policy == "skip":
        out.update(
            action="skip",
            reason="post_add_distance",
            actual_add_qty=0.0,
            projected_total_short_avg=full_avg,
            projected_post_add_distance_pct=full_dist,
        )
        return out

    # scale_down
    max_q = max_allowed_add_qty(
        current_total_short_qty=current_total_short_qty,
        current_total_short_avg=current_total_short_avg,
        candidate_fill_price=candidate_fill_price,
        minimum_post_add_distance_pct=min_d,
        current_price=current_price,
    )
    out["max_allowed_add_qty"] = max_q
    if max_q is None:
        out.update(action="skip", reason="no_feasible_qty", actual_add_qty=0.0)
        return out
    if max_q == math.inf:
        capped = trial
    else:
        capped = min(trial, float(max_q))
    qty = floor_qty_to_step(capped, qty_step)
    if qty <= 0:
        out.update(action="skip", reason="floored_to_zero", actual_add_qty=0.0)
        return out
    if qty * float(candidate_fill_price) + 1e-12 < float(min_notional):
        out.update(action="skip", reason="min_notional", actual_add_qty=0.0)
        return out

    proj_avg = projected_total_short_avg_after_add(
        current_total_short_qty=current_total_short_qty,
        current_total_short_avg=current_total_short_avg,
        candidate_add_qty=qty,
        candidate_fill_price=candidate_fill_price,
    )
    dist = projected_post_add_distance_pct(
        projected_total_short_avg=proj_avg, current_price=current_price
    )
    if dist + 1e-12 < min_d:
        out.update(
            action="skip",
            reason="scaled_still_violates",
            actual_add_qty=0.0,
            projected_total_short_avg=proj_avg,
            projected_post_add_distance_pct=dist,
        )
        return out

    action = "scale_down" if qty + 1e-12 < trial else "fill"
    out.update(
        actual_add_qty=qty,
        projected_total_short_avg=proj_avg,
        projected_post_add_distance_pct=dist,
        action=action,
    )
    return out


def select_first_causal_start(
    candles: list[dict[str, Any]],
    *,
    signal_ts: Any,
    existing_short_qty: float,
    existing_short_avg: float,
    neutralization_qty: float,
    minimum_start_distance_pct: float | None,
    parse_ts,
) -> dict[str, Any]:
    """First candle at/after signal where projected neutralization distance holds."""
    sig = parse_ts(signal_ts)
    rows: list[dict[str, Any]] = []
    for i, candle in enumerate(candles):
        ts = parse_ts(candle["timestamp"])
        if ts < sig:
            continue
        px = float(candle["open"])
        proj_avg = projected_short_avg_after_neutralization(
            existing_short_qty=existing_short_qty,
            existing_short_avg=existing_short_avg,
            neutralization_qty=neutralization_qty,
            neutralization_fill_price=px,
        )
        dist = projected_start_distance_pct(
            projected_short_avg=proj_avg, current_price=px
        )
        delay_bars = len(rows)
        row = {
            "candle_index": i,
            "timestamp": ts.isoformat(),
            "price": px,
            "projected_short_avg": proj_avg,
            "projected_start_distance_pct": dist,
            "delay_bars_from_signal": delay_bars,
            "delay_minutes_from_signal": delay_bars * 5,
            "meets_threshold": (
                True
                if minimum_start_distance_pct is None
                else dist + 1e-15 >= float(minimum_start_distance_pct)
            ),
        }
        rows.append(row)
        if minimum_start_distance_pct is None or row["meets_threshold"]:
            return {
                "selected": row,
                "scan": rows,
                "minimum_start_distance_pct": minimum_start_distance_pct,
            }
    raise ValueError(
        f"no candle met minimum_start_distance_pct={minimum_start_distance_pct}"
    )


def _proj_at_price(
    *,
    existing_short_qty: float,
    existing_short_avg: float,
    neutralization_qty: float,
    price: float,
) -> tuple[float, float]:
    avg = projected_short_avg_after_neutralization(
        existing_short_qty=existing_short_qty,
        existing_short_avg=existing_short_avg,
        neutralization_qty=neutralization_qty,
        neutralization_fill_price=float(price),
    )
    dist = projected_start_distance_pct(
        projected_short_avg=avg, current_price=float(price)
    )
    return avg, dist


def select_start_by_timing_mode(
    candles: list[dict[str, Any]],
    *,
    signal_ts: Any,
    existing_short_qty: float,
    existing_short_avg: float,
    neutralization_qty: float,
    minimum_start_distance_pct: float,
    timing_mode: str,
    parse_ts,
) -> dict[str, Any]:
    """Select trigger/fill under causal execution timing modes T0–T3.

    Modes
    -----
    T0: observe candle open; if distance met, fill immediately at same open.
    T1: observe completed candle close; fill at next candle open.
    T2: at candle X open, use only prior close X-1; if met, fill at open X.
        (Causal twin of T1; same fill when prior close exists.)
    T3: use candle low only to detect intrabar threshold touch; fill next open.
        Never fills at the low.
    """
    if timing_mode not in ("T0", "T1", "T2", "T3"):
        raise ValueError(f"unsupported timing_mode: {timing_mode}")
    thr = float(minimum_start_distance_pct)
    sig = parse_ts(signal_ts)

    # Indices at/after signal.
    idxs = [
        i
        for i, c in enumerate(candles)
        if parse_ts(c["timestamp"]) >= sig
    ]
    if not idxs:
        raise ValueError("no candles at/after signal")
    signal_i0 = idxs[0]

    def delay_from(fill_i: int) -> tuple[int, int]:
        bars = fill_i - signal_i0
        return bars, bars * 5

    scan: list[dict[str, Any]] = []

    if timing_mode == "T0":
        for i in idxs:
            c = candles[i]
            obs = float(c["open"])
            avg, dist = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=obs,
            )
            meets = dist + 1e-15 >= thr
            row = {
                "candle_index": i,
                "observation": "open",
                "observation_price": obs,
                "timestamp": parse_ts(c["timestamp"]).isoformat(),
                "projected_short_avg": avg,
                "projected_start_distance_pct": dist,
                "meets_threshold": meets,
            }
            scan.append(row)
            if meets:
                db, dm = delay_from(i)
                return {
                    "timing_mode": timing_mode,
                    "trigger_timestamp": row["timestamp"],
                    "trigger_observation_price": obs,
                    "trigger_observation_kind": "open",
                    "fill_timestamp": row["timestamp"],
                    "fill_price": obs,
                    "fill_candle_index": i,
                    "projected_short_avg_at_fill": avg,
                    "projected_distance_at_fill": dist,
                    "delay_bars": db,
                    "delay_minutes": dm,
                    "same_bar_fill": True,
                    "used_low_as_fill": False,
                    "scan": scan,
                }

    if timing_mode == "T1":
        # Close of i known only after i completes → fill open of i+1.
        for k, i in enumerate(idxs):
            c = candles[i]
            obs = float(c["close"])
            avg_obs, dist_obs = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=obs,
            )
            meets = dist_obs + 1e-15 >= thr
            scan.append(
                {
                    "candle_index": i,
                    "observation": "close",
                    "observation_price": obs,
                    "timestamp": parse_ts(c["timestamp"]).isoformat(),
                    "projected_short_avg": avg_obs,
                    "projected_start_distance_pct": dist_obs,
                    "meets_threshold": meets,
                }
            )
            if not meets:
                continue
            if k + 1 >= len(idxs):
                raise ValueError("T1 trigger on last candle: no next open")
            j = idxs[k + 1]
            fill_c = candles[j]
            fill_px = float(fill_c["open"])
            avg_f, dist_f = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=fill_px,
            )
            db, dm = delay_from(j)
            return {
                "timing_mode": timing_mode,
                "trigger_timestamp": parse_ts(c["timestamp"]).isoformat(),
                "trigger_observation_price": obs,
                "trigger_observation_kind": "close",
                "fill_timestamp": parse_ts(fill_c["timestamp"]).isoformat(),
                "fill_price": fill_px,
                "fill_candle_index": j,
                "projected_short_avg_at_fill": avg_f,
                "projected_distance_at_fill": dist_f,
                "delay_bars": db,
                "delay_minutes": dm,
                "same_bar_fill": False,
                "used_low_as_fill": False,
                "scan": scan,
            }

    if timing_mode == "T2":
        # At open of X, only close of X-1 is known.
        for k, i in enumerate(idxs):
            if k == 0:
                # No prior close after signal yet — cannot trigger on missing history
                # inside the post-signal window. Optionally allow pre-signal prior
                # close if present in series.
                prev_i = i - 1
                if prev_i < 0:
                    scan.append(
                        {
                            "candle_index": i,
                            "observation": "prior_close_missing",
                            "meets_threshold": False,
                        }
                    )
                    continue
            else:
                prev_i = idxs[k - 1]
            prev = candles[prev_i]
            obs = float(prev["close"])
            avg_obs, dist_obs = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=obs,
            )
            meets = dist_obs + 1e-15 >= thr
            scan.append(
                {
                    "candle_index": prev_i,
                    "fill_candidate_index": i,
                    "observation": "prior_close",
                    "observation_price": obs,
                    "timestamp": parse_ts(prev["timestamp"]).isoformat(),
                    "projected_short_avg": avg_obs,
                    "projected_start_distance_pct": dist_obs,
                    "meets_threshold": meets,
                }
            )
            if not meets:
                continue
            fill_c = candles[i]
            fill_px = float(fill_c["open"])
            avg_f, dist_f = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=fill_px,
            )
            db, dm = delay_from(i)
            return {
                "timing_mode": timing_mode,
                "trigger_timestamp": parse_ts(prev["timestamp"]).isoformat(),
                "trigger_observation_price": obs,
                "trigger_observation_kind": "prior_close",
                "fill_timestamp": parse_ts(fill_c["timestamp"]).isoformat(),
                "fill_price": fill_px,
                "fill_candle_index": i,
                "projected_short_avg_at_fill": avg_f,
                "projected_distance_at_fill": dist_f,
                "delay_bars": db,
                "delay_minutes": dm,
                "same_bar_fill": False,
                "used_low_as_fill": False,
                "scan": scan,
            }

    if timing_mode == "T3":
        for k, i in enumerate(idxs):
            c = candles[i]
            low = float(c["low"])
            # Lower price → larger projected distance; low is the most optimistic
            # intrabar touch check. Never used as fill.
            avg_touch, dist_touch = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=low,
            )
            meets = dist_touch + 1e-15 >= thr
            scan.append(
                {
                    "candle_index": i,
                    "observation": "low_touch_only",
                    "observation_price": low,
                    "timestamp": parse_ts(c["timestamp"]).isoformat(),
                    "projected_short_avg": avg_touch,
                    "projected_start_distance_pct": dist_touch,
                    "meets_threshold": meets,
                }
            )
            if not meets:
                continue
            if k + 1 >= len(idxs):
                raise ValueError("T3 trigger on last candle: no next open")
            j = idxs[k + 1]
            fill_c = candles[j]
            fill_px = float(fill_c["open"])
            if abs(fill_px - low) <= 1e-15:
                # Extremely unlikely; still enforce semantic: fill is next open.
                pass
            avg_f, dist_f = _proj_at_price(
                existing_short_qty=existing_short_qty,
                existing_short_avg=existing_short_avg,
                neutralization_qty=neutralization_qty,
                price=fill_px,
            )
            db, dm = delay_from(j)
            return {
                "timing_mode": timing_mode,
                "trigger_timestamp": parse_ts(c["timestamp"]).isoformat(),
                "trigger_observation_price": low,
                "trigger_observation_kind": "low_touch_only",
                "fill_timestamp": parse_ts(fill_c["timestamp"]).isoformat(),
                "fill_price": fill_px,
                "fill_candle_index": j,
                "projected_short_avg_at_fill": avg_f,
                "projected_distance_at_fill": dist_f,
                "delay_bars": db,
                "delay_minutes": dm,
                "same_bar_fill": False,
                "used_low_as_fill": False,
                "scan": scan,
            }

    raise ValueError(
        f"no start found for timing_mode={timing_mode} "
        f"minimum_start_distance_pct={minimum_start_distance_pct}"
    )
