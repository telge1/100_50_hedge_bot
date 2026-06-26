#!/usr/bin/env python3
"""Resolve pair-state symbol adoption vs best_coin fallback for start_long_bot.sh."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def pid_alive(pid_path: Path) -> bool:
    try:
        pid_text = pid_path.read_text(encoding="utf-8").strip()
        pid = int(pid_text) if pid_text else None
    except Exception:
        pid = None
    if pid is None:
        return False
    return (Path("/proc") / str(pid)).exists()


def resolve_pair_symbol(
    pair_state_path: Path,
    long_pid_path: Path,
    short_pid_path: Path,
) -> dict[str, Any]:
    long_alive = pid_alive(long_pid_path)
    short_alive = pid_alive(short_pid_path)
    result: dict[str, Any] = {
        "pair_symbol": "",
        "stale_cleared": False,
        "old_symbol": None,
        "long_running": False,
        "short_running": False,
        "long_alive": long_alive,
        "short_alive": short_alive,
        "pair_active": False,
    }
    if not pair_state_path.exists():
        return result

    try:
        data = json.loads(pair_state_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        data = {}

    symbol = str(data.get("symbol") or "").strip().upper()
    long_running = bool(data.get("long_running"))
    short_running = bool(data.get("short_running"))
    result["long_running"] = long_running
    result["short_running"] = short_running

    if not symbol:
        return result

    pair_active = long_running or short_running or long_alive or short_alive
    result["pair_active"] = pair_active
    if pair_active:
        result["pair_symbol"] = symbol
        return result

    result["old_symbol"] = symbol
    result["stale_cleared"] = True
    return result


def archive_stale_pair_state(pair_state_path: Path) -> Path | None:
    if not pair_state_path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = pair_state_path.with_name(f"{pair_state_path.name}.stale.{timestamp}")
    pair_state_path.replace(archive_path)
    return archive_path


def format_stale_log(result: dict[str, Any]) -> str:
    return (
        "stale_pair_state_ignored_and_cleared "
        f"old_symbol={result.get('old_symbol')} "
        f"long_running={str(bool(result.get('long_running'))).lower()} "
        f"short_running={str(bool(result.get('short_running'))).lower()} "
        f"reason=both_bots_stopped"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 3:
        print(
            "usage: pair_symbol_resolve.py <pair_state.json> <long_pid_file> <short_pid_file>",
            file=sys.stderr,
        )
        return 2

    pair_state_path = Path(args[0])
    long_pid_path = Path(args[1])
    short_pid_path = Path(args[2])
    result = resolve_pair_symbol(pair_state_path, long_pid_path, short_pid_path)

    if result.get("stale_cleared"):
        archive_stale_pair_state(pair_state_path)
        print(format_stale_log(result), file=sys.stderr)

    print(str(result.get("pair_symbol") or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
