"""Causal 5m price stream for MarketMemoryState (research only)."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from market_memory_state import (
    EVENT_DROP_EVENT,
    EVENT_DIAG_SELLOFF,
    EVENT_REFERENCE_LOW_INVALIDATED,
    MarketMemoryState,
    _iso,
    _parse_utc,
)

_REPO = Path(__file__).resolve().parents[4].parents[2]
_DET = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_to_upward_staircase_detector_v1"


def load_drop_params() -> dict:
    return json.loads((_DET / "detector_parameters.json").read_text(encoding="utf-8"))


@dataclass
class BarRing:
    """Rolling 5m OHLC window for drawdown (max 288 bars)."""

    max_len: int = 288
    times: list[str] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"max_len": self.max_len, "times": self.times, "highs": self.highs, "lows": self.lows, "closes": self.closes}

    @classmethod
    def from_dict(cls, d: dict) -> BarRing:
        r = cls(max_len=int(d.get("max_len", 288)))
        r.times = list(d.get("times") or [])
        r.highs = [float(x) for x in d.get("highs") or []]
        r.lows = [float(x) for x in d.get("lows") or []]
        r.closes = [float(x) for x in d.get("closes") or []]
        return r

    def push(self, t: str, o: float, h: float, l: float, c: float) -> None:
        self.times.append(t)
        self.highs.append(h)
        self.lows.append(l)
        self.closes.append(c)
        while len(self.times) > self.max_len:
            self.times.pop(0)
            self.highs.pop(0)
            self.lows.pop(0)
            self.closes.pop(0)

    def rolling_high(self) -> float:
        return max(self.highs) if self.highs else 0.0


@dataclass
class PriceStreamProcessor:
    params: dict
    bar_ring: BarRing = field(default_factory=BarRing)
    watermark_5m: str | None = None
    merged_drop: dict | None = None
    drop_episodes: list[dict] = field(default_factory=list)
    active_reference_low: float | None = None
    active_reference_source: str | None = None
    invalidations: list[dict] = field(default_factory=list)
    diagnostic_selloffs: list[dict] = field(default_factory=list)
    feature_rows: list[dict] = field(default_factory=list)
    calib_range_p90: float | None = None
    processed_bars: int = 0

    def to_dict(self) -> dict:
        return {
            "watermark_5m": self.watermark_5m,
            "bar_ring": self.bar_ring.to_dict(),
            "merged_drop": self.merged_drop,
            "drop_episodes": self.drop_episodes,
            "active_reference_low": self.active_reference_low,
            "active_reference_source": self.active_reference_source,
            "invalidations": self.invalidations,
            "diagnostic_selloffs": self.diagnostic_selloffs,
            "calib_range_p90": self.calib_range_p90,
            "processed_bars": self.processed_bars,
        }

    @classmethod
    def from_dict(cls, d: dict, params: dict) -> PriceStreamProcessor:
        p = cls(params=params)
        p.watermark_5m = d.get("watermark_5m")
        p.bar_ring = BarRing.from_dict(d.get("bar_ring") or {})
        p.merged_drop = d.get("merged_drop")
        p.drop_episodes = list(d.get("drop_episodes") or [])
        p.active_reference_low = d.get("active_reference_low")
        p.active_reference_source = d.get("active_reference_source")
        p.invalidations = list(d.get("invalidations") or [])
        p.diagnostic_selloffs = list(d.get("diagnostic_selloffs") or [])
        p.calib_range_p90 = d.get("calib_range_p90")
        p.processed_bars = int(d.get("processed_bars") or 0)
        return p

    def calibrate_diagnostic_threshold(self, bars: dict, start: datetime, end: datetime) -> None:
        """P90 red-candle range from pre-August window (not August-tuned)."""
        ranges: list[float] = []
        for t, b in sorted(bars.items()):
            if t < start or t > end:
                continue
            o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            if c >= o and c > 0:
                ranges.append((h - l) / c * 100)
        self.calib_range_p90 = statistics.quantiles(ranges, n=10)[8] if len(ranges) >= 20 else None

    def _bar_metrics(self, o: float, h: float, l: float, c: float) -> dict:
        rng = h - l
        range_pct = rng / c * 100 if c else 0.0
        lower_wick = min(o, c) - l
        wick_ratio = lower_wick / rng if rng > 1e-12 else 0.0
        wh = self.bar_ring.rolling_high()
        dd_pct = (wh - c) / wh * 100 if wh else 0.0
        return {
            "range_pct": range_pct,
            "lower_wick_ratio": wick_ratio,
            "drawdown_pct": dd_pct,
            "is_red": c < o,
            "rolling_high": wh,
        }

    def process_bar(self, state: MarketMemoryState, t: datetime, bar: dict, emit_features: bool = True) -> dict:
        ts = _iso(t)
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        self.bar_ring.push(ts, o, h, l, c)
        self.watermark_5m = ts
        self.processed_bars += 1
        m = self._bar_metrics(o, h, l, c)
        out: dict[str, Any] = {"bar_utc": ts, **m}

        if emit_features:
            self.feature_rows.append(
                {
                    "bar_utc": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    **m,
                    "is_drop_bar": False,
                    "is_diagnostic_selloff": False,
                }
            )

        eps = self.params["reference_low_invalidation_eps"]
        if self.active_reference_low is not None and l < float(self.active_reference_low) - eps:
            inv = {
                "bar_utc": ts,
                "reference_low": self.active_reference_low,
                "bar_low": l,
                "source_drop": self.active_reference_source,
            }
            self.invalidations.append(inv)
            state._advance_asof(ts)
            for pd in (1, 10):
                state.price_events_append(
                    {
                        "asof_utc": ts,
                        "pool_detail": pd,
                        "event_type": EVENT_REFERENCE_LOW_INVALIDATED,
                        "reference_low": self.active_reference_low,
                        "note": json.dumps(inv),
                    }
                )
            self.active_reference_low = None
            self.active_reference_source = None
            out["reference_invalidated"] = True

        dd_ok = m["drawdown_pct"] >= self.params["drop_min_drawdown_from_lookback_high_pct"]
        rng_ok = m["range_pct"] >= self.params["drop_min_single_bar_range_pct"]
        is_drop = dd_ok or rng_ok

        if is_drop:
            cand = {
                "drop_close_utc": ts,
                "reference_low": l,
                "drawdown_pct": m["drawdown_pct"],
                "bar_range_pct": m["range_pct"],
                "wick_lower_ratio": m["lower_wick_ratio"],
            }
            gap_h = self.params["drop_merge_gap_hours"]
            if self.merged_drop:
                prev_t = _parse_utc(self.merged_drop["drop_close_utc"])
                if (t - prev_t).total_seconds() / 3600 < gap_h:
                    if cand["drawdown_pct"] > self.merged_drop["drawdown_pct"]:
                        self.merged_drop = cand
                else:
                    self._finalize_drop(state, self.merged_drop)
                    self.merged_drop = cand
            else:
                self.merged_drop = cand
            if emit_features and self.feature_rows:
                self.feature_rows[-1]["is_drop_bar"] = True
            out["drop_candidate"] = True

        elif (
            m["is_red"]
            and m["lower_wick_ratio"] >= self.params["wick_lower_ratio_variant_a"]
            and self.calib_range_p90 is not None
            and m["range_pct"] >= self.calib_range_p90
            and not is_drop
        ):
            diag = {
                "bar_utc": ts,
                "low": l,
                "range_pct": m["range_pct"],
                "wick_lower_ratio": m["lower_wick_ratio"],
                "rule": "red_wick_ge_variant_a_and_range_ge_calib_p90_pre_august",
            }
            self.diagnostic_selloffs.append(diag)
            state._advance_asof(ts)
            for pd in (1, 10):
                state.price_events_append(
                    {
                        "asof_utc": ts,
                        "pool_detail": pd,
                        "event_type": EVENT_DIAG_SELLOFF,
                        "bottom": l,
                        "note": json.dumps(diag),
                        "diagnostic_only": True,
                    }
                )
            if emit_features and self.feature_rows:
                self.feature_rows[-1]["is_diagnostic_selloff"] = True
            out["diagnostic_selloff"] = True

        state._advance_asof(ts)
        return out

    def _finalize_drop(self, state: MarketMemoryState, drop: dict) -> None:
        ep = {
            "episode_id": f"drop:{drop['drop_close_utc']}",
            "reference_low": drop["reference_low"],
            "source_drop_close": drop["drop_close_utc"],
            "status": "ACTIVE",
            "drawdown_pct": drop["drawdown_pct"],
        }
        self.drop_episodes.append(ep)
        self.active_reference_low = float(drop["reference_low"])
        self.active_reference_source = drop["drop_close_utc"]
        ts = drop["drop_close_utc"]
        for pd in (1, 10):
            state.price_events_append(
                {
                    "asof_utc": ts,
                    "pool_detail": pd,
                    "event_type": EVENT_DROP_EVENT,
                    "bottom": drop["reference_low"],
                    "note": json.dumps(ep),
                }
            )
            state.record_drop_episode_reference(
                asof_utc=ts,
                pool_detail=pd,
                episode_id=ep["episode_id"],
                reference_low=float(drop["reference_low"]),
                source_drop_close=ts,
                status="ACTIVE",
                diagnostic_only=False,
            )

    def flush_pending_drop(self, state: MarketMemoryState) -> None:
        if self.merged_drop:
            self._finalize_drop(state, self.merged_drop)
            self.merged_drop = None


def attach_price_processor(state: MarketMemoryState) -> PriceStreamProcessor:
    params = load_drop_params()
    blob = state.price_processor or {}
    if blob:
        return PriceStreamProcessor.from_dict(blob, params)
    return PriceStreamProcessor(params=params)
