#!/usr/bin/env python3
"""Phase 1 DRY-RUN sync service — polls spread_recovery drawings.json."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.manual_long_box_live_sync_v1.config import (  # noqa: E402
    DEFAULT_ACTIVATIONS_PATH,
    DEFAULT_DRAWINGS_PATH,
    DEFAULT_EVENT_LOG_PATH,
    DEFAULT_STATE_PATH,
    DRY_RUN_ONLY,
)
from research.manual_long_box_live_sync_v1.service import SyncService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drawings", type=Path, default=DEFAULT_DRAWINGS_PATH)
    ap.add_argument("--activations", type=Path, default=DEFAULT_ACTIVATIONS_PATH)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    ap.add_argument("--events", type=Path, default=DEFAULT_EVENT_LOG_PATH)
    ap.add_argument("--once", action="store_true", help="Single sync pass (for tests)")
    ap.add_argument("--poll-sec", type=float, default=0.5)
    args = ap.parse_args()

    if not DRY_RUN_ONLY:
        print(json.dumps({"ok": False, "error": "live trading disabled in phase 1"}))
        return 2

    svc = SyncService(
        drawings_path=args.drawings,
        activations_path=args.activations,
        state_path=args.state,
        event_log_path=args.events,
        poll_sec=args.poll_sec,
    )

    if args.once:
        out = svc.run_once()
        print(json.dumps({"ok": True, "dry_run": True, "results": out}, indent=2))
        return 0

    def _stop(*_):
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    svc.start(background=True)
    print(json.dumps({"ok": True, "dry_run": True, "status": "running", "drawings": str(args.drawings)}))
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
