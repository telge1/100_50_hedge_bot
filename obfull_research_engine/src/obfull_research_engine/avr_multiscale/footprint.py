"""Footprint window aggregates via Dashboard SecondSeries.features_at (no formula fork)."""

from __future__ import annotations

from typing import Any

from .config import map_avr_state


def floor_to_5m(unix_s: float | int) -> int:
    u = int(unix_s)
    return u - (u % 300)


def footprint_window_from_series(
    series: Any,
    *,
    available_at: int,
    window_s: int,
    prefix: str = "fp",
) -> dict[str, Any]:
    """Causal footprint metrics for [available_at - window_s, available_at).

    Uses Dashboard ``SecondSeries.features_at`` for response/efficiency/velocity.
    """
    w = int(window_s)
    t = int(available_at)
    lo_ts = t - w
    out: dict[str, Any] = {
        f"{prefix}_window_s": w,
        f"{prefix}_window_start_unix": lo_ts,
        f"{prefix}_window_end_unix": t,
    }
    feats = series.features_at(t, w, min_valid_frac=0.0)
    if feats is None:
        out.update(
            {
                f"{prefix}_trade_count": 0,
                f"{prefix}_buy_trade_count": 0,
                f"{prefix}_sell_trade_count": 0,
                f"{prefix}_buy_notional": 0.0,
                f"{prefix}_sell_notional": 0.0,
                f"{prefix}_total_notional": 0.0,
                f"{prefix}_delta_notional": 0.0,
                f"{prefix}_imbalance": 0.0,
                f"{prefix}_open": None,
                f"{prefix}_high": None,
                f"{prefix}_low": None,
                f"{prefix}_close": None,
                f"{prefix}_return_bps": None,
                f"{prefix}_max_up_bps": None,
                f"{prefix}_max_down_bps": None,
                f"{prefix}_realized_range_bps": None,
                f"{prefix}_price_velocity_bps_per_second": None,
                f"{prefix}_notional_per_second": 0.0,
                f"{prefix}_buy_notional_per_second": 0.0,
                f"{prefix}_sell_notional_per_second": 0.0,
                f"{prefix}_response_bps": None,
                f"{prefix}_efficiency_bps_per_million": None,
                f"{prefix}_active_seconds": 0,
                f"{prefix}_no_trade_seconds": w,
                f"{prefix}_coverage_status": "NO_TRADES",
                f"{prefix}_quality_flags": ["NO_FEATURES"],
            }
        )
        return out

    buy_n = float(feats.get("buy_notional") or 0.0)
    sell_n = float(feats.get("sell_notional") or 0.0)
    buy_c = int(round(float(feats.get("buy_trade_rate") or 0.0) * w))
    sell_c = int(round(float(feats.get("sell_trade_rate") or 0.0) * w))
    # Prefer exact counts from bucket scan for integer fidelity
    buy_c, sell_c, active = _count_trades(series, lo_ts, t)
    total = buy_n + sell_n
    delta = buy_n - sell_n
    imb = (delta / total) if total > 0 else 0.0
    first_px = feats.get("first_price")
    last_px = feats.get("last_price")
    high_px = feats.get("high_price")
    low_px = feats.get("low_price")
    move = feats.get("price_move_bps")
    max_up = None
    max_down = None
    range_bps = None
    if first_px and high_px is not None and first_px > 0:
        max_up = (float(high_px) - float(first_px)) / float(first_px) * 1e4
    if first_px and low_px is not None and first_px > 0:
        max_down = (float(low_px) - float(first_px)) / float(first_px) * 1e4
    if first_px and high_px is not None and low_px is not None and first_px > 0:
        range_bps = (float(high_px) - float(low_px)) / float(first_px) * 1e4

    # Directional response/efficiency from Dashboard features (no new formula)
    if move is not None and float(move) >= 0:
        resp = feats.get("response_ratio_buy")
        eff = feats.get("buy_efficiency")
    else:
        resp = feats.get("response_ratio_sell")
        eff = feats.get("sell_efficiency")

    valid = int(feats.get("valid_seconds") or active)
    no_trade = max(0, w - valid)
    flags: list[str] = []
    if feats.get("insufficient_coverage"):
        flags.append("INSUFFICIENT_WINDOW_COVERAGE")
    cov = "COMPLETE" if valid == w else ("PARTIAL" if valid > 0 else "NO_TRADES")

    out.update(
        {
            f"{prefix}_trade_count": buy_c + sell_c,
            f"{prefix}_buy_trade_count": buy_c,
            f"{prefix}_sell_trade_count": sell_c,
            f"{prefix}_buy_notional": buy_n,
            f"{prefix}_sell_notional": sell_n,
            f"{prefix}_total_notional": total,
            f"{prefix}_delta_notional": delta,
            f"{prefix}_imbalance": imb,
            f"{prefix}_open": first_px,
            f"{prefix}_high": high_px,
            f"{prefix}_low": low_px,
            f"{prefix}_close": last_px,
            f"{prefix}_return_bps": move,
            f"{prefix}_max_up_bps": max_up,
            f"{prefix}_max_down_bps": max_down,
            f"{prefix}_realized_range_bps": range_bps,
            f"{prefix}_price_velocity_bps_per_second": feats.get(
                "price_velocity_bps_per_second"
            ),
            f"{prefix}_notional_per_second": total / float(w),
            f"{prefix}_buy_notional_per_second": buy_n / float(w),
            f"{prefix}_sell_notional_per_second": sell_n / float(w),
            f"{prefix}_response_bps": resp,
            f"{prefix}_efficiency_bps_per_million": eff,
            f"{prefix}_active_seconds": valid,
            f"{prefix}_no_trade_seconds": no_trade,
            f"{prefix}_coverage_status": cov,
            f"{prefix}_quality_flags": flags,
        }
    )
    return out


def _count_trades(series: Any, lo_ts: int, hi_ts: int) -> tuple[int, int, int]:
    import bisect

    lo = bisect.bisect_left(series.ts, lo_ts)
    hi = bisect.bisect_left(series.ts, hi_ts)
    buy_c = sell_c = 0
    for i in range(lo, hi):
        b = series.buckets[i]
        buy_c += int(b.buy_trade_count)
        sell_c += int(b.sell_trade_count)
    return buy_c, sell_c, hi - lo


def aggregate_avr_persistence(
    avr_1s: Any,
    *,
    t_unix: float,
    window_s: int,
    prefix: str,
) -> dict[str, Any]:
    """Summarize AVR 1s states with available_at in (t-window, t] i.e. state in [t-w, t).

    AVR row for state_ts S has available_at = S+1. Causal cut: available_at <= t
    and available_at > t - window  ⇔  state_ts in [ceil(t)-window, floor(t)-1] roughly.
    We include rows where available_at_unix <= t and available_at_unix > t - window.
    """
    import pandas as pd

    from .config import CLASSIFIABLE_BUCKETS, PERSISTENCE_BUCKETS, PROXY_OR_UNCLEAR

    w = int(window_s)
    t = float(t_unix)
    lo = t - w
    out: dict[str, Any] = {f"{prefix}_window_s": w}
    for b in PERSISTENCE_BUCKETS:
        out[f"{prefix}_seconds_{b}"] = 0
        out[f"{prefix}_share_{b}"] = 0.0
        out[f"{prefix}_longest_run_{b}"] = 0

    empty = {
        f"{prefix}_n_seconds": 0,
        f"{prefix}_n_state_changes": 0,
        f"{prefix}_first_state": None,
        f"{prefix}_last_state": None,
        f"{prefix}_majority_state": None,
        f"{prefix}_share_classifiable": 0.0,
        f"{prefix}_share_INSUFFICIENT_BASELINE": 0.0,
        f"{prefix}_share_INSUFFICIENT_DATA": 0.0,
        f"{prefix}_share_proxy_or_unclear": 0.0,
        f"{prefix}_contradiction_count": 0,
        f"{prefix}_contradiction_share": 0.0,
        f"{prefix}_coverage_status": "NO_AVR_ROWS",
    }
    if avr_1s is None or getattr(avr_1s, "empty", True):
        out.update(empty)
        return out

    df = avr_1s
    # available_at in (lo, t]
    mask = (df["available_at_unix"] > lo) & (df["available_at_unix"] <= t)
    sub = df.loc[mask].sort_values("available_at_unix")
    n = int(len(sub))
    if n == 0:
        out.update(empty)
        return out

    buckets = [map_avr_state(s) for s in sub["avr_state"].tolist()]
    counts: dict[str, int] = {b: 0 for b in PERSISTENCE_BUCKETS}
    for b in buckets:
        counts[b] = counts.get(b, 0) + 1
        if b not in counts:
            counts[b] = 1

    # longest runs
    longest = {b: 0 for b in PERSISTENCE_BUCKETS}
    if buckets:
        cur = buckets[0]
        run = 1
        longest[cur] = 1
        for nxt in buckets[1:]:
            if nxt == cur:
                run += 1
            else:
                cur = nxt
                run = 1
            longest[cur] = max(longest.get(cur, 0), run)

    changes = sum(1 for i in range(1, n) if buckets[i] != buckets[i - 1])

    # majority / tie
    max_c = max(counts[b] for b in PERSISTENCE_BUCKETS)
    tops = sorted([b for b in PERSISTENCE_BUCKETS if counts[b] == max_c and max_c > 0])
    majority = "TIE" if len(tops) > 1 else (tops[0] if tops else None)

    classifiable = sum(counts[b] for b in CLASSIFIABLE_BUCKETS)
    insuff_bl = counts.get("INSUFFICIENT_BASELINE", 0)
    insuff_data = counts.get("INSUFFICIENT_DATA", 0)
    proxy_unclear = sum(counts.get(b, 0) for b in PROXY_OR_UNCLEAR)

    contra = 0
    if "avr_contradiction_flags" in sub.columns:
        for v in sub["avr_contradiction_flags"].tolist():
            if isinstance(v, list) and v:
                contra += 1
            elif isinstance(v, str) and v not in ("", "[]", "None"):
                contra += 1

    for b in PERSISTENCE_BUCKETS:
        out[f"{prefix}_seconds_{b}"] = int(counts.get(b, 0))
        out[f"{prefix}_share_{b}"] = float(counts.get(b, 0)) / float(n)
        out[f"{prefix}_longest_run_{b}"] = int(longest.get(b, 0))

    cov = "COMPLETE" if n >= w else "PARTIAL"
    out.update(
        {
            f"{prefix}_n_seconds": n,
            f"{prefix}_n_state_changes": changes,
            f"{prefix}_first_state": buckets[0],
            f"{prefix}_last_state": buckets[-1],
            f"{prefix}_majority_state": majority,
            f"{prefix}_share_classifiable": classifiable / float(n),
            f"{prefix}_share_INSUFFICIENT_BASELINE": insuff_bl / float(n),
            f"{prefix}_share_INSUFFICIENT_DATA": insuff_data / float(n),
            f"{prefix}_share_proxy_or_unclear": proxy_unclear / float(n),
            f"{prefix}_contradiction_count": contra,
            f"{prefix}_contradiction_share": contra / float(n),
            f"{prefix}_coverage_status": cov,
        }
    )
    return out
