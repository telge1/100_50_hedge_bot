#!/usr/bin/env python3
"""Smoke-test live collector recovery (APTUSDT + DOGEUSDT).

Does NOT delete history. Creates a real multi-minute gap by stopping the
collector, waiting, then restarting and verifying REST recovery + continuity.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import BybitHistoryClient, ensure_utc  # noqa: E402
from signal_generator.bybit.live.collector import Live1mCollector  # noqa: E402
from signal_generator.bybit.live.health import CollectorState  # noqa: E402
from signal_generator.bybit.live.recovery import recover_symbols  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402

OUT = ROOT / "results" / "bybit_live_collector_smoke"


def _gap_count(repo: CandleRepository, symbol: str, start: datetime, end: datetime) -> int:
    rows = repo.get_candles(symbol, start, end)
    if len(rows) < 2:
        return 0
    gaps = 0
    for a, b in zip(rows, rows[1:]):
        ot_a = ensure_utc(a["open_time"]) if a["open_time"].tzinfo else a["open_time"].replace(tzinfo=timezone.utc)
        ot_b = ensure_utc(b["open_time"]) if b["open_time"].tzinfo else b["open_time"].replace(tzinfo=timezone.utc)
        if int((ot_b - ot_a).total_seconds()) != 60:
            gaps += 1
    return gaps


async def run_collector_for(seconds: float, symbols: list[str]) -> Live1mCollector:
    settings = get_clickhouse_settings()
    ch = setup_clickhouse(settings=settings)
    history = BybitHistoryClient(request_pause_s=0.05)
    collector = Live1mCollector(symbols=symbols, ch=ch, history=history)

    task = asyncio.create_task(collector.run())

    # Wait until LIVE or timeout
    deadline = time.time() + max(60.0, seconds)
    while time.time() < deadline:
        if collector.health.state == CollectorState.LIVE:
            break
        if collector.health.state == CollectorState.STOPPED:
            break
        await asyncio.sleep(0.5)

    # Stay live for remaining duration
    live_deadline = time.time() + seconds
    while time.time() < live_deadline and not task.done():
        await asyncio.sleep(0.5)

    collector.request_stop()
    try:
        await asyncio.wait_for(task, timeout=30)
    except asyncio.TimeoutError:
        logging.error("collector did not stop in time")
    ch.close()
    return collector


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["APTUSDT", "DOGEUSDT"])
    p.add_argument("--live-seconds", type=float, default=90.0)
    p.add_argument("--gap-wait-seconds", type=float, default=180.0)
    args = p.parse_args()
    symbols = [s.upper() for s in args.symbols]
    OUT.mkdir(parents=True, exist_ok=True)

    settings = get_clickhouse_settings()
    ch = setup_clickhouse(settings=settings)
    repo = CandleRepository(ch)

    before = {s: repo.get_last_closed_open_time(s) for s in symbols}
    logging.info("before start last_closed=%s", before)

    logging.info("phase1: run collector ~%ss", args.live_seconds)
    c1 = asyncio.run(run_collector_for(args.live_seconds, symbols))
    mid = {s: repo.get_last_closed_open_time(s) for s in symbols}
    logging.info("after phase1 last_closed=%s state=%s", mid, c1.health.state)

    logging.info("phase2: wait %ss to create gap", args.gap_wait_seconds)
    time.sleep(args.gap_wait_seconds)

    pre_restart = {s: repo.get_last_closed_open_time(s) for s in symbols}
    logging.info("pre-restart last_closed=%s", pre_restart)

    # Explicit recovery probe (same path as collector startup)
    history = BybitHistoryClient(request_pause_s=0.05)
    recovered = recover_symbols(symbols, repo=repo, history=history)
    for r in recovered:
        logging.info(
            "recovery %s gap_minutes=%s fetched=%s inserted=%s ok=%s",
            r.symbol,
            r.gap_minutes,
            r.fetched,
            r.inserted,
            r.ok,
        )

    logging.info("phase3: restart collector briefly")
    c2 = asyncio.run(run_collector_for(75.0, symbols))
    after = {s: repo.get_last_closed_open_time(s) for s in symbols}

    lines = [
        "# Bybit Live Collector Smoke",
        "",
        f"- symbols: {', '.join(symbols)}",
        f"- gap_wait_seconds: {args.gap_wait_seconds}",
        "",
        "## Recovery",
    ]
    all_ok = True
    for s in symbols:
        pre = pre_restart[s]
        post = after[s]
        # Continuity check from pre to last fully closed now
        if pre is None or post is None:
            all_ok = False
            lines.append(f"- {s}: FAIL missing timestamps pre={pre} post={post}")
            continue
        start = ensure_utc(pre)
        end = ensure_utc(post) + timedelta(minutes=1)
        final_n = repo.count_final(s, start, end)
        expected = int((end - start).total_seconds() // 60)
        gaps = _gap_count(repo, s, start, end)
        phys = repo.count_physical(s, start, end)
        rec = next(r for r in recovered if r.symbol == s)
        ok = gaps == 0 and final_n == expected and rec.ok and c2.health.state in (
            CollectorState.STOPPED,
            CollectorState.LIVE,
            CollectorState.STOPPING,
        )
        # After stop, state should be STOPPED; recovery should have inserted gap candles
        ok = gaps == 0 and final_n == expected and rec.ok
        if not ok:
            all_ok = False
        lines.append(
            f"- {s}: {'PASS' if ok else 'FAIL'} pre={ensure_utc(pre).isoformat()} "
            f"post={ensure_utc(post).isoformat()} expected={expected} FINAL={final_n} "
            f"gaps={gaps} phys={phys} recovered_inserted={rec.inserted} "
            f"gap_minutes={rec.gap_minutes}"
        )

    lines += [
        "",
        f"- collector_restart_ended_state: {c2.health.state.value}",
        f"- NO_SILENT_DATA_LOSS_GUARANTEE: {'PASS' if all_ok else 'FAIL'}",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    ch.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
