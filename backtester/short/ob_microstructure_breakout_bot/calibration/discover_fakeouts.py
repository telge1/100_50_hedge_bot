"""Phase B: discover EMA59 fakeouts for long and short (no YAML thresholds).

Finds first-in-cluster EMA59 touches that fail to follow through on closed
5m candles, then enriches with trade windows, EMA, and Full-OB — mirrored
for long (from_below) and short (from_above).
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

from ob_microstructure_breakout_bot.backtest.scan_touches import detect_ema59_touches
from ob_microstructure_breakout_bot.calibration.discover_strong_breakouts import (
    _median,
    _snapshot_at,
    _sum_bars,
    _true_range,
)
from ob_microstructure_breakout_bot.data.bars import Bar5m, load_5m_bars
from ob_microstructure_breakout_bot.data.ema_candles import _ema_series
from ob_microstructure_breakout_bot.models import (
    EmaSnapshot,
    ObBandSnapshot,
    TouchDirection,
    TradeWindowStats,
)

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class FakeoutParams:
    """Candle-only failure rules for EMA59 touches (long + short mirrored)."""

    hold_bars: int = 6  # ~30m after touch
    atr_bars: int = 24
    min_fail_ret: float = 0.0015  # |continuation| against setup
    cluster_gap_bars: int = 3  # match touch detector default
    only_first_in_cluster: bool = True
    confirm_bars: int = 2
    followthrough_bars: int = 2
    context_bars: int = 6
    # Skip timestamps already labeled as strong breakouts in Phase A.
    exclude_strong_gap_bars: int = 6


@dataclass(frozen=True)
class FakeoutCandidate:
    bar_ts: datetime
    direction: Direction  # setup that failed: long or short
    touch_direction: str
    score: float
    atr_mult: float
    body_ratio: float
    close_loc: float
    continuation_ret: float
    ended_against_ema59: bool
    full_reject: bool
    impulse_open: float
    impulse_high: float
    impulse_low: float
    impulse_close: float
    ema59: float
    fail_reasons: tuple[str, ...]


@dataclass
class EnrichedFakeoutEvent:
    symbol: str
    event_type: str
    direction: Direction
    touch_direction: str
    bar_ts: datetime
    decision_ts: datetime
    score: float
    atr_mult: float
    body_ratio: float
    close_loc: float
    continuation_ret: float
    ended_against_ema59: bool
    full_reject: bool
    fail_reasons: list[str]
    impulse_open: float
    impulse_high: float
    impulse_low: float
    impulse_close: float
    ema59: float
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
            "touch_direction": self.touch_direction,
            "bar_ts": self.bar_ts.isoformat(),
            "decision_ts": self.decision_ts.isoformat(),
            "score": self.score,
            "atr_mult": self.atr_mult,
            "body_ratio": self.body_ratio,
            "close_loc": self.close_loc,
            "continuation_ret": self.continuation_ret,
            "ended_against_ema59": self.ended_against_ema59,
            "full_reject": self.full_reject,
            "fail_reasons": list(self.fail_reasons),
            "impulse_open": self.impulse_open,
            "impulse_high": self.impulse_high,
            "impulse_low": self.impulse_low,
            "impulse_close": self.impulse_close,
            "ema59": self.ema59,
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


def _load_strong_bar_ts(path: Path | None) -> set[datetime]:
    if path is None or not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    out: set[datetime] = set()
    for ev in data.get("events", []):
        raw = ev.get("bar_ts")
        if not raw:
            continue
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        out.add(dt.astimezone(timezone.utc))
    return out


def discover_fakeouts(
    bars: list[Bar5m],
    *,
    start: datetime,
    end: datetime,
    params: FakeoutParams | None = None,
    exclude_bar_ts: set[datetime] | None = None,
) -> list[FakeoutCandidate]:
    """Find failing EMA59 touches for long and short setups."""
    p = params or FakeoutParams()
    exclude = exclude_bar_ts or set()
    if len(bars) < p.atr_bars + p.hold_bars + 5:
        return []

    touches = detect_ema59_touches(bars, cluster_gap_bars=p.cluster_gap_bars)
    by_ts = {b.ts: i for i, b in enumerate(bars)}

    trs: list[float] = []
    for i, bar in enumerate(bars):
        prev = bars[i - 1].close if i > 0 else None
        trs.append(_true_range(bar, prev))

    raw: list[FakeoutCandidate] = []
    for touch in touches:
        if touch.bar_ts < start or touch.bar_ts >= end:
            continue
        if p.only_first_in_cluster and not touch.is_first_in_cluster:
            continue
        idx = by_ts.get(touch.bar_ts)
        if idx is None:
            continue
        if idx + p.hold_bars >= len(bars):
            continue

        # Skip Phase-A strong breakouts (and nearby cluster mates).
        if any(
            abs((touch.bar_ts - s).total_seconds()) <= 5 * 60 * p.exclude_strong_gap_bars
            for s in exclude
        ):
            continue

        bar = bars[idx]
        hold = bars[idx + 1 : idx + 1 + p.hold_bars]
        if len(hold) < p.hold_bars:
            continue

        atr = _median(trs[max(0, idx - p.atr_bars) : idx])
        rng = bar.high - bar.low
        if atr <= 0 or rng <= 0:
            continue
        atr_mult = rng / atr
        body = abs(bar.close - bar.open)
        body_ratio = body / rng

        ema59 = touch.ema.ema59
        cont_ret = (hold[-1].close - bar.close) / bar.close

        if touch.direction == TouchDirection.FROM_BELOW:
            # Long setup attempt that fails.
            direction: Direction = "long"
            close_loc = (bar.close - bar.low) / rng
            ended_against = hold[-1].close < ema59
            full_reject = hold[-1].close < bar.low
            hard_fail = cont_ret <= -p.min_fail_ret
            reasons: list[str] = []
            if ended_against:
                reasons.append("close_below_ema59")
            if hard_fail:
                reasons.append("hard_negative_continuation")
            if full_reject:
                reasons.append("close_below_touch_low")
            # Need at least one clear failure signal against the long.
            if not (ended_against or hard_fail or full_reject):
                continue
            # If price still expands strongly long, this is not a fakeout.
            if cont_ret >= p.min_fail_ret and hold[-1].close > ema59 and not full_reject:
                continue
            score = (
                abs(min(cont_ret, 0.0)) * 100.0
                + (1.5 if ended_against else 0.0)
                + (2.0 if full_reject else 0.0)
                + max(0.0, 2.0 - atr_mult)  # weaker impulse → slightly higher fake weight
            )
        elif touch.direction == TouchDirection.FROM_ABOVE:
            # Short setup attempt that fails.
            direction = "short"
            close_loc = (bar.high - bar.close) / rng
            ended_against = hold[-1].close > ema59
            full_reject = hold[-1].close > bar.high
            hard_fail = cont_ret >= p.min_fail_ret
            reasons = []
            if ended_against:
                reasons.append("close_above_ema59")
            if hard_fail:
                reasons.append("hard_positive_continuation")
            if full_reject:
                reasons.append("close_above_touch_high")
            if not (ended_against or hard_fail or full_reject):
                continue
            if cont_ret <= -p.min_fail_ret and hold[-1].close < ema59 and not full_reject:
                continue
            score = (
                abs(max(cont_ret, 0.0)) * 100.0
                + (1.5 if ended_against else 0.0)
                + (2.0 if full_reject else 0.0)
                + max(0.0, 2.0 - atr_mult)
            )
        else:
            continue

        raw.append(
            FakeoutCandidate(
                bar_ts=bar.ts,
                direction=direction,
                touch_direction=touch.direction.value,
                score=score,
                atr_mult=atr_mult,
                body_ratio=body_ratio,
                close_loc=close_loc,
                continuation_ret=cont_ret,
                ended_against_ema59=ended_against,
                full_reject=full_reject,
                impulse_open=bar.open,
                impulse_high=bar.high,
                impulse_low=bar.low,
                impulse_close=bar.close,
                ema59=ema59,
                fail_reasons=tuple(reasons),
            )
        )

    # Dedup local clusters: keep highest-score fakeout per direction.
    kept: list[FakeoutCandidate] = []
    kept_idx_by_dir: dict[str, list[int]] = {"long": [], "short": []}
    for cand in sorted(raw, key=lambda c: c.score, reverse=True):
        idx = by_ts[cand.bar_ts]
        if any(abs(idx - j) <= p.cluster_gap_bars for j in kept_idx_by_dir[cand.direction]):
            continue
        kept_idx_by_dir[cand.direction].append(idx)
        kept.append(cand)
    kept.sort(key=lambda c: c.bar_ts)
    return kept


def enrich_fakeouts(
    symbol: str,
    bars: list[Bar5m],
    candidates: list[FakeoutCandidate],
    *,
    params: FakeoutParams | None = None,
    fetch_ob: bool = True,
) -> list[EnrichedFakeoutEvent]:
    p = params or FakeoutParams()
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    closes = [b.close for b in bars]
    e9 = _ema_series(closes, 9)
    e20 = _ema_series(closes, 20)
    e59 = _ema_series(closes, 59)
    e200 = _ema_series(closes, 200)

    out: list[EnrichedFakeoutEvent] = []
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
            EnrichedFakeoutEvent(
                symbol=symbol,
                event_type="fakeout",
                direction=cand.direction,
                touch_direction=cand.touch_direction,
                bar_ts=cand.bar_ts,
                decision_ts=decision_ts,
                score=cand.score,
                atr_mult=cand.atr_mult,
                body_ratio=cand.body_ratio,
                close_loc=cand.close_loc,
                continuation_ret=cand.continuation_ret,
                ended_against_ema59=cand.ended_against_ema59,
                full_reject=cand.full_reject,
                fail_reasons=list(cand.fail_reasons),
                impulse_open=cand.impulse_open,
                impulse_high=cand.impulse_high,
                impulse_low=cand.impulse_low,
                impulse_close=cand.impulse_close,
                ema59=cand.ema59,
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


def run_phase_b(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    client: Any | None = None,
    fetch_ob: bool = True,
    params: FakeoutParams | None = None,
    exclude_strong_path: Path | None = None,
) -> list[EnrichedFakeoutEvent]:
    p = params or FakeoutParams()
    warmup = start - timedelta(minutes=5 * 220)
    load_end = end + timedelta(
        minutes=5 * (p.hold_bars + p.confirm_bars + p.followthrough_bars + 2)
    )
    bars = load_5m_bars(symbol, warmup, load_end, client=client)
    if len(bars) < 200:
        raise RuntimeError(f"Not enough 5m bars for Phase B ({len(bars)})")
    exclude = _load_strong_bar_ts(exclude_strong_path)
    cands = discover_fakeouts(
        bars, start=start, end=end, params=p, exclude_bar_ts=exclude
    )
    return enrich_fakeouts(symbol, bars, cands, params=p, fetch_ob=fetch_ob)


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
    parser = argparse.ArgumentParser(
        description="Phase B: EMA59 fakeout discovery (long + short)"
    )
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--scan-from", required=True)
    parser.add_argument("--scan-to", required=True)
    parser.add_argument("--no-ob", action="store_true")
    parser.add_argument(
        "--exclude-strong",
        type=Path,
        default=None,
        help="Phase-A JSON to exclude overlapping strong breakouts",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)

    _load_dotenv()
    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    symbol = args.symbol.upper().replace("/", "")

    exclude_path = args.exclude_strong
    if exclude_path is None:
        default_a = (
            Path(__file__).resolve().parent
            / "events"
            / f"{symbol}_strong_breakouts_phase_a.json"
        )
        if default_a.exists():
            exclude_path = default_a

    params = FakeoutParams()
    events = run_phase_b(
        symbol,
        start,
        end,
        fetch_ob=not args.no_ob,
        params=params,
        exclude_strong_path=exclude_path,
    )

    payload = {
        "symbol": symbol,
        "scan_from": start.isoformat(),
        "scan_to": end.isoformat(),
        "params": asdict(params),
        "exclude_strong": str(exclude_path) if exclude_path else None,
        "n_events": len(events),
        "n_long": sum(1 for e in events if e.direction == "long"),
        "n_short": sum(1 for e in events if e.direction == "short"),
        "events": [e.to_dict() for e in events],
    }

    out = args.out
    if out is None:
        out_dir = Path(__file__).resolve().parent / "events"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{symbol}_fakeouts_phase_b.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.json_stdout:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Phase B {symbol}: {payload['n_events']} fakeouts "
            f"(long={payload['n_long']} short={payload['n_short']})"
        )
        for e in events:
            print(
                f"  {e.bar_ts.isoformat()}  {e.direction:5}  "
                f"touch={e.touch_direction:11}  score={e.score:.2f}  "
                f"atr×={e.atr_mult:.2f}  cont={e.continuation_ret:+.3%}  "
                f"Δc={e.confirm.delta_notional:+.0f}  "
                f"Δft={e.followthrough.delta_notional:+.0f}  "
                f"reasons={','.join(e.fail_reasons)}"
                + (f"  OB_ERR={e.ob_error}" if not e.ob_ok else "")
            )
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
