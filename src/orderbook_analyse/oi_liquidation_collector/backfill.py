"""Restartable Bybit REST 5-minute OI history backfill. Does not stop live WS."""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import CATEGORY, EXCHANGE, SOURCE_REST_5M
from .logic import to_decimal, utc_now
from .schema import apply_schema
from .settings import DEFAULT_BACKFILL_DIR, load_oi_settings, redact_settings
from .universe import fetch_bybit_linear_usdt_perps, plan_universe
from .writer import AllowlistedWriter, assert_table_allowed

logger = logging.getLogger(__name__)


def _get_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    req = Request(url, headers={"User-Agent": "oi-liquidation-collector/1"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_open_interest_page(
    rest_url: str,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    cursor: str = "",
) -> tuple[list[dict[str, Any]], str]:
    params = {
        "category": "linear",
        "symbol": symbol,
        "intervalTime": "5min",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "200",
    }
    if cursor:
        params["cursor"] = cursor
    url = rest_url.rstrip("/") + "/v5/market/open-interest?" + urlencode(params)
    payload = _get_json(url)
    if payload.get("retCode") not in (0, None):
        raise RuntimeError(payload.get("retMsg") or "open-interest failed")
    result = payload.get("result") or {}
    return list(result.get("list") or []), str(result.get("nextPageCursor") or "").strip()


def checkpoint_path(root: Path, symbol: str) -> Path:
    return root / f"{symbol}.json"


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def save_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def rows_from_list(
    symbol: str, items: list[dict[str, Any]], instance_id: str
) -> list[dict[str, Any]]:
    rows = []
    now = utc_now()
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp")
        oi = to_decimal(item.get("openInterest"))
        oiv = to_decimal(item.get("openInterestValue"))
        if ts is None or oi is None:
            continue
        bucket = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
        rows.append(
            {
                "exchange": EXCHANGE,
                "category": CATEGORY,
                "symbol": symbol,
                "bucket_time": bucket,
                "open_interest": oi,
                "open_interest_value": oiv,
                "source": SOURCE_REST_5M,
                "collector_instance_id": instance_id,
                "inserted_at": now,
            }
        )
    return rows


def run_backfill(
    *,
    symbols: tuple[str, ...],
    days: int,
    rest_url: str,
    writer: AllowlistedWriter,
    checkpoint_dir: Path,
    instance_id: str,
    min_interval_sec: float = 0.25,
) -> dict[str, Any]:
    end = utc_now()
    start = end - timedelta(days=days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    summary: dict[str, Any] = {"symbols": {}, "source": SOURCE_REST_5M, "days": days}
    for symbol in symbols:
        path = checkpoint_path(checkpoint_dir, symbol)
        ck = load_checkpoint(path)
        cursor = str(ck.get("cursor") or "")
        done = bool(ck.get("done"))
        inserted = int(ck.get("inserted") or 0)
        if done:
            summary["symbols"][symbol] = {"skipped": True, "inserted": inserted}
            continue
        pages = 0
        while True:
            backoff = 1.0
            items: list[dict[str, Any]] = []
            for _attempt in range(8):
                try:
                    items, cursor = fetch_open_interest_page(
                        rest_url,
                        symbol=symbol,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        cursor=cursor,
                    )
                    break
                except Exception as exc:
                    logger.warning("rest retry %s %s", symbol, type(exc).__name__)
                    time.sleep(backoff + random.random() * 0.3)
                    backoff = min(16.0, backoff * 2)
            else:
                raise RuntimeError(f"backfill failed for {symbol}")
            recs = rows_from_list(symbol, items, instance_id)
            if recs:
                raise RuntimeError("run_backfill requires an already-running async writer; use main()")
            pages += 1
            ck = {
                "symbol": symbol,
                "cursor": cursor,
                "inserted": inserted,
                "pages": pages,
                "done": not cursor,
                "updated_utc": utc_now().isoformat(),
            }
            save_checkpoint(path, ck)
            time.sleep(min_interval_sec + random.random() * 0.05)
            if not cursor:
                break
        summary["symbols"][symbol] = {"inserted": inserted, "pages": pages, "done": True}
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bybit REST 5m OI history backfill")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    settings = load_oi_settings()
    logger.info("backfill settings %s", redact_settings(settings))
    bybit = fetch_bybit_linear_usdt_perps(settings.bybit_rest_url)
    plan = plan_universe(universe_path=settings.universe_path, bybit_symbols=bybit)
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip()) or plan.supported
    from .collector import default_client_factory, make_instance_id
    import asyncio

    factory = default_client_factory(settings)
    client = factory()
    apply_schema(client)
    client.close()
    writer = AllowlistedWriter(client_factory=factory, batch_size=200, flush_interval_sec=0.5)
    async def _run() -> dict[str, Any]:
        await writer.start()
        try:
            # Use thread inserts by enqueueing then stopping (flush)
            end = utc_now()
            start = end - timedelta(days=args.days)
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            instance_id = make_instance_id()
            summary: dict[str, Any] = {"source": SOURCE_REST_5M, "days": args.days, "symbols": {}}
            for symbol in symbols:
                path = checkpoint_path(DEFAULT_BACKFILL_DIR, symbol)
                ck = load_checkpoint(path)
                cursor = str(ck.get("cursor") or "")
                inserted = int(ck.get("inserted") or 0)
                if ck.get("done"):
                    summary["symbols"][symbol] = {"skipped": True, "inserted": inserted}
                    continue
                pages = 0
                while True:
                    backoff = 1.0
                    items: list[dict[str, Any]] = []
                    fetched = False
                    for _attempt in range(8):
                        try:
                            items, cursor = fetch_open_interest_page(
                                settings.bybit_rest_url,
                                symbol=symbol,
                                start_ms=start_ms,
                                end_ms=end_ms,
                                cursor=cursor,
                            )
                            fetched = True
                            break
                        except Exception:
                            logger.warning("rest retry %s %s", symbol, _attempt + 1)
                            await asyncio.sleep(backoff + random.random() * 0.3)
                            backoff = min(16.0, backoff * 2)
                    if not fetched:
                        raise RuntimeError(f"backfill failed for {symbol}")
                    recs = rows_from_list(symbol, items, instance_id)
                    if recs:
                        await writer.enqueue("open_interest_5m_history", recs)
                        inserted += len(recs)
                    pages += 1
                    save_checkpoint(
                        path,
                        {
                            "symbol": symbol,
                            "cursor": cursor,
                            "inserted": inserted,
                            "pages": pages,
                            "done": not cursor,
                            "updated_utc": utc_now().isoformat(),
                        },
                    )
                    await asyncio.sleep(0.25 + random.random() * 0.05)
                    if not cursor:
                        break
                summary["symbols"][symbol] = {"inserted": inserted, "pages": pages, "done": True}
                logger.info("backfill %s inserted=%s pages=%s", symbol, inserted, pages)
            return summary
        finally:
            await writer.stop()

    summary = asyncio.run(_run())
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
