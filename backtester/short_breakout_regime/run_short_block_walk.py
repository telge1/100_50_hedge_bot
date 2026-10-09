"""Walk the short blocker from a start timestamp through now.

Example:
  PYTHONPATH=backtester python -m short_breakout_regime.run_short_block_walk \
    --start 2026-09-20T00:00:00Z
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from short_breakout_regime.replay import BlockSample, OhlcBar, TimedPool, block_ranges, walk_hours


def _paths() -> None:
    root = Path(__file__).resolve().parents[2]
    short = root / "backtester" / "short"
    extra = [
        short / "dashboard",
        short,
        Path("/home/telgenbuescher/projects/orderbook_analyse/src"),
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src"),
        root,
    ]
    for path in extra:
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pools_from_engine(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[TimedPool]:
    from dashboard.research_charts.service import _candles_from_packed, lld_config_for_timeframe, load_candles
    from dashboard.research_charts.trp_import import load_trp

    warm = start - timedelta(days=45)
    packed = load_candles(
        symbol,
        timeframe,
        start=int(warm.timestamp()),
        end=int(end.timestamp()),
        limit=50000,
    )
    candles = _candles_from_packed(packed)
    trp = load_trp()
    cfg = lld_config_for_timeframe(trp["LiquidityLocationConfig"].defaults(), timeframe)
    result = trp["run_liquidity_location"](candles, cfg)
    pools: list[TimedPool] = []
    for pool in getattr(result, "pools_all", None) or result.pools:
        born = getattr(pool, "created_timestamp", None)
        if born is None:
            continue
        invalidated = getattr(pool, "invalidated_timestamp", None)
        pools.append(
            TimedPool(
                bottom=float(pool.bottom_price),
                top=float(pool.top_price),
                strength=float(pool.strength or 0.0),
                born_ts=born if born.tzinfo else born.replace(tzinfo=timezone.utc),
                invalidated_ts=(
                    invalidated
                    if invalidated is None or invalidated.tzinfo
                    else invalidated.replace(tzinfo=timezone.utc)
                ),
            )
        )
    return pools


def _load(symbol: str, start: datetime, end: datetime) -> tuple[list[OhlcBar], list[TimedPool]]:
    _paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles

    warm = start - timedelta(days=45)
    packed = load_candles(
        symbol,
        "5m",
        start=int(warm.timestamp()),
        end=int(end.timestamp()),
        limit=50000,
    )
    bars = [
        OhlcBar(
            ts=c.timestamp,
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
        )
        for c in _candles_from_packed(packed)
    ]
    # 4h zones are the leftover shelf and the older pools above it.
    return bars, _pools_from_engine(symbol, "4h", start, end)


def _print_report(samples: list[BlockSample]) -> None:
    ranges = block_ranges(samples)
    blocked = [row for row in samples if row.block_short]
    print(f"kerzen_4h={len(samples)}  kerzen_blockiert={len(blocked)}  bloecke={len(ranges)}")
    if not ranges:
        print("kein Short-Block in diesem Fenster")
        return
    for item in ranges:
        first = next(row for row in samples if row.ts == item.start and row.block_short)
        release = next((row for row in samples if row.ts > item.end and not row.block_short), None)
        launch = f" ausbruch {_iso(first.breakout_ts)}" if first.breakout_ts is not None else ""
        opened = f"SHORT BLOCK {_iso(item.start)}{launch}"
        if release is None:
            print(f"{opened} | freigabe noch offen, zuletzt blockiert {_iso(item.end)}")
        else:
            print(f"{opened} | freigabe {_iso(release.ts)} {release.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Short-Blocker von einem Startzeitpunkt bis jetzt laufen lassen.")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--start", required=True, help="ISO-Zeit, z.B. 2026-09-20T00:00:00Z")
    parser.add_argument("--end", default="", help="ISO-Zeit, Standard ist jetzt")
    args = parser.parse_args(argv)
    start = _parse_ts(args.start)
    end = _parse_ts(args.end) if args.end else datetime.now(timezone.utc)
    bars, pools = _load(args.symbol, start, end)
    print(f"symbol={args.symbol} start={_iso(start)} end={_iso(end)} bars_5m={len(bars)} pools={len(pools)}")
    samples = walk_hours(start=start, end=end, bars_5m=bars, pools=pools)
    _print_report(samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
