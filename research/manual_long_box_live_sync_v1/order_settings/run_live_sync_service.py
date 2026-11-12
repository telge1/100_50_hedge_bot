#!/usr/bin/env python3
"""Live Bybit sync — polls spread research_charts drawings.json and places orders when armed."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.manual_long_box_live_sync_v1.config import (  # noqa: E402
    DEFAULT_ACTIVATIONS_PATH,
    DEFAULT_DRAWINGS_PATH,
    DEFAULT_EVENT_LOG_PATH,
    DEFAULT_STATE_PATH,
)
from research.manual_long_box_live_sync_v1.order_settings.config import (  # noqa: E402
    assert_live_ready,
    load_dotenv_chain,
    load_settings,
)
from research.manual_long_box_live_sync_v1.order_settings.order_sync import (  # noqa: E402
    build_sync_engine,
    startup_reconcile_notes,
)
from research.manual_long_box_live_sync_v1.service import SyncService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual Long Box → Bybit live sync service")
    ap.add_argument("--drawings", type=Path, default=DEFAULT_DRAWINGS_PATH)
    ap.add_argument("--activations", type=Path, default=DEFAULT_ACTIVATIONS_PATH)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    ap.add_argument("--events", type=Path, default=DEFAULT_EVENT_LOG_PATH)
    ap.add_argument("--once", action="store_true", help="Single sync pass")
    ap.add_argument("--poll-sec", type=float, default=0.5)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Force DRY-RUN even if MANUAL_LONG_BOX_LIVE_ARMED=1",
    )
    args = ap.parse_args()

    load_dotenv_chain()
    settings = load_settings()
    force_dry = args.dry_run or not settings.writes_allowed
    if not force_dry:
        assert_live_ready(settings)

    eng, mode = build_sync_engine(
        args.state,
        args.events,
        force_dry_run=force_dry,
        settings=settings,
    )
    notes = startup_reconcile_notes(eng.state)

    svc = SyncService(
        drawings_path=args.drawings,
        activations_path=args.activations,
        state_path=args.state,
        event_log_path=args.events,
        poll_sec=args.poll_sec,
        engine=eng,
    )

    meta = {
        "ok": True,
        "mode": mode,
        "dry_run": eng.dry_run,
        "symbol": settings.symbol,
        "live_armed": settings.live_armed,
        "writes_allowed": settings.writes_allowed,
        "drawings": str(args.drawings),
        "activations": str(args.activations),
        "startup_reconcile_notes": notes,
    }

    if args.once:
        out = svc.run_once()
        meta["results"] = out
        print(json.dumps(meta, indent=2))
        return 0

    def _stop(*_):
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    svc.start(background=True)
    meta["status"] = "running"
    print(json.dumps(meta, indent=2))
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
