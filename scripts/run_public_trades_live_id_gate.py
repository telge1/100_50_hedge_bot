#!/usr/bin/env python3
"""Isolated DOGEUSDT publicTrade capture for the live-id gate.

Does not stop the running candle collector. Writes JSONL only — no ClickHouse insert.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.live.ws_kline import BYBIT_LINEAR_WS_URL  # noqa: E402
from signal_generator.bybit.live.ws_public_trade import (  # noqa: E402
    parse_ws_public_trade_payload,
    public_trade_topic_for_symbol,
)


async def capture(*, duration_s: float, out_path: Path, symbol: str) -> dict:
    import websockets

    topic = public_trade_topic_for_symbol(symbol)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    started = datetime.now(timezone.utc)
    async with websockets.connect(BYBIT_LINEAR_WS_URL, ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
        deadline = asyncio.get_event_loop().time() + duration_s
        with out_path.open("w", encoding="utf-8") as fh:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                for trade in parse_ws_public_trade_payload(payload):
                    rec = {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "price": str(trade.price),
                        "size": str(trade.size),
                        "trade_ts": trade.trade_ts.isoformat(),
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    }
                    fh.write(json.dumps(rec) + "\n")
                    n += 1
    ended = datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "topic": topic,
        "rows": n,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "out_path": str(out_path),
        "note": "JSONL dry-run buffer only; no ClickHouse writes; collector untouched.",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="DOGEUSDT")
    p.add_argument("--duration-s", type=float, default=300.0)
    p.add_argument(
        "--out",
        type=Path,
        default=ROOT / "results" / "public_trades_51_coin_7d" / "live_id_gate_capture.jsonl",
    )
    args = p.parse_args(argv)
    summary = asyncio.run(
        capture(duration_s=args.duration_s, out_path=args.out, symbol=args.symbol.upper())
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
