"""Pure AVR engine: 1s buckets, half-open rolling windows, exclusive states.

Causality
---------
For ``available_at = T`` only buckets with ``second_ts ∈ [T - W, T)`` are used.
That corresponds to trades with ``trade_ts < T`` (second floor of trade_ts).

``down_velocity_percentile`` (down_vel_p)
-----------------------------------------
Percentile rank of ``max(-price_velocity_bps_per_second, 0)`` against the
causal baseline distribution of down-velocities (also non-negative).
A high value means an unusually fast *downward* move vs prior 30m — not a
signed-velocity percentile. It always belongs to the same window that
produced the classification (see evidence.window_*).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .response_baseline import BaselineDistributions, build_baseline_from_feature_rows
from .response_contracts import (
    BASELINE_LOOKBACK_S,
    CANDLE_SECONDS,
    DEFAULT_THRESHOLDS,
    EPS_NOTIONAL_MILLIONS,
    LIFECYCLE_CLOSED,
    LIFECYCLE_PROVISIONAL,
    NOTIONAL_TO_MILLIONS,
    PRIMARY_WINDOW_S,
    ROLLING_WINDOWS_S,
    ResponseState,
    STATE_PRIORITY,
    THIRD_BOUNDS,
    VACUUM_CONFIRMATION,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    AvrThresholds,
    badge_for_state,
    clv,
    config_hash,
)


@dataclass
class SecondBucket:
    second_ts: int
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    buy_size: float = 0.0
    sell_size: float = 0.0
    buy_trade_count: int = 0
    sell_trade_count: int = 0
    first_price: float | None = None
    last_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    _first_key: tuple | None = field(default=None, repr=False)
    _last_key: tuple | None = field(default=None, repr=False)

    @property
    def delta_notional(self) -> float:
        return self.buy_notional - self.sell_notional

    @property
    def total_notional(self) -> float:
        return self.buy_notional + self.sell_notional

    @property
    def total_size(self) -> float:
        return self.buy_size + self.sell_size

    @property
    def vwap(self) -> float | None:
        if self.total_size <= 0:
            return None
        return self.total_notional / self.total_size

    @property
    def move_bps(self) -> float | None:
        if self.first_price is None or self.last_price is None or self.first_price <= 0:
            return None
        return (self.last_price - self.first_price) / self.first_price * 1e4


@dataclass
class RawTrade:
    trade_ts: float
    trade_id: str | int
    side: str
    price: float
    size: float
    notional: float | None = None


def aggregate_trades_to_seconds(trades: Iterable[RawTrade]) -> list[SecondBucket]:
    """UTC 1s buckets. First/last tie-break: (trade_ts, trade_id)."""
    by_sec: dict[int, SecondBucket] = {}
    for tr in trades:
        side = str(tr.side).strip()
        if side not in ("Buy", "Sell"):
            raise ValueError(f"unknown side: {tr.side!r}")
        sec = int(tr.trade_ts)  # floor → bucket label; trades in [sec, sec+1)
        b = by_sec.get(sec)
        if b is None:
            b = SecondBucket(second_ts=sec)
            by_sec[sec] = b
        px = float(tr.price)
        sz = float(tr.size)
        nt = float(tr.notional) if tr.notional is not None else px * sz
        key = (float(tr.trade_ts), str(tr.trade_id))
        if side == "Buy":
            b.buy_notional += nt
            b.buy_size += sz
            b.buy_trade_count += 1
        else:
            b.sell_notional += nt
            b.sell_size += sz
            b.sell_trade_count += 1
        if b.high_price is None or px > b.high_price:
            b.high_price = px
        if b.low_price is None or px < b.low_price:
            b.low_price = px
        if b._first_key is None or key < b._first_key:
            b._first_key = key
            b.first_price = px
        if b._last_key is None or key > b._last_key:
            b._last_key = key
            b.last_price = px
    return [by_sec[k] for k in sorted(by_sec.keys())]


class SecondSeries:
    """Sparse second buckets; rolling features on half-open [T-W, T)."""

    def __init__(self, buckets: Sequence[SecondBucket]):
        self.buckets = list(buckets)
        self.ts = [int(b.second_ts) for b in self.buckets]
        n = len(self.buckets)
        self.p_buy_n = [0.0] * (n + 1)
        self.p_sell_n = [0.0] * (n + 1)
        self.p_buy_c = [0] * (n + 1)
        self.p_sell_c = [0] * (n + 1)
        for i, b in enumerate(self.buckets):
            self.p_buy_n[i + 1] = self.p_buy_n[i] + b.buy_notional
            self.p_sell_n[i + 1] = self.p_sell_n[i] + b.sell_notional
            self.p_buy_c[i + 1] = self.p_buy_c[i] + b.buy_trade_count
            self.p_sell_c[i + 1] = self.p_sell_c[i] + b.sell_trade_count

    def window_slice(self, available_at: int, window_s: int) -> tuple[int, int, int]:
        """Indices for second_ts in [available_at - window_s, available_at)."""
        t = int(available_at)
        start = t - int(window_s)
        hi = bisect.bisect_left(self.ts, t)  # first >= T  → exclusive end
        lo = bisect.bisect_left(self.ts, start)
        return lo, hi, hi - lo

    def features_at(
        self,
        available_at: int,
        window_s: int,
        *,
        min_valid_frac: float = 0.5,
    ) -> dict[str, Any] | None:
        """Causal features for window [available_at - W, available_at)."""
        t = int(available_at)
        w = int(window_s)
        lo, hi, valid = self.window_slice(t, w)
        if valid <= 0:
            return None
        frac = valid / float(w)
        buy_n = self.p_buy_n[hi] - self.p_buy_n[lo]
        sell_n = self.p_sell_n[hi] - self.p_sell_n[lo]
        buy_c = self.p_buy_c[hi] - self.p_buy_c[lo]
        sell_c = self.p_sell_c[hi] - self.p_sell_c[lo]
        wf = float(w)

        first_px = last_px = high_px = low_px = None
        path = 0.0
        prev_last = None
        for i in range(lo, hi):
            b = self.buckets[i]
            if b.first_price is not None and first_px is None:
                first_px = b.first_price
            if b.last_price is not None:
                last_px = b.last_price
            if b.high_price is not None:
                high_px = b.high_price if high_px is None else max(high_px, b.high_price)
            if b.low_price is not None:
                low_px = b.low_price if low_px is None else min(low_px, b.low_price)
            if prev_last is not None and b.first_price is not None:
                path += abs(b.first_price - prev_last)
            if b.first_price is not None and b.last_price is not None:
                path += abs(b.last_price - b.first_price)
            if b.last_price is not None:
                prev_last = b.last_price

        move_bps = None
        if first_px is not None and last_px is not None and first_px > 0:
            move_bps = (last_px - first_px) / first_px * 1e4
        vel = (move_bps / wf) if move_bps is not None else None

        buy_m = buy_n * NOTIONAL_TO_MILLIONS
        sell_m = sell_n * NOTIONAL_TO_MILLIONS
        down_move = max(-(move_bps or 0.0), 0.0) if move_bps is not None else 0.0
        up_move = max((move_bps or 0.0), 0.0) if move_bps is not None else 0.0

        # impact_efficiency_bps_per_million (documented unit)
        sell_eff = down_move / max(sell_m, EPS_NOTIONAL_MILLIONS) if move_bps is not None else 0.0
        buy_eff = up_move / max(buy_m, EPS_NOTIONAL_MILLIONS) if move_bps is not None else 0.0
        if sell_m < EPS_NOTIONAL_MILLIONS:
            sell_eff = 0.0
        if buy_m < EPS_NOTIONAL_MILLIONS:
            buy_eff = 0.0

        # response_ratio: directed progress per unit aggression rate (bps / (USDT/s))
        # opposite_response: progress against aggression direction
        sell_rate = sell_n / wf
        buy_rate = buy_n / wf
        sell_response = down_move / max(sell_rate * NOTIONAL_TO_MILLIONS, EPS_NOTIONAL_MILLIONS)
        buy_response = up_move / max(buy_rate * NOTIONAL_TO_MILLIONS, EPS_NOTIONAL_MILLIONS)
        sell_opposite = up_move / max(sell_rate * NOTIONAL_TO_MILLIONS, EPS_NOTIONAL_MILLIONS)
        buy_opposite = down_move / max(buy_rate * NOTIONAL_TO_MILLIONS, EPS_NOTIONAL_MILLIONS)

        total = buy_n + sell_n
        return {
            "available_at": t,
            "classified_at": t,
            "window_seconds": w,
            "window_start": t - w,
            "window_end": t,  # exclusive
            "valid_seconds": valid,
            "valid_frac": frac,
            "insufficient_coverage": frac < float(min_valid_frac),
            "buy_notional_rate": buy_rate,
            "sell_notional_rate": sell_rate,
            "aggression_notional_rate_buy": buy_rate,
            "aggression_notional_rate_sell": sell_rate,
            "delta_notional_rate": (buy_n - sell_n) / wf,
            "abs_delta_notional_rate": abs(buy_n - sell_n) / wf,
            "buy_trade_rate": buy_c / wf,
            "sell_trade_rate": sell_c / wf,
            "trade_rate": (buy_c + sell_c) / wf,
            "total_notional_rate": total / wf,
            "price_move_bps": move_bps,
            "directional_move_bps_down": down_move,
            "directional_move_bps_up": up_move,
            "price_velocity_bps_per_second": vel,
            "directional_velocity_bps_s_down": max(-(vel or 0.0), 0.0) if vel is not None else 0.0,
            "directional_velocity_bps_s_up": max((vel or 0.0), 0.0) if vel is not None else 0.0,
            "price_acceleration": None,
            "buy_efficiency": buy_eff,
            "sell_efficiency": sell_eff,
            "impact_efficiency_bps_per_million_buy": buy_eff,
            "impact_efficiency_bps_per_million_sell": sell_eff,
            "response_ratio_buy": buy_response,
            "response_ratio_sell": sell_response,
            "opposite_response_buy": buy_opposite,
            "opposite_response_sell": sell_opposite,
            "directional_efficiency": sell_eff if (move_bps or 0) < 0 else buy_eff,
            "directional_dominance_buy": (buy_n / total) if total > 0 else 0.0,
            "directional_dominance_sell": (sell_n / total) if total > 0 else 0.0,
            "first_price": first_px,
            "last_price": last_px,
            "high_price": high_px,
            "low_price": low_px,
            "path_abs": path,
            "buy_notional": buy_n,
            "sell_notional": sell_n,
        }


def attach_acceleration(
    feats: dict[str, Any],
    series: SecondSeries,
    available_at: int,
    window_s: int,
    *,
    min_valid_frac: float,
) -> dict[str, Any]:
    prev_t = int(available_at) - int(window_s)
    prev = series.features_at(prev_t, window_s, min_valid_frac=min_valid_frac)
    vel = feats.get("price_velocity_bps_per_second")
    if prev is None or vel is None:
        feats["price_acceleration"] = None
        return feats
    pvel = prev.get("price_velocity_bps_per_second")
    if pvel is None:
        feats["price_acceleration"] = None
        return feats
    feats["price_acceleration"] = float(vel) - float(pvel)
    return feats


def count_valid_seconds(series: SecondSeries, start: int, end: int) -> int:
    """Count existing 1s buckets with second_ts in [start, end)."""
    lo = bisect.bisect_left(series.ts, int(start))
    hi = bisect.bisect_left(series.ts, int(end))
    return max(0, hi - lo)


def collect_baseline_rows(
    series: SecondSeries,
    available_at: int,
    *,
    lookback_s: int = BASELINE_LOOKBACK_S,
    window_s: int = PRIMARY_WINDOW_S,
    thresholds: AvrThresholds = DEFAULT_THRESHOLDS,
    precomputed_feats: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, float]], int]:
    """Return (feature_rows, valid_second_bucket_count) for [T−lookback, T)."""
    t = int(available_at)
    start = t - int(lookback_s)
    valid_secs = count_valid_seconds(series, start, t)
    rows: list[dict[str, float]] = []
    if precomputed_feats is not None:
        for tp in precomputed_feats:
            if start <= int(tp) < t:
                feats = precomputed_feats[tp]
                if feats and not feats.get("insufficient_coverage"):
                    rows.append(feats)
        return rows, valid_secs
    lo = bisect.bisect_left(series.ts, start - 1)
    hi = bisect.bisect_left(series.ts, t)
    for i in range(lo, hi):
        tp = int(series.ts[i]) + 1
        if not (start <= tp < t):
            continue
        feats = series.features_at(
            tp, window_s, min_valid_frac=thresholds.minimum_valid_seconds
        )
        if feats is None or feats.get("insufficient_coverage"):
            continue
        rows.append(feats)
    return rows, valid_secs


def _strength(state: str, evidence: dict[str, Any]) -> float:
    """Deterministic strength in [0, 100] for dominant selection."""
    if state in (
        ResponseState.INSUFFICIENT_DATA.value,
        ResponseState.INSUFFICIENT_BASELINE.value,
        ResponseState.BALANCED.value,
    ):
        return 0.0
    if state == ResponseState.SELLER_CONTROL.value:
        return min(
            100.0,
            0.34 * float(evidence.get("sell_aggression_percentile") or 0)
            + 0.33 * float(evidence.get("down_velocity_percentile") or 0)
            + 0.33 * float(evidence.get("sell_efficiency_percentile") or 0),
        )
    if state == ResponseState.BUYER_CONTROL.value:
        return min(
            100.0,
            0.34 * float(evidence.get("buy_aggression_percentile") or 0)
            + 0.33 * float(evidence.get("up_velocity_percentile") or 0)
            + 0.33 * float(evidence.get("buy_efficiency_percentile") or 0),
        )
    if state == ResponseState.SELL_ABSORPTION_CANDIDATE.value:
        # Strong absorption = high aggression + weak down response
        weak = 100.0 - float(evidence.get("down_velocity_percentile") or 0)
        return min(
            100.0,
            0.5 * float(evidence.get("sell_aggression_percentile") or 0)
            + 0.3 * weak
            + 0.2 * (100.0 - float(evidence.get("sell_efficiency_percentile") or 0)),
        )
    if state == ResponseState.BUY_ABSORPTION_CANDIDATE.value:
        weak = 100.0 - float(evidence.get("up_velocity_percentile") or 0)
        return min(
            100.0,
            0.5 * float(evidence.get("buy_aggression_percentile") or 0)
            + 0.3 * weak
            + 0.2 * (100.0 - float(evidence.get("buy_efficiency_percentile") or 0)),
        )
    if state in (
        ResponseState.VACUUM_DOWN_PROXY.value,
        ResponseState.VACUUM_UP_PROXY.value,
    ):
        return min(
            100.0,
            0.6
            * float(
                evidence.get("down_velocity_percentile")
                if state.endswith("DOWN_PROXY")
                else evidence.get("up_velocity_percentile")
                or 0
            )
            + 0.4 * (100.0 - float(evidence.get("total_notional_percentile") or 0)),
        )
    return 0.0


def classify_features(
    feats: dict[str, Any],
    baseline: BaselineDistributions,
    thresholds: AvrThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """Exclusive state from one window's features + causal baseline ranks."""
    empty = {
        "state": ResponseState.INSUFFICIENT_DATA.value,
        "confirmation": None,
        "evidence": {},
        "ranks": {},
        "strength": 0.0,
    }
    if feats is None or feats.get("insufficient_coverage"):
        empty["evidence"] = {"reason": "insufficient_window_coverage"}
        return empty
    if not baseline.sufficient:
        return {
            "state": ResponseState.INSUFFICIENT_BASELINE.value,
            "confirmation": None,
            "evidence": {"reason": "insufficient_baseline", **baseline.to_meta()},
            "ranks": {},
            "strength": 0.0,
        }

    ranks = baseline.ranks(feats)
    sell_dom = float(feats.get("directional_dominance_sell") or 0.0)
    buy_dom = float(feats.get("directional_dominance_buy") or 0.0)
    move = feats.get("price_move_bps")
    move_f = float(move) if move is not None else 0.0
    vel = feats.get("price_velocity_bps_per_second")
    vel_f = float(vel) if vel is not None else 0.0

    sell_agg = ranks["sell_aggression_percentile"]
    buy_agg = ranks["buy_aggression_percentile"]
    down_vel_p = ranks["down_velocity_percentile"]
    up_vel_p = ranks["up_velocity_percentile"]
    sell_eff_p = ranks["sell_efficiency_percentile"]
    buy_eff_p = ranks["buy_efficiency_percentile"]
    tot_agg = ranks["total_notional_percentile"]
    sell_resp_p = ranks.get("sell_response_percentile", sell_eff_p)
    buy_resp_p = ranks.get("buy_response_percentile", buy_eff_p)
    down_prog_p = ranks.get("down_progress_percentile", down_vel_p)
    up_prog_p = ranks.get("up_progress_percentile", up_vel_p)

    thr = thresholds
    evidence = {
        "classified_at": feats.get("classified_at"),
        "available_at": feats.get("available_at"),
        "window_seconds": feats.get("window_seconds"),
        "window_start": feats.get("window_start"),
        "window_end": feats.get("window_end"),
        "sell_aggression_percentile": round(sell_agg, 2),
        "buy_aggression_percentile": round(buy_agg, 2),
        "down_velocity_percentile": round(down_vel_p, 2),
        "up_velocity_percentile": round(up_vel_p, 2),
        "down_progress_percentile": round(down_prog_p, 2),
        "up_progress_percentile": round(up_prog_p, 2),
        "sell_efficiency_percentile": round(sell_eff_p, 2),
        "buy_efficiency_percentile": round(buy_eff_p, 2),
        "sell_response_percentile": round(sell_resp_p, 2),
        "buy_response_percentile": round(buy_resp_p, 2),
        "total_notional_percentile": round(tot_agg, 2),
        "directional_dominance_sell": round(sell_dom, 4),
        "directional_dominance_buy": round(buy_dom, 4),
        "price_progress_bps": round(move_f, 4),
        "directional_move_bps_down": round(float(feats.get("directional_move_bps_down") or 0), 4),
        "directional_move_bps_up": round(float(feats.get("directional_move_bps_up") or 0), 4),
        "price_velocity_bps_per_second": round(vel_f, 6) if vel is not None else None,
        "directional_velocity_bps_s_down": round(
            float(feats.get("directional_velocity_bps_s_down") or 0), 6
        ),
        "aggression_notional_rate_sell": round(
            float(feats.get("aggression_notional_rate_sell") or 0), 3
        ),
        "aggression_notional_rate_buy": round(
            float(feats.get("aggression_notional_rate_buy") or 0), 3
        ),
        "impact_efficiency_bps_per_million_sell": round(
            float(feats.get("impact_efficiency_bps_per_million_sell") or 0), 6
        ),
        "impact_efficiency_bps_per_million_buy": round(
            float(feats.get("impact_efficiency_bps_per_million_buy") or 0), 6
        ),
        "response_ratio_sell": round(float(feats.get("response_ratio_sell") or 0), 6),
        "response_ratio_buy": round(float(feats.get("response_ratio_buy") or 0), 6),
        "opposite_response_sell": round(float(feats.get("opposite_response_sell") or 0), 6),
        "opposite_response_buy": round(float(feats.get("opposite_response_buy") or 0), 6),
        "buy_notional_rate": round(float(feats.get("buy_notional_rate") or 0), 3),
        "sell_notional_rate": round(float(feats.get("sell_notional_rate") or 0), 3),
        "triggered": [],
    }

    # --- helpers ---
    sell_side = (
        sell_agg >= thr.high_aggression_percentile
        and sell_dom >= thr.minimum_directional_dominance
    )
    buy_side = (
        buy_agg >= thr.high_aggression_percentile
        and buy_dom >= thr.minimum_directional_dominance
    )
    # Impact OK if efficiency OR response OR unusual absolute progress vs baseline
    sell_impact_ok = (
        sell_eff_p >= thr.high_efficiency_percentile
        or sell_resp_p >= thr.high_efficiency_percentile
        or down_prog_p >= thr.high_efficiency_percentile
    )
    buy_impact_ok = (
        buy_eff_p >= thr.high_efficiency_percentile
        or buy_resp_p >= thr.high_efficiency_percentile
        or up_prog_p >= thr.high_efficiency_percentile
    )
    sell_fast_efficient = (
        move_f <= -thr.min_control_progress_bps
        and down_vel_p >= thr.fast_velocity_percentile
        and sell_impact_ok
    )
    buy_fast_efficient = (
        move_f >= thr.min_control_progress_bps
        and up_vel_p >= thr.fast_velocity_percentile
        and buy_impact_ok
    )
    # Contradiction: strong directed dump cannot be absorption
    sell_control_like = (
        move_f < 0
        and down_vel_p >= thr.fast_velocity_percentile
        and sell_impact_ok
    )
    buy_control_like = (
        move_f > 0
        and up_vel_p >= thr.fast_velocity_percentile
        and buy_impact_ok
    )
    sell_weak_response = (
        move_f >= -thr.weak_progress_bps  # flat / up / tiny down
        or down_vel_p <= thr.slow_velocity_max_percentile
        or sell_eff_p <= thr.low_efficiency_percentile
        or float(feats.get("opposite_response_sell") or 0) > float(feats.get("response_ratio_sell") or 0)
    )
    buy_weak_response = (
        move_f <= thr.weak_progress_bps
        or up_vel_p <= thr.slow_velocity_max_percentile
        or buy_eff_p <= thr.low_efficiency_percentile
        or float(feats.get("opposite_response_buy") or 0) > float(feats.get("response_ratio_buy") or 0)
    )

    # 1) CONTROL
    if sell_side and sell_fast_efficient:
        evidence["triggered"] = [
            "sell_aggression_high",
            "sell_dominance",
            "down_progress",
            "down_velocity_high",
            "sell_efficiency_or_response_high",
        ]
        st = ResponseState.SELLER_CONTROL.value
        return {
            "state": st,
            "confirmation": None,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }
    if buy_side and buy_fast_efficient:
        evidence["triggered"] = [
            "buy_aggression_high",
            "buy_dominance",
            "up_progress",
            "up_velocity_high",
            "buy_efficiency_or_response_high",
        ]
        st = ResponseState.BUYER_CONTROL.value
        return {
            "state": st,
            "confirmation": None,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }

    # 2) ABSORPTION — blocked by contradiction guard
    if sell_side and sell_weak_response and not sell_control_like:
        evidence["triggered"] = [
            "sell_aggression_high",
            "sell_dominance",
            "weak_or_opposite_down_response",
        ]
        st = ResponseState.SELL_ABSORPTION_CANDIDATE.value
        return {
            "state": st,
            "confirmation": None,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }
    if buy_side and buy_weak_response and not buy_control_like:
        evidence["triggered"] = [
            "buy_aggression_high",
            "buy_dominance",
            "weak_or_opposite_up_response",
        ]
        st = ResponseState.BUY_ABSORPTION_CANDIDATE.value
        return {
            "state": st,
            "confirmation": None,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }

    # 3) VACUUM proxies
    if (
        down_vel_p >= thr.fast_velocity_percentile
        and tot_agg <= thr.vacuum_aggression_max_percentile
        and move_f < 0
    ):
        evidence["triggered"] = ["fast_down", "low_total_aggression"]
        st = ResponseState.VACUUM_DOWN_PROXY.value
        return {
            "state": st,
            "confirmation": VACUUM_CONFIRMATION,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }
    if (
        up_vel_p >= thr.fast_velocity_percentile
        and tot_agg <= thr.vacuum_aggression_max_percentile
        and move_f > 0
    ):
        evidence["triggered"] = ["fast_up", "low_total_aggression"]
        st = ResponseState.VACUUM_UP_PROXY.value
        return {
            "state": st,
            "confirmation": VACUUM_CONFIRMATION,
            "evidence": evidence,
            "ranks": ranks,
            "strength": _strength(st, evidence),
        }

    st = ResponseState.BALANCED.value
    evidence["triggered"] = ["none"]
    return {
        "state": st,
        "confirmation": None,
        "evidence": evidence,
        "ranks": ranks,
        "strength": 0.0,
    }


def _verification_for_coverage(coverage: str) -> str:
    return (
        VERIFICATION_VERIFIED
        if str(coverage).upper() == "COMPLETE"
        else VERIFICATION_UNVERIFIED
    )


def _pick_dominant(
    classifications: list[tuple[int, str, dict]],
) -> tuple[str, float, dict | None]:
    """Peak-weighted dominant with duration bonus.

    score = peak_strength * (1 + 0.12 * min(count, 8))
    Tie-break: STATE_PRIORITY, then latest available_at of the peak window.
    Prevents many weak windows from beating a stronger opposing impulse.
    """
    if not classifications:
        return ResponseState.INSUFFICIENT_DATA.value, 0.0, None
    peaks: dict[str, float] = {}
    counts: dict[str, int] = {}
    best_ev: dict[str, tuple[float, int, dict]] = {}
    for t, st, cl in classifications:
        if st in (
            ResponseState.BALANCED.value,
            ResponseState.INSUFFICIENT_DATA.value,
            ResponseState.INSUFFICIENT_BASELINE.value,
        ):
            continue
        s = float(cl.get("strength") or _strength(st, cl.get("evidence") or {}))
        counts[st] = counts.get(st, 0) + 1
        if st not in peaks or s > peaks[st]:
            peaks[st] = s
        prev = best_ev.get(st)
        if prev is None or s > prev[0] or (s == prev[0] and t >= prev[1]):
            best_ev[st] = (s, t, cl)

    if not peaks:
        t, st, cl = classifications[-1]
        return st, float(cl.get("strength") or 0.0), cl

    def score(st: str) -> float:
        return peaks[st] * (1.0 + 0.12 * min(counts[st], 8))

    def key(st: str) -> tuple:
        return (score(st), STATE_PRIORITY.get(st, 0), best_ev[st][1])

    dom = max(peaks.keys(), key=key)
    return dom, score(dom), best_ev[dom][2]


def summarize_candle_response(
    *,
    candle_time: int,
    ohlc: dict[str, float],
    coverage: str,
    series: SecondSeries,
    now_unix: int | None = None,
    thresholds: AvrThresholds = DEFAULT_THRESHOLDS,
    baseline_invalidated: bool = False,
    baseline_invalidate_reason: str | None = None,
    vpoc_price: float | None = None,
    sample_every_s: int = 5,
    precomputed_feats: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ct = int(candle_time)
    c_end = ct + CANDLE_SECONDS
    now = int(now_unix) if now_unix is not None else c_end
    forming = now < c_end
    # Exclusive cutoff: only data with trade_ts < cutoff
    cutoff = min(now, c_end) if forming else c_end
    lifecycle = LIFECYCLE_PROVISIONAL if forming else LIFECYCLE_CLOSED

    if str(coverage).upper() == "MISSING":
        return {
            "engine": "avr-v1",
            "provisional": forming,
            "lifecycle": lifecycle,
            "coverage_status": coverage,
            "verification": VERIFICATION_UNVERIFIED,
            "dominant_state": ResponseState.INSUFFICIENT_DATA.value,
            "final_state": ResponseState.INSUFFICIENT_DATA.value,
            "dominant_strength": 0.0,
            "final_strength": 0.0,
            "state_counts": {},
            "state_share": {},
            "state_confidence_tier": "none",
            "badge": badge_for_state(ResponseState.INSUFFICIENT_DATA),
            "evidence": [{"reason": "coverage_MISSING"}],
            "third_states": {},
            "config_hash": config_hash(thresholds),
        }

    verification = _verification_for_coverage(coverage)
    primary_w = int(thresholds.primary_window_s)
    step = max(1, int(sample_every_s))
    classifications: list[tuple[int, str, dict]] = []

    # Sample available_at cutoffs inside (ct, cutoff] on step grid + third ends
    must = {ct + 100, ct + 200, cutoff}
    sample_ts: list[int] = []
    t = ct + step
    while t <= cutoff:
        sample_ts.append(t)
        t += step
    for m in must:
        if ct < m <= cutoff and m not in sample_ts:
            sample_ts.append(m)
    sample_ts = sorted(set(sample_ts))

    for available_at in sample_ts:
        if precomputed_feats is not None and available_at in precomputed_feats:
            feats = precomputed_feats[available_at]
        else:
            feats = series.features_at(
                available_at, primary_w, min_valid_frac=thresholds.minimum_valid_seconds
            )
            if feats is not None:
                attach_acceleration(
                    feats,
                    series,
                    available_at,
                    primary_w,
                    min_valid_frac=thresholds.minimum_valid_seconds,
                )
        if feats is None:
            continue
        base_rows, valid_secs = collect_baseline_rows(
            series,
            available_at,
            window_s=primary_w,
            thresholds=thresholds,
            precomputed_feats=precomputed_feats,
        )
        baseline = build_baseline_from_feature_rows(
            base_rows,
            invalidated=baseline_invalidated,
            invalidate_reason=baseline_invalidate_reason,
            valid_second_buckets=valid_secs,
        )
        classified = classify_features(feats, baseline, thresholds)
        if verification == VERIFICATION_UNVERIFIED:
            classified = dict(classified)
            classified["verification"] = VERIFICATION_UNVERIFIED
        classifications.append((available_at, classified["state"], classified))

    # Totals in [ct, cutoff)
    lo = bisect.bisect_left(series.ts, ct)
    hi = bisect.bisect_left(series.ts, cutoff)
    buy_n = sell_n = 0.0
    first_px = last_px = high_px = low_px = None
    for i in range(lo, hi):
        b = series.buckets[i]
        buy_n += b.buy_notional
        sell_n += b.sell_notional
        if b.first_price is not None and first_px is None:
            first_px = b.first_price
        if b.last_price is not None:
            last_px = b.last_price
        if b.high_price is not None:
            high_px = b.high_price if high_px is None else max(high_px, b.high_price)
        if b.low_price is not None:
            low_px = b.low_price if low_px is None else min(low_px, b.low_price)

    delta_n = buy_n - sell_n
    tot = buy_n + sell_n
    delta_pct = (delta_n / tot * 100.0) if tot > 0 else 0.0
    o, h, l, c = (
        float(ohlc["open"]),
        float(ohlc["high"]),
        float(ohlc["low"]),
        float(ohlc["close"]),
    )
    range_bps_ohlc = ((h - l) / o * 1e4) if o > 0 else None
    trade_move = (
        (last_px - first_px) / first_px * 1e4
        if first_px and last_px and first_px > 0
        else None
    )

    # Thirds
    third_states: dict[str, Any] = {}
    for name, (a, b) in THIRD_BOUNDS.items():
        t0 = ct + a
        t1 = ct + b  # exclusive end of third
        third_forming = forming and cutoff < t1
        if cutoff <= t0:
            third_states[name] = {
                "state": ResponseState.INSUFFICIENT_DATA.value,
                "provisional": True,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
            }
            continue
        end_t = min(cutoff, t1)
        t_lo = bisect.bisect_left(series.ts, t0)
        t_hi = bisect.bisect_left(series.ts, end_t)
        tb = ts_ = 0.0
        for j in range(t_lo, t_hi):
            tb += series.buckets[j].buy_notional
            ts_ += series.buckets[j].sell_notional
        in_third = [(tt, st, cl) for tt, st, cl in classifications if t0 < tt <= end_t]
        if in_third:
            dom, _, dom_cl = _pick_dominant(in_third)
            final_st = in_third[-1][1]
            ev = (dom_cl or in_third[-1][2]).get("evidence", {})
        else:
            dom = final_st = ResponseState.INSUFFICIENT_DATA.value
            ev = {}
        td = tb + ts_
        third_states[name] = {
            "state": dom,
            "final_state": final_st,
            "provisional": third_forming,
            "buy_notional": tb,
            "sell_notional": ts_,
            "delta_notional": tb - ts_,
            "delta_pct": ((tb - ts_) / td * 100.0) if td > 0 else 0.0,
            "evidence": ev,
            "badge": badge_for_state(dom),
        }

    state_counts: dict[str, int] = {}
    for _, st, _ in classifications:
        state_counts[st] = state_counts.get(st, 0) + 1
    n_cls = max(len(classifications), 1)
    state_share = {k: round(v / n_cls, 4) for k, v in state_counts.items()}

    if not classifications:
        dominant = final = ResponseState.INSUFFICIENT_DATA.value
        dom_strength = final_strength = 0.0
        dom_cl = None
        evidence_list: list[dict] = [{"reason": "no_valid_windows"}]
    else:
        final = classifications[-1][1]
        final_strength = float(classifications[-1][2].get("strength") or 0.0)
        dominant, dom_strength, dom_cl = _pick_dominant(classifications)
        # Evidence MUST belong to the dominant state's strongest window
        if dom_cl is not None:
            evidence_list = [dom_cl.get("evidence") or {}]
        else:
            evidence_list = [classifications[-1][2].get("evidence") or {}]

    conf = "low" if verification == VERIFICATION_UNVERIFIED else "medium"
    if verification == VERIFICATION_VERIFIED and dominant not in (
        ResponseState.BALANCED.value,
        ResponseState.INSUFFICIENT_DATA.value,
    ):
        conf = "high"

    panel = None
    if classifications:
        src = dom_cl or classifications[-1][2]
        panel = _panel_from_classification(dominant, src, lifecycle, verification)

    # Peak windows for diagnostics
    strongest_ctrl = None
    strongest_abs = None
    best_c = best_a = -1.0
    for t, st, cl in classifications:
        s = float(cl.get("strength") or 0)
        if st == ResponseState.SELLER_CONTROL.value and s >= best_c:
            best_c, strongest_ctrl = s, t
        if st == ResponseState.SELL_ABSORPTION_CANDIDATE.value and s >= best_a:
            best_a, strongest_abs = s, t

    return {
        "engine": "avr-v1",
        "provisional": forming,
        "lifecycle": lifecycle,
        "coverage_status": coverage,
        "verification": verification,
        "config_hash": config_hash(thresholds),
        "thresholds_provisional": True,
        "total_buy_notional": buy_n,
        "total_sell_notional": sell_n,
        "candle_delta_notional": delta_n,
        "delta_pct": delta_pct,
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
        "ohlc_source": "signal_generator.candles_1m",
        "clv": clv(o, h, l, c),
        "range_bps": range_bps_ohlc,
        "vpoc": vpoc_price,
        "trade_price": {
            "source": "public_trades_canonical",
            "first": first_px,
            "last": last_px,
            "high": high_px,
            "low": low_px,
            "move_bps": trade_move,
        },
        "early_metrics": third_states.get("EARLY"),
        "middle_metrics": third_states.get("MIDDLE"),
        "late_metrics": third_states.get("LATE"),
        "third_states": {
            k: v.get("state") for k, v in third_states.items() if isinstance(v, dict)
        },
        "thirds": third_states,
        "state_counts": state_counts,
        "state_share": state_share,
        "dominant_state": dominant,
        "dominant_strength": round(dom_strength, 3),
        "final_state": final,
        "final_strength": round(final_strength, 3),
        "strongest_seller_control_at": strongest_ctrl,
        "strongest_sell_absorption_at": strongest_abs,
        "state_confidence_tier": conf,
        "badge": badge_for_state(dominant),
        "final_badge": badge_for_state(final),
        "evidence": evidence_list,
        "panel": panel,
        "primary_window_s": primary_w,
        "down_vel_p_semantics": (
            "percentile of max(-velocity_bps_s,0) vs causal baseline; "
            "high = unusually fast downward move in THIS window"
        ),
    }


def _panel_from_classification(
    state: str, cl: dict, lifecycle: str, verification: str
) -> dict[str, Any]:
    ev = cl.get("evidence") or {}
    sell_agg = float(ev.get("sell_aggression_percentile") or 0)
    buy_agg = float(ev.get("buy_aggression_percentile") or 0)
    down_v = float(ev.get("down_velocity_percentile") or 0)
    up_v = float(ev.get("up_velocity_percentile") or 0)
    sell_eff = float(ev.get("sell_efficiency_percentile") or 0)
    buy_eff = float(ev.get("buy_efficiency_percentile") or 0)
    side = "SELL" if sell_agg >= buy_agg else "BUY"
    agg_p = max(sell_agg, buy_agg)
    agg_lbl = "EXTREME" if agg_p >= 98 else ("HIGH" if agg_p >= 90 else "ELEVATED")
    if down_v >= 90:
        vel_lbl = "DOWN FAST"
    elif up_v >= 90:
        vel_lbl = "UP FAST"
    elif down_v >= 60:
        vel_lbl = "DOWN SLOW"
    elif up_v >= 60:
        vel_lbl = "UP SLOW"
    else:
        vel_lbl = "FLAT"
    eff_p = sell_eff if side == "SELL" else buy_eff
    eff_lbl = "HIGH" if eff_p >= 70 else ("LOW" if eff_p <= 35 else "MID")
    waiting = ""
    if state == ResponseState.SELL_ABSORPTION_CANDIDATE.value:
        waiting = "RECLAIM"
    elif state == ResponseState.BUY_ABSORPTION_CANDIDATE.value:
        waiting = "REJECT"
    return {
        "aggression": f"{side} {agg_lbl}",
        "velocity": vel_lbl,
        "efficiency": eff_lbl,
        "control": (
            "SELLERS"
            if state == ResponseState.SELLER_CONTROL.value
            else (
                "BUYERS"
                if state == ResponseState.BUYER_CONTROL.value
                else ("ABSORPTION?" if "ABSORPTION" in state else "UNCLEAR")
            )
        ),
        "state": state,
        "badge": badge_for_state(state),
        "strength": round(float(cl.get("strength") or 0), 2),
        "window_seconds": ev.get("window_seconds"),
        "available_at": ev.get("available_at"),
        "aggression_percentile": round(agg_p, 1),
        "velocity_percentile": round(down_v if side == "SELL" else up_v, 1),
        "efficiency_percentile": round(eff_p, 1),
        "price_progress_bps": ev.get("price_progress_bps"),
        "status": lifecycle,
        "provisional": lifecycle == LIFECYCLE_PROVISIONAL,
        "verification": verification,
        "waiting": waiting,
    }


__all__ = [
    "SecondBucket",
    "RawTrade",
    "aggregate_trades_to_seconds",
    "SecondSeries",
    "attach_acceleration",
    "collect_baseline_rows",
    "classify_features",
    "summarize_candle_response",
    "_strength",
    "_pick_dominant",
]
