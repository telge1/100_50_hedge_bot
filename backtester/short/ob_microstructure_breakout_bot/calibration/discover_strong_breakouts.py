"""Phase A: discover strong candle breakouts (no YAML thresholds).

Looks only at closed 5m OHLC structure first, then optionally enriches each
event with EMA snapshots, public-trade windows, and Full-OB bands.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_REPO),
]
for _p in reversed(_EXTRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ob_microstructure_breakout_bot.data.bars import Bar5m, load_5m_bars
from ob_microstructure_breakout_bot.data.ema_candles import _ema_series
from ob_microstructure_breakout_bot.models import EmaSnapshot, ObBandSnapshot, TradeWindowStats

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class StrongBreakoutCandidate:
    """Candle-structure candidate before enrichment."""

    bar_ts: datetime
    direction: Direction
    score: float
    atr_mult: float
    body_ratio: float
    close_loc: float
    break_prior_high: bool
    break_prior_low: bool
    continuation_ret: float
    impulse_open: float
    impulse_high: float
    impulse_low: float
    impulse_close: float
    prior_structure_level: float
    ema59_dist_bps: float | None
    near_ema59: bool


@dataclass
class EnrichedBreakoutEvent:
    """Phase-A event row ready for calibration storage."""

    symbol: str
    event_type: str
    direction: Direction
    bar_ts: datetime
    decision_ts: datetime
    score: float
    atr_mult: float
    body_ratio: float
    close_loc: float
    continuation_ret: float
    near_ema59: bool
    ema59_dist_bps: float | None
    impulse_open: float
    impulse_high: float
    impulse_low: float
    impulse_close: float
    prior_structure_level: float
    context: TradeWindowStats
    confirm: TradeWindowStats
    followthrough: TradeWindowStats
    ema: EmaSnapshot | None
    ob: ObBandSnapshot | None
    ob_ok: bool
    ob_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def _win(w: TradeWindowStats) -> dict[str, Any]:
            return {
                "buy_notional": w.buy_notional,
                "sell_notional": w.sell_notional,
                "delta_notional": w.delta_notional,
                "trade_count": w.trade_count,
                "open_price": w.open_price,
                "close_price": w.close_price,
                "high": w.high,
                "low": w.low,
            }

        ob_d: dict[str, Any] | None = None
        if self.ob is not None:
            ob_d = {
                "bid_5bps": self.ob.bid_5bps,
                "ask_5bps": self.ob.ask_5bps,
                "bid_10bps": self.ob.bid_10bps,
                "ask_10bps": self.ob.ask_10bps,
                "bid_ask_ratio_5bps": self.ob.bid_ask_ratio_5bps,
            }
        ema_d: dict[str, Any] | None = None
        if self.ema is not None:
            ema_d = {
                "ema9": self.ema.ema9,
                "ema20": self.ema.ema20,
                "ema59": self.ema.ema59,
                "ema200": self.ema.ema200,
                "price": self.ema.price,
                "bullish_stack": self.ema.bullish_stack,
            }
        return {
            "symbol": self.symbol,
            "event_type": self.event_type,
            "direction": self.direction,
            "bar_ts": self.bar_ts.isoformat(),
            "decision_ts": self.decision_ts.isoformat(),
            "score": self.score,
            "atr_mult": self.atr_mult,
            "body_ratio": self.body_ratio,
            "close_loc": self.close_loc,
            "continuation_ret": self.continuation_ret,
            "near_ema59": self.near_ema59,
            "ema59_dist_bps": self.ema59_dist_bps,
            "impulse_open": self.impulse_open,
            "impulse_high": self.impulse_high,
            "impulse_low": self.impulse_low,
            "impulse_close": self.impulse_close,
            "prior_structure_level": self.prior_structure_level,
            "confirm_delta": self.confirm.delta_notional,
            "followthrough_delta": self.followthrough.delta_notional,
            "context_delta": self.context.delta_notional,
            "context": _win(self.context),
            "confirm": _win(self.confirm),
            "followthrough": _win(self.followthrough),
            "ema": ema_d,
            "ob": ob_d,
            "ob_ok": self.ob_ok,
            "ob_error": self.ob_error,
        }


@dataclass(frozen=True)
class DiscoveryParams:
    """Candle-only filters for strong breakouts (coin-agnostic relative rules)."""

    lookback_bars: int = 12  # local structure window (~1h)
    atr_bars: int = 24  # median true-range lookback (~2h)
    min_atr_mult: float = 2.0
    min_body_ratio: float = 0.55
    min_close_loc: float = 0.70  # long: close near high; short: near low
    hold_bars: int = 6  # continuation horizon (~30m)
    min_continuation_ret: float = 0.0015  # +0.15% / -0.15%
    max_adverse_frac: float = 0.50  # adverse excursion vs impulse range
    ema59_near_bps: float = 25.0  # mark near-EMA59 if within 25 bps
    require_near_ema59: bool = False
    cluster_gap_bars: int = 6
    confirm_bars: int = 2
    followthrough_bars: int = 2
    context_bars: int = 6


def _true_range(bar: Bar5m, prev_close: float | None) -> float:
    if prev_close is None:
        return bar.high - bar.low
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    mid = len(ys) // 2
    if len(ys) % 2:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def _sum_bars(bars: list[Bar5m]) -> TradeWindowStats:
    if not bars:
        return TradeWindowStats(0.0, 0.0)
    return TradeWindowStats(
        buy_notional=sum(b.buy_notional for b in bars),
        sell_notional=sum(b.sell_notional for b in bars),
        trade_count=sum(b.trade_count for b in bars),
        open_price=bars[0].open,
        close_price=bars[-1].close,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
    )


def _snapshot_at(
    bars: list[Bar5m],
    idx: int,
    e9: list[float | None],
    e20: list[float | None],
    e59: list[float | None],
    e200: list[float | None],
) -> EmaSnapshot | None:
    if e9[idx] is None or e20[idx] is None or e59[idx] is None:
        return None
    return EmaSnapshot(
        ema9=float(e9[idx]),
        ema20=float(e20[idx]),
        ema59=float(e59[idx]),
        price=bars[idx].close,
        ema200=None if e200[idx] is None else float(e200[idx]),
    )


def discover_strong_breakouts(
    bars: list[Bar5m],
    *,
    start: datetime,
    end: datetime,
    params: DiscoveryParams | None = None,
) -> list[StrongBreakoutCandidate]:
    """Find strong long/short candle breakouts inside ``[start, end)``.

    Uses only closed-bar OHLC structure + EMA59 proximity metadata.
    Does **not** use coin YAML thresholds.
    """
    p = params or DiscoveryParams()
    if len(bars) < max(p.lookback_bars, p.atr_bars) + p.hold_bars + 2:
        return []

    closes = [b.close for b in bars]
    e59 = _ema_series(closes, 59)

    trs: list[float] = []
    for i, bar in enumerate(bars):
        prev = bars[i - 1].close if i > 0 else None
        trs.append(_true_range(bar, prev))

    raw: list[StrongBreakoutCandidate] = []
    for i in range(p.lookback_bars, len(bars) - p.hold_bars):
        bar = bars[i]
        if bar.ts < start or bar.ts >= end:
            continue

        atr = _median(trs[i - p.atr_bars : i])
        if atr <= 0:
            continue
        rng = bar.high - bar.low
        if rng <= 0:
            continue
        atr_mult = rng / atr
        if atr_mult < p.min_atr_mult:
            continue

        body = abs(bar.close - bar.open)
        body_ratio = body / rng
        if body_ratio < p.min_body_ratio:
            continue

        prior = bars[i - p.lookback_bars : i]
        prior_high = max(b.high for b in prior)
        prior_low = min(b.low for b in prior)
        hold = bars[i + 1 : i + 1 + p.hold_bars]
        if len(hold) < p.hold_bars:
            continue

        ema59 = e59[i]
        ema_dist: float | None = None
        near = False
        if ema59 is not None and ema59 > 0:
            # Distance of impulse low/high band to EMA59 in bps.
            if bar.low <= ema59 <= bar.high:
                ema_dist = 0.0
            else:
                nearest = bar.low if abs(bar.low - ema59) < abs(bar.high - ema59) else bar.high
                ema_dist = abs(nearest - ema59) / ema59 * 10_000.0
            near = ema_dist <= p.ema59_near_bps
        if p.require_near_ema59 and not near:
            continue

        # ---- long ----
        close_loc_long = (bar.close - bar.low) / rng
        if (
            bar.close > bar.open
            and close_loc_long >= p.min_close_loc
            and bar.close > prior_high
            and bar.high >= prior_high
        ):
            cont_ret = (hold[-1].close - bar.close) / bar.close
            adverse = min(b.low for b in hold)
            adverse_frac = (bar.close - adverse) / rng if bar.close > adverse else 0.0
            if (
                cont_ret >= p.min_continuation_ret
                and adverse_frac <= p.max_adverse_frac
            ):
                score = atr_mult * body_ratio * close_loc_long * (1.0 + max(cont_ret, 0.0) * 100.0)
                raw.append(
                    StrongBreakoutCandidate(
                        bar_ts=bar.ts,
                        direction="long",
                        score=score,
                        atr_mult=atr_mult,
                        body_ratio=body_ratio,
                        close_loc=close_loc_long,
                        break_prior_high=True,
                        break_prior_low=False,
                        continuation_ret=cont_ret,
                        impulse_open=bar.open,
                        impulse_high=bar.high,
                        impulse_low=bar.low,
                        impulse_close=bar.close,
                        prior_structure_level=prior_high,
                        ema59_dist_bps=ema_dist,
                        near_ema59=near,
                    )
                )

        # ---- short ----
        close_loc_short = (bar.high - bar.close) / rng
        if (
            bar.close < bar.open
            and close_loc_short >= p.min_close_loc
            and bar.close < prior_low
            and bar.low <= prior_low
        ):
            cont_ret = (hold[-1].close - bar.close) / bar.close  # negative is good
            adverse = max(b.high for b in hold)
            adverse_frac = (adverse - bar.close) / rng if adverse > bar.close else 0.0
            if (
                cont_ret <= -p.min_continuation_ret
                and adverse_frac <= p.max_adverse_frac
            ):
                score = (
                    atr_mult
                    * body_ratio
                    * close_loc_short
                    * (1.0 + max(-cont_ret, 0.0) * 100.0)
                )
                raw.append(
                    StrongBreakoutCandidate(
                        bar_ts=bar.ts,
                        direction="short",
                        score=score,
                        atr_mult=atr_mult,
                        body_ratio=body_ratio,
                        close_loc=close_loc_short,
                        break_prior_high=False,
                        break_prior_low=True,
                        continuation_ret=cont_ret,
                        impulse_open=bar.open,
                        impulse_high=bar.high,
                        impulse_low=bar.low,
                        impulse_close=bar.close,
                        prior_structure_level=prior_low,
                        ema59_dist_bps=ema_dist,
                        near_ema59=near,
                    )
                )

    # Keep strongest event per local cluster (per direction).
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    kept: list[StrongBreakoutCandidate] = []
    kept_idx_by_dir: dict[str, list[int]] = {"long": [], "short": []}
    for cand in sorted(raw, key=lambda c: c.score, reverse=True):
        idx = by_ts[cand.bar_ts]
        if any(abs(idx - j) <= p.cluster_gap_bars for j in kept_idx_by_dir[cand.direction]):
            continue
        kept_idx_by_dir[cand.direction].append(idx)
        kept.append(cand)
    kept.sort(key=lambda c: c.bar_ts)
    return kept


def enrich_candidates(
    symbol: str,
    bars: list[Bar5m],
    candidates: list[StrongBreakoutCandidate],
    *,
    params: DiscoveryParams | None = None,
    fetch_ob: bool = True,
) -> list[EnrichedBreakoutEvent]:
    """Attach context / confirm / follow-through / EMA / Full-OB to candidates."""
    p = params or DiscoveryParams()
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    closes = [b.close for b in bars]
    e9 = _ema_series(closes, 9)
    e20 = _ema_series(closes, 20)
    e59 = _ema_series(closes, 59)
    e200 = _ema_series(closes, 200)

    out: list[EnrichedBreakoutEvent] = []
    for cand in candidates:
        idx = by_ts.get(cand.bar_ts)
        if idx is None:
            continue
        ctx = bars[max(0, idx - p.context_bars) : idx]
        confirm = bars[idx : idx + p.confirm_bars]
        ft = bars[idx + p.confirm_bars : idx + p.confirm_bars + p.followthrough_bars]
        if len(confirm) < p.confirm_bars or len(ft) < p.followthrough_bars:
            continue
        decision_ts = ft[-1].ts + timedelta(minutes=5)
        ema = _snapshot_at(bars, idx, e9, e20, e59, e200)

        ob: ObBandSnapshot | None = None
        ob_ok = True
        ob_error: str | None = None
        if fetch_ob:
            try:
                from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands

                ob = sample_ob_bands(symbol, cand.bar_ts)
            except Exception as exc:  # noqa: BLE001
                ob_ok = False
                ob_error = str(exc)

        out.append(
            EnrichedBreakoutEvent(
                symbol=symbol,
                event_type="strong_breakout",
                direction=cand.direction,
                bar_ts=cand.bar_ts,
                decision_ts=decision_ts,
                score=cand.score,
                atr_mult=cand.atr_mult,
                body_ratio=cand.body_ratio,
                close_loc=cand.close_loc,
                continuation_ret=cand.continuation_ret,
                near_ema59=cand.near_ema59,
                ema59_dist_bps=cand.ema59_dist_bps,
                impulse_open=cand.impulse_open,
                impulse_high=cand.impulse_high,
                impulse_low=cand.impulse_low,
                impulse_close=cand.impulse_close,
                prior_structure_level=cand.prior_structure_level,
                context=_sum_bars(ctx),
                confirm=_sum_bars(confirm),
                followthrough=_sum_bars(ft),
                ema=ema,
                ob=ob,
                ob_ok=ob_ok,
                ob_error=ob_error,
            )
        )
    return out


def run_phase_a(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    client: Any | None = None,
    fetch_ob: bool = True,
    params: DiscoveryParams | None = None,
) -> list[EnrichedBreakoutEvent]:
    """Load bars, discover strong candle breakouts, enrich them."""
    p = params or DiscoveryParams()
    warmup = start - timedelta(minutes=5 * 220)
    # Need hold_bars after last candidate; extend load end a bit past scan end.
    load_end = end + timedelta(minutes=5 * (p.hold_bars + p.confirm_bars + p.followthrough_bars + 2))
    bars = load_5m_bars(symbol, warmup, load_end, client=client)
    if len(bars) < 200:
        raise RuntimeError(f"Not enough 5m bars for Phase A ({len(bars)})")
    cands = discover_strong_breakouts(bars, start=start, end=end, params=p)
    return enrich_candidates(symbol, bars, cands, params=p, fetch_ob=fetch_ob)


def _load_dotenv() -> None:
    env_path = Path(
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
        "signal_generator_stoch_waves/.env"
    )
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass


def _parse_utc(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase A: strong candle breakout discovery")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--scan-from", required=True)
    parser.add_argument("--scan-to", required=True)
    parser.add_argument("--no-ob", action="store_true")
    parser.add_argument("--require-near-ema59", action="store_true")
    parser.add_argument("--min-atr-mult", type=float, default=2.0)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON output path (default: calibration/events/<symbol>_strong_breakouts_<ts>.json)",
    )
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    _load_dotenv()
    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    params = DiscoveryParams(
        min_atr_mult=args.min_atr_mult,
        require_near_ema59=args.require_near_ema59,
    )
    events = run_phase_a(
        args.symbol.upper().replace("/", ""),
        start,
        end,
        fetch_ob=not args.no_ob,
        params=params,
    )

    payload = {
        "symbol": args.symbol.upper().replace("/", ""),
        "scan_from": start.isoformat(),
        "scan_to": end.isoformat(),
        "params": asdict(params),
        "n_events": len(events),
        "n_long": sum(1 for e in events if e.direction == "long"),
        "n_short": sum(1 for e in events if e.direction == "short"),
        "n_near_ema59": sum(1 for e in events if e.near_ema59),
        "events": [e.to_dict() for e in events],
    }

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(__file__).resolve().parent / "events"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{payload['symbol']}_strong_breakouts_{stamp}.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.json_stdout:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Phase A {payload['symbol']}: {payload['n_events']} strong breakouts "
            f"(long={payload['n_long']} short={payload['n_short']} "
            f"near_ema59={payload['n_near_ema59']})"
        )
        for e in events:
            print(
                f"  {e.bar_ts.isoformat()}  {e.direction:5}  score={e.score:.2f}  "
                f"atr×={e.atr_mult:.2f}  body={e.body_ratio:.2f}  "
                f"cont={e.continuation_ret:+.3%}  near_ema59={e.near_ema59}  "
                f"Δc={e.confirm.delta_notional:+.0f}  Δft={e.followthrough.delta_notional:+.0f}"
                + (f"  OB_ERR={e.ob_error}" if not e.ob_ok else "")
            )
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
