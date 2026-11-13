#!/usr/bin/env python3
"""DOGE-LIVE-P4-LIVE — catch-up + live tick for 5m PD1 on the production DOGE line."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASH = REPO / "backtester" / "short" / "dashboard"
LIVE_TOOL = Path(__file__).resolve().parent
HIST_TOOL = REPO / "tools" / "doge_live_p4_hist_publish_v1"
sys.path.insert(0, str(DASH))
sys.path.insert(0, str(LIVE_TOOL))
sys.path.insert(0, str(HIST_TOOL))

from research_charts.trp_import import ensure_trp_on_path  # noqa: E402

ensure_trp_on_path()

from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config  # noqa: E402
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.close_sequence import (  # noqa: E402
    missing_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (  # noqa: E402
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_runner import (  # noqa: E402
    extended_oracle_check,
    process_rolling_close,
)
from research_charts.mc3a_historical_runner_v1 import parse_utc  # noqa: E402
from research_charts.multicoin_pd1_scope_v1 import (  # noqa: E402
    DOGE_PROTECTED_5M_PD1_LINE,
    assert_safe_write_target,
)

from common import (  # noqa: E402
    CHECKPOINT_FILE,
    POOL_DETAIL,
    REPO,
    SYMBOL,
    TIMEFRAME,
    WORK_ROOT,
    acquire_lock,
    collector_status,
    load_checkpoint,
    pending_closes,
    pending_closes_after,
    publish_lag_seconds,
    read_state,
    save_checkpoint,
    strict_5m_bucket_complete,
    write_state,
)
from run_doge_p4_hist import (  # noqa: E402
    _client,
    last_closed_5m_decision_close,
    line_inventory,
    run_batch,
)

_stop = False


def _on_stop(signum, frame) -> None:
    global _stop
    _stop = True


def _setup_logging() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = WORK_ROOT / "publisher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )


def preflight_live() -> dict:
    pd = resolve_historical_pd_config(POOL_DETAIL, timeframe=TIMEFRAME)
    assert pd.pipeline_line_id == DOGE_PROTECTED_5M_PD1_LINE
    cfg, client = _client()
    db = cfg.database
    inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
    last_1m = client.query(
        f"""
        SELECT max(open_time) mx FROM {db}.candles_1m FINAL
        WHERE symbol={{s:String}} AND is_closed=1
        """,
        parameters={"s": SYMBOL},
    ).first_item["mx"]
    last_5m = last_closed_5m_decision_close(client, db, SYMBOL)
    reader = PublishedActiveReader()
    pub_max = parse_utc(inv["max_decision_close"]) if inv.get("max_decision_close") else None
    if pub_max is None:
        pub_max = reader.max_published_decision_close(
            pd.pipeline_line_id, config_hash=pd.config_hash
        )
    todo = pending_closes_after(pub_max, last_5m) if pub_max else []
    lag = publish_lag_seconds(pub_max, last_5m)
    lock_held = CHECKPOINT_FILE.parent.joinpath("publisher.lock").is_file()
    other_pid = None
    pid_file = WORK_ROOT / "publisher.pid"
    if pid_file.is_file():
        try:
            other_pid = int(pid_file.read_text().strip())
        except ValueError:
            other_pid = None
    return {
        "symbol": SYMBOL,
        "line": pd.to_api_dict(),
        "inventory": inv,
        "candles_1m_max": str(last_1m),
        "last_complete_5m_close": last_5m.isoformat(),
        "pending_closes": len(todo),
        "first_pending": todo[0].isoformat() if todo else None,
        "last_pending": todo[-1].isoformat() if todo else None,
        "publish_lag_sec": round(lag, 1) if lag is not None else None,
        "collector": collector_status(),
        "publisher_pid_file": other_pid,
        "lock_file_exists": lock_held,
    }


def cmd_status() -> int:
    pf = preflight_live()
    st = read_state()
    ck = load_checkpoint()
    out = {"preflight": pf, "worker_state": st, "checkpoint": ck}
    print(json.dumps(out, indent=2))
    return 0


def _run_catch_up(
    *,
    allow_doge: bool,
    batch_size: int,
    max_batches: int,
    dry_run: bool,
) -> dict:
    pd = resolve_historical_pd_config(POOL_DETAIL, timeframe=TIMEFRAME)
    assert_safe_write_target(SYMBOL, pd.pipeline_line_id, allow_doge_production=allow_doge)
    cfg, client = _client()
    db = cfg.database
    reader = PublishedActiveReader()
    test_run_id = str(uuid.uuid4())
    batches: list[dict] = []
    total_published = 0

    for bi in range(max_batches):
        if _stop:
            break
        last_5m = last_closed_5m_decision_close(client, db, SYMBOL)
        published = reader.list_published_closes(pd.pipeline_line_id, config_hash=pd.config_hash)
        todo = pending_closes(published, last_5m)
        if not todo:
            break
        chunk = todo[:batch_size]
        if dry_run:
            rep = {
                "dry_run": True,
                "batch_index": bi,
                "would_publish": len(chunk),
                "range": [chunk[0].isoformat(), chunk[-1].isoformat()],
            }
            batches.append(rep)
            break
        rep = run_batch(chunk, allow_doge=allow_doge, dry_run=False)
        rep["batch_index"] = bi
        rep["status"] = (
            "BATCH_OK"
            if not rep.get("errors") and not rep.get("stopped_early") and not rep.get("missing_in_batch")
            else "BATCH_FAIL"
        )
        batches.append(rep)
        total_published += int(rep.get("closes_processed") or 0)
        inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
        save_checkpoint(
            {
                "test_run_id": test_run_id,
                "last_batch": bi,
                "last_inventory": inv,
                "last_batch_status": rep["status"],
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_state(
            {
                "mode": "catch_up",
                "status": "RUNNING" if rep["status"] == "BATCH_OK" else "FAILED",
                "preflight": preflight_live(),
                "last_batch": rep,
                "batches_completed": len(batches),
            }
        )
        logging.info(
            "batch %s processed=%s remaining≈%s",
            bi,
            rep.get("closes_processed"),
            len(todo) - len(chunk),
        )
        if rep["status"] != "BATCH_OK":
            break

    last_5m = last_closed_5m_decision_close(client, db, SYMBOL)
    published = reader.list_published_closes(pd.pipeline_line_id, config_hash=pd.config_hash)
    remaining = pending_closes(published, last_5m)
    inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
    return {
        "batches": batches,
        "closes_published_this_run": total_published,
        "remaining_pending": len(remaining),
        "final_inventory": inv,
        "caught_up": len(remaining) == 0,
    }


def _run_live_loop(
    *,
    allow_doge: bool,
    poll_sec: float,
    once: bool,
    dry_run: bool,
    batch_size: int,
) -> dict:
    pd = resolve_historical_pd_config(POOL_DETAIL, timeframe=TIMEFRAME)
    assert_safe_write_target(SYMBOL, pd.pipeline_line_id, allow_doge_production=allow_doge)
    cfg, client = _client()
    db = cfg.database
    reader = PublishedActiveReader()
    test_run_id = str(uuid.uuid4())
    published_closes: list[str] = []
    errors: list[str] = []

    while not _stop:
        last_5m = last_closed_5m_decision_close(client, db, SYMBOL)
        inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
        pub_max = parse_utc(inv["max_decision_close"]) if inv.get("max_decision_close") else None
        if pub_max is None:
            pub_max = reader.max_published_decision_close(
                pd.pipeline_line_id, config_hash=pd.config_hash
            )
        todo = pending_closes_after(pub_max, last_5m) if pub_max else []
        if not todo:
            write_state(
                {
                    "mode": "live",
                    "status": "WAITING",
                    "preflight": preflight_live(),
                    "poll_sec": poll_sec,
                }
            )
            if once:
                return {
                    "status": "WAITING",
                    "published_this_run": published_closes,
                    "errors": errors,
                }
            time.sleep(poll_sec)
            continue

        if len(todo) > 1 and not dry_run:
            chunk = todo[: max(1, batch_size)]
            t_batch = time.perf_counter()
            rep = run_batch(chunk, allow_doge=allow_doge, dry_run=False)
            elapsed = time.perf_counter() - t_batch
            ok = (
                "BATCH_OK"
                if not rep.get("errors")
                and not rep.get("stopped_early")
                and not rep.get("missing_in_batch")
                else "BATCH_FAIL"
            )
            processed_n = int(rep.get("closes_processed") or 0)
            for dc in chunk[:processed_n]:
                published_closes.append(dc.isoformat())
            logging.info(
                "live backlog batch n=%s processed=%s elapsed_sec=%.1f status=%s",
                len(chunk),
                processed_n,
                elapsed,
                ok,
            )
            inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
            last_dc = chunk[-1]
            save_checkpoint(
                {
                    "test_run_id": test_run_id,
                    "last_published": last_dc.isoformat(),
                    "inventory": inv,
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                    "last_batch_elapsed_sec": round(elapsed, 2),
                }
            )
            write_state(
                {
                    "mode": "live",
                    "status": "RUNNING" if ok == "BATCH_OK" else "FAILED",
                    "last_published": last_dc.isoformat(),
                    "preflight": preflight_live(),
                    "published_this_session": len(published_closes),
                    "last_batch_closes": processed_n,
                    "last_batch_elapsed_sec": round(elapsed, 2),
                    "last_batch_per_close_sec": round(elapsed / max(1, processed_n), 3),
                }
            )
            if rep.get("errors"):
                errors.extend(rep["errors"])
            if ok != "BATCH_OK":
                if once:
                    break
                time.sleep(poll_sec)
                continue
            if once:
                break
            if len(pending_closes_after(last_dc, last_5m)) > 0:
                time.sleep(0.5)
            else:
                time.sleep(max(1.0, poll_sec))
            continue

        dc = todo[0]
        if not strict_5m_bucket_complete(client, db, dc):
            errors.append(f"{dc.isoformat()}: incomplete 1m bucket")
            time.sleep(poll_sec)
            if once:
                break
            continue

        if dry_run:
            published_closes.append(dc.isoformat())
            if once:
                break
            time.sleep(poll_sec)
            continue

        try:
            t_close = time.perf_counter()
            row = process_rolling_close(
                symbol=SYMBOL,
                timeframe=TIMEFRAME,
                decision_close=dc,
                pool_detail=POOL_DETAIL,
                pipeline_line_id=pd.pipeline_line_id,
                test_run_id=test_run_id,
            )
            close_elapsed = time.perf_counter() - t_close
            timing = row.get("timing")
            published_closes.append(dc.isoformat())
            logging.info(
                "live publish %s fp=%s elapsed_sec=%.2f",
                dc.isoformat(),
                row.get("active_state_fingerprint"),
                close_elapsed,
            )
        except Exception as exc:
            errors.append(f"{dc.isoformat()}: {exc!r}")
            logging.exception("live publish failed %s", dc)
            if once:
                break
            time.sleep(poll_sec)
            continue

        inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
        save_checkpoint(
            {
                "test_run_id": test_run_id,
                "last_published": dc.isoformat(),
                "inventory": inv,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_state(
            {
                "mode": "live",
                "status": "RUNNING",
                "last_published": dc.isoformat(),
                "preflight": preflight_live(),
                "published_this_session": len(published_closes),
                "last_close_elapsed_sec": round(close_elapsed, 3),
                "last_close_timing": (
                    {
                        "load_seconds": round(timing.load_seconds, 3),
                        "engine_seconds": round(timing.engine_seconds, 3),
                        "write_seconds": round(timing.write_seconds, 3),
                        "total_seconds": round(timing.total_seconds, 3),
                    }
                    if timing is not None
                    else None
                ),
            }
        )
        if once:
            break
        if len(todo) > 1:
            time.sleep(0.5)
        else:
            time.sleep(max(1.0, poll_sec))

    inv = line_inventory(client, db, pd.pipeline_line_id, pd.config_hash)
    return {
        "status": "STOPPED" if _stop else ("FAILED" if errors else "OK"),
        "published_this_run": published_closes,
        "errors": errors,
        "final_inventory": inv,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DOGEUSDT 5m PD1 live publisher")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--catch-up", action="store_true", help="Batch catch-up from last publish to latest strict 5m")
    ap.add_argument("--live", action="store_true", help="Live tick (one close at a time)")
    ap.add_argument("--once", action="store_true", help="With --live: single iteration then exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-doge-production", action="store_true")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--max-batches", type=int, default=0, help="0 = until caught up")
    ap.add_argument("--poll-sec", type=float, default=20.0)
    ap.add_argument("--out", type=Path, default=REPO / "results/doge_live_p4_live_run_v1.json")
    args = ap.parse_args(argv)

    if args.preflight:
        rep = preflight_live()
        print(json.dumps(rep, indent=2))
        args.out.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        return 0

    if args.status:
        return cmd_status()

    if not args.catch_up and not args.live:
        ap.print_help()
        return 2

    if not args.dry_run and not args.allow_doge_production:
        print("Refusing writes without --allow-doge-production", file=sys.stderr)
        return 2

    _setup_logging()
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    pf = preflight_live()
    summary: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "preflight_start": pf}

    if args.catch_up or args.live:
        acquire_lock()

    cu: dict = {}
    if args.catch_up:
        max_b = args.max_batches if args.max_batches > 0 else 10_000
        cu = _run_catch_up(
            allow_doge=args.allow_doge_production,
            batch_size=args.batch_size,
            max_batches=max_b,
            dry_run=args.dry_run,
        )
        summary["catch_up"] = cu

    if args.live and not _stop and (not args.catch_up or cu.get("caught_up") or args.once):
        if args.catch_up and not cu.get("caught_up") and not args.once:
            summary["live_skipped"] = "catch_up incomplete"
        else:
            lv = _run_live_loop(
                allow_doge=args.allow_doge_production,
                poll_sec=args.poll_sec,
                once=args.once,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
            )
            summary["live"] = lv

    pd = resolve_historical_pd_config(POOL_DETAIL, timeframe=TIMEFRAME)
    cfg, client = _client()
    inv = line_inventory(client, cfg.database, pd.pipeline_line_id, pd.config_hash)
    pub_max = parse_utc(inv["max_decision_close"])
    oracle = extended_oracle_check(
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        pool_detail=POOL_DETAIL,
        pipeline_line_id=pd.pipeline_line_id,
        oracle_closes=[pub_max],
    )
    summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
    summary["final_inventory"] = inv
    summary["tail_oracle"] = oracle
    summary["preflight_end"] = preflight_live()
    args.out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    write_state({**read_state(), "status": "IDLE", "last_run_summary_path": str(args.out)})

    live_err = summary.get("live", {}).get("errors") or []
    batch_fail = any(b.get("status") != "BATCH_OK" for b in summary.get("catch_up", {}).get("batches") or [])
    if batch_fail or live_err:
        return 5
    if args.catch_up and args.max_batches > 0:
        remaining = summary.get("catch_up", {}).get("remaining_pending", 0)
        if remaining:
            return 0
    if args.catch_up and not summary.get("catch_up", {}).get("caught_up", True):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
