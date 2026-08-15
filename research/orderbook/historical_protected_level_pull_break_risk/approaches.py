"""Detect 1h/4h protected-level approaches and label outcomes (causal)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from research.orderbook.historical_protected_level_pull_break_risk import (
    COOLDOWN_MINUTES,
    ENTRY_BPS,
    MIN_NEAR_BPS_FOR_HOLD,
    OB_DAYS,
    ONE_M_ROOT,
    OUTCOME_HORIZON_MINUTES,
    REJECT_AWAY_BPS,
    REJECT_HOLD_MINUTES,
)
from research.orderbook.historical_structure_break_ob_deep_dive.inventory import (
    load_symbol_5m,
    run_structure_tf,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp


def _iso(ts: Any) -> str:
    t = ensure_utc_timestamp(pd.Timestamp(ts))
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_1m(symbol: str) -> pd.DataFrame:
    from research.backtests.candle_loader import symbol_to_feather_name
    import pyarrow.feather as feather

    path = ONE_M_ROOT / symbol_to_feather_name(symbol, timeframe="1m")
    raw = feather.read_table(path).to_pandas()
    if "date" in raw.columns and "timestamp" not in raw.columns:
        raw = raw.rename(columns={"date": "timestamp"})
    df = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


@dataclass
class ActiveLevel:
    symbol: str
    timeframe: str
    side: str  # low | high
    direction: str  # bearish approach to low / bullish approach to high
    level: float
    available_at_ms: int
    available_at: str


def build_active_level_timeline(struct: pd.DataFrame, *, symbol: str, timeframe: str) -> list[ActiveLevel]:
    """Emit active protected low/high after each closed HTF bar (causal)."""
    out: list[ActiveLevel] = []
    for _, row in struct.iterrows():
        avail = ensure_utc_timestamp(row["available_at"])
        avail_ms = int(avail.timestamp() * 1000)
        pl = row.get("protected_low")
        ph = row.get("protected_high")
        if pd.notna(pl):
            out.append(
                ActiveLevel(
                    symbol=symbol,
                    timeframe=timeframe,
                    side="low",
                    direction="bearish",
                    level=float(pl),
                    available_at_ms=avail_ms,
                    available_at=_iso(avail),
                )
            )
        if pd.notna(ph):
            out.append(
                ActiveLevel(
                    symbol=symbol,
                    timeframe=timeframe,
                    side="high",
                    direction="bullish",
                    level=float(ph),
                    available_at_ms=avail_ms,
                    available_at=_iso(avail),
                )
            )
    return out


def _dist_bps(price: float, level: float, *, side: str) -> float:
    """Signed distance: positive = still on safe side of level; negative = beyond."""
    if level <= 0:
        return 0.0
    if side == "low":
        # above low is safe; dist = (price - level)/level * 1e4
        return (price - level) / level * 1e4
    # high: below high is safe; dist = (level - price)/level * 1e4
    return (level - price) / level * 1e4


def _abs_dist_bps(price: float, level: float) -> float:
    if level <= 0:
        return 1e18
    return abs(price - level) / level * 1e4


@dataclass
class ApproachEpisode:
    approach_id: str
    symbol: str
    date: str
    timeframe: str
    side: str
    direction: str
    level: float
    level_available_at: str
    episode_start_ts: str
    episode_start_ms: int
    approach_50bps_ts: str | None = None
    approach_25bps_ts: str | None = None
    approach_10bps_ts: str | None = None
    approach_5bps_ts: str | None = None
    first_touch_ts: str | None = None
    first_break_ts: str | None = None
    reject_ts: str | None = None
    outcome: str = "AMBIGUOUS"
    min_abs_dist_bps: float | None = None
    overlap_cluster: str | None = None
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "approach_id": self.approach_id,
            "symbol": self.symbol,
            "date": self.date,
            "timeframe": self.timeframe,
            "side": self.side,
            "direction": self.direction,
            "level": self.level,
            "level_available_at": self.level_available_at,
            "episode_start_ts": self.episode_start_ts,
            "approach_50bps_ts": self.approach_50bps_ts,
            "approach_25bps_ts": self.approach_25bps_ts,
            "approach_10bps_ts": self.approach_10bps_ts,
            "approach_5bps_ts": self.approach_5bps_ts,
            "first_touch_ts": self.first_touch_ts,
            "first_break_ts": self.first_break_ts,
            "reject_ts": self.reject_ts,
            "outcome": self.outcome,
            "min_abs_dist_bps": self.min_abs_dist_bps,
            "overlap_cluster": self.overlap_cluster,
            "notes": self.notes,
        }


def _active_level_at(
    timeline: list[ActiveLevel], *, side: str, asof_ms: int
) -> ActiveLevel | None:
    """Latest level of this side with available_at <= asof."""
    cand = [x for x in timeline if x.side == side and x.available_at_ms <= asof_ms]
    if not cand:
        return None
    return max(cand, key=lambda x: x.available_at_ms)


def detect_episodes_for_day(
    *,
    symbol: str,
    day: str,
    bars_1m: pd.DataFrame,
    timeline_1h: list[ActiveLevel],
    timeline_4h: list[ActiveLevel],
) -> list[ApproachEpisode]:
    """Scan 1m bars for approach episodes on 1h and 4h active levels."""
    day_start = pd.Timestamp(f"{day}T00:00:00Z")
    day_end = day_start + pd.Timedelta(days=1)
    day_bars = bars_1m[(bars_1m["timestamp"] >= day_start) & (bars_1m["timestamp"] < day_end)].copy()
    if day_bars.empty:
        return []

    episodes: list[ApproachEpisode] = []
    # state per (tf, side)
    for timeframe, timeline in (("1h", timeline_1h), ("4h", timeline_4h)):
        for side in ("low", "high"):
            direction = "bearish" if side == "low" else "bullish"
            in_ep = False
            ep: ApproachEpisode | None = None
            cooldown_until_ms = 0
            away_since_ms: int | None = None
            reached_near = False
            min_abs = None
            last_level: float | None = None

            for _, row in day_bars.iterrows():
                ts = ensure_utc_timestamp(row["timestamp"])
                # use candle close as causal mid proxy at close time (= open+1m)
                close_ts = ts + pd.Timedelta(minutes=1)
                close_ms = int(close_ts.timestamp() * 1000)
                px = float(row["close"])
                hi = float(row["high"])
                lo = float(row["low"])

                lvl_obj = _active_level_at(timeline, side=side, asof_ms=close_ms)
                if lvl_obj is None:
                    continue
                level = lvl_obj.level

                # signed / abs distance using close
                signed = _dist_bps(px, level, side=side)
                abs_d = abs(signed)
                # Structure-aligned break: 1m close beyond protected level (causal at close).
                # Touch still uses extremes (wick).
                if side == "low":
                    broke = px < level
                    touched = lo <= level * (1 + 1e-12)
                else:
                    broke = px > level
                    touched = hi >= level * (1 - 1e-12)

                if not in_ep:
                    if close_ms < cooldown_until_ms:
                        continue
                    # approaching from safe side only (not already broken)
                    if abs_d <= ENTRY_BPS and signed >= -0.5 and not broke:
                        # start episode
                        aid = (
                            f"{symbol}_{timeframe}_{side}_{day.replace('-', '')}_"
                            f"{close_ts.strftime('%H%M%S')}_{level:.6g}"
                        ).replace(".", "p")
                        ep = ApproachEpisode(
                            approach_id=aid,
                            symbol=symbol,
                            date=day,
                            timeframe=timeframe,
                            side=side,
                            direction=direction,
                            level=level,
                            level_available_at=lvl_obj.available_at,
                            episode_start_ts=_iso(close_ts),
                            episode_start_ms=close_ms,
                            approach_50bps_ts=_iso(close_ts) if abs_d <= 50 else None,
                            min_abs_dist_bps=abs_d,
                        )
                        for thr, attr in (
                            (50.0, "approach_50bps_ts"),
                            (25.0, "approach_25bps_ts"),
                            (10.0, "approach_10bps_ts"),
                            (5.0, "approach_5bps_ts"),
                        ):
                            if abs_d <= thr and getattr(ep, attr) is None:
                                setattr(ep, attr, _iso(close_ts))
                        if touched:
                            ep.first_touch_ts = _iso(close_ts)
                        if abs_d <= MIN_NEAR_BPS_FOR_HOLD:
                            reached_near = True
                        in_ep = True
                        last_level = level
                        away_since_ms = None
                        min_abs = abs_d
                    continue

                assert ep is not None
                # level changed materially → end ambiguous / restart later
                if last_level is not None and abs(level - last_level) / last_level * 1e4 > 5:
                    ep.outcome = "AMBIGUOUS"
                    ep.notes = "level_changed_during_episode"
                    episodes.append(ep)
                    in_ep = False
                    ep = None
                    cooldown_until_ms = close_ms + COOLDOWN_MINUTES * 60_000
                    continue

                min_abs = abs_d if min_abs is None else min(min_abs, abs_d)
                ep.min_abs_dist_bps = min_abs
                for thr, attr in (
                    (50.0, "approach_50bps_ts"),
                    (25.0, "approach_25bps_ts"),
                    (10.0, "approach_10bps_ts"),
                    (5.0, "approach_5bps_ts"),
                ):
                    if abs_d <= thr and getattr(ep, attr) is None:
                        setattr(ep, attr, _iso(close_ts))
                if touched and ep.first_touch_ts is None:
                    ep.first_touch_ts = _iso(close_ts)
                if abs_d <= MIN_NEAR_BPS_FOR_HOLD:
                    reached_near = True

                if broke:
                    ep.first_break_ts = _iso(close_ts)
                    ep.outcome = "LEVEL_BREAK"
                    episodes.append(ep)
                    in_ep = False
                    ep = None
                    cooldown_until_ms = close_ms + COOLDOWN_MINUTES * 60_000
                    reached_near = False
                    continue

                # reject: moved away
                if abs_d >= REJECT_AWAY_BPS and reached_near:
                    if away_since_ms is None:
                        away_since_ms = close_ms
                        ep.reject_ts = _iso(close_ts)
                    elif close_ms - away_since_ms >= REJECT_HOLD_MINUTES * 60_000:
                        ep.outcome = "LEVEL_HOLD_REJECT"
                        episodes.append(ep)
                        in_ep = False
                        ep = None
                        cooldown_until_ms = close_ms + COOLDOWN_MINUTES * 60_000
                        reached_near = False
                        away_since_ms = None
                        continue
                else:
                    away_since_ms = None

                # horizon timeout
                if close_ms - ep.episode_start_ms >= OUTCOME_HORIZON_MINUTES * 60_000:
                    if reached_near and not broke:
                        ep.outcome = "LEVEL_HOLD_REJECT" if abs_d >= REJECT_AWAY_BPS / 2 else "AMBIGUOUS"
                    else:
                        ep.outcome = "AMBIGUOUS"
                    ep.notes = (ep.notes + "|horizon").strip("|")
                    episodes.append(ep)
                    in_ep = False
                    ep = None
                    cooldown_until_ms = close_ms + COOLDOWN_MINUTES * 60_000
                    reached_near = False

            if in_ep and ep is not None:
                ep.outcome = "AMBIGUOUS"
                ep.notes = (ep.notes + "|day_end").strip("|")
                episodes.append(ep)

    return episodes


def cluster_overlaps(episodes: list[ApproachEpisode]) -> None:
    """Mark 1h/4h overlaps on same symbol/side/level/time."""
    for i, a in enumerate(episodes):
        for b in episodes[i + 1 :]:
            if a.symbol != b.symbol or a.side != b.side:
                continue
            if abs(a.level - b.level) / max(a.level, 1e-12) * 1e4 > 15:
                continue
            if abs(a.episode_start_ms - b.episode_start_ms) > 45 * 60_000:
                continue
            cid = f"ov_{a.symbol}_{a.side}_{min(a.episode_start_ms, b.episode_start_ms)}"
            a.overlap_cluster = cid
            b.overlap_cluster = cid


def build_all_approaches() -> list[ApproachEpisode]:
    all_eps: list[ApproachEpisode] = []
    for symbol, days in OB_DAYS.items():
        ohlcv_5m = load_symbol_5m(symbol)
        day_ts = [pd.Timestamp(d, tz="UTC") for d in days]
        load_start = min(day_ts) - pd.Timedelta(days=14)
        load_end = max(day_ts) + pd.Timedelta(days=1)
        ohlcv_5m = ohlcv_5m[(ohlcv_5m["timestamp"] >= load_start) & (ohlcv_5m["timestamp"] < load_end)]
        struct_1h = run_structure_tf(ohlcv_5m, "1h", symbol=symbol)
        struct_4h = run_structure_tf(ohlcv_5m, "4h", symbol=symbol)
        tl_1h = build_active_level_timeline(struct_1h, symbol=symbol, timeframe="1h")
        tl_4h = build_active_level_timeline(struct_4h, symbol=symbol, timeframe="4h")
        bars_1m = load_1m(symbol)
        for day in days:
            eps = detect_episodes_for_day(
                symbol=symbol,
                day=day,
                bars_1m=bars_1m,
                timeline_1h=tl_1h,
                timeline_4h=tl_4h,
            )
            all_eps.extend(eps)
    cluster_overlaps(all_eps)
    return all_eps
