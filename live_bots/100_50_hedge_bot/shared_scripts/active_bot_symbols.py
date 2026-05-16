#!/usr/bin/env python3
import argparse
import json
import subprocess
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_POLL_SECONDS = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage unique symbol reservations for long bots.")
    parser.add_argument("--bot-name", required=True)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("--bot-group-dir", type=Path)
    parser.add_argument("--cleanup-script", type=Path)
    parser.add_argument("command", choices=["reserve", "release"])
    parser.add_argument("--best-coin-file", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("--source", default="start_long_bot")
    parser.add_argument("--pid", type=int, default=None)
    return parser.parse_args()


def _validate_bot_name(name: str) -> bool:
    return bool(name and name.startswith("long_bot_") and name[9:].isdigit())


def _read_best_coin(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    symbol = payload.get("symbol")
    if not symbol or not isinstance(symbol, str):
        return None
    symbol = symbol.strip().upper()
    if not symbol.endswith("USDT"):
        return None
    return symbol


def _load_state(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_state(path: Path, data: dict[str, dict[str, object]]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp.{int(time.time() * 1000)}")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _lock_file(fd):
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_file(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)


def _run_cleanup(args: argparse.Namespace, state_path: Path, lock_path: Path, bot_name: str) -> None:
    script_path = args.cleanup_script
    bot_group_dir = args.bot_group_dir
    if not script_path or not bot_group_dir:
        return
    if not script_path.exists():
        print(f"[{bot_name}] cleanup script missing: {script_path}", flush=True)
        return
    cmd = [
        sys.executable,
        str(script_path),
        "--state-file",
        str(state_path),
        "--lock-file",
        str(lock_path),
        "--bot-group-dir",
        str(bot_group_dir),
        "--log-prefix",
        bot_name,
    ]
    try:
        subprocess.run(cmd, timeout=90, check=False)
    except Exception as exc:
        print(f"[{bot_name}] cleanup script execution failed: {exc}", flush=True)


def reserve_symbol(args: argparse.Namespace) -> int:
    bot_name = args.bot_name
    if not _validate_bot_name(bot_name):
        print(f"[{bot_name}] invalid bot name", file=sys.stderr)
        return 2

    state_path = args.state_file
    lock_path = args.lock_file
    best_coin_path = args.best_coin_file
    poll_seconds = max(args.poll_seconds, 0.1)
    timeout_seconds = max(args.timeout_seconds, 0.0)
    deadline = None if timeout_seconds == 0 else time.monotonic() + timeout_seconds

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        while True:
            _run_cleanup(args, state_path, lock_path, bot_name)
            if args.cleanup_script:
                print(f"[{bot_name}] reservation_cleanup_before_reserve_done", flush=True)

            if deadline and time.monotonic() >= deadline:
                print(
                    f"[{bot_name}] timed out after waiting {timeout_seconds:.0f}s for a free symbol",
                    file=sys.stderr,
                )
                return 3

            symbol = _read_best_coin(best_coin_path)
            if not symbol:
                print(f"[{bot_name}] best_coin.json missing/invalid; waiting...", flush=True)
                time.sleep(poll_seconds)
                continue

            _lock_file(lock_fd)
            try:
                state = _load_state(state_path)
                occupying_bot = None
                for other_bot, meta in state.items():
                    if (
                        other_bot != bot_name
                        and isinstance(meta, dict)
                        and meta.get("symbol") == symbol
                        and meta.get("status") == "reserved"
                    ):
                        occupying_bot = other_bot
                        break

                if occupying_bot:
                    occupying_symbol = state.get(occupying_bot, {}).get("symbol", "")
                    if occupying_symbol and occupying_symbol != symbol:
                        state.pop(occupying_bot, None)
                        _write_state(state_path, state)
                        print(
                            f"[{bot_name}] cleared stale reservation of {occupying_bot} ({occupying_symbol})",
                            flush=True,
                        )
                        occupying_bot = None
                    else:
                        occupant_meta = state.get(occupying_bot, {})
                        occupant_pid = occupant_meta.get("pid")
                        occupant_source = occupant_meta.get("source")
                        print(
                            f"[{bot_name}] reservation_blocked_by_live_bot occupant={occupying_bot} symbol={symbol} pid={occupant_pid} source={occupant_source} state={state_path}",
                            flush=True,
                        )
                if not occupying_bot:
                    entry = {
                        "symbol": symbol,
                        "status": "reserved",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "source": args.source,
                        "pid": args.pid,
                    }
                    state[bot_name] = entry
                    _write_state(state_path, state)
                    print(f"[{bot_name}] reserved {symbol}", flush=True)
                    return 0
            finally:
                _unlock_file(lock_fd)

            time.sleep(poll_seconds)


def release_symbol(args: argparse.Namespace) -> int:
    bot_name = args.bot_name
    if not _validate_bot_name(bot_name):
        print(f"[{bot_name}] invalid bot name", file=sys.stderr)
        return 2

    state_path = args.state_file
    lock_path = args.lock_file

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fd:
        _lock_file(lock_fd)
        try:
            state = _load_state(state_path)
            entry = state.pop(bot_name, None)
            if not entry:
                print(f"[{bot_name}] no reservation to release", flush=True)
                return 0
            _write_state(state_path, state)
            symbol = entry.get("symbol")
            print(f"[{bot_name}] released {symbol}", flush=True)
            return 0
        finally:
            _unlock_file(lock_fd)


def main() -> int:
    args = parse_args()
    if args.command == "reserve":
        if not args.best_coin_file:
            print("best_coin_file is required for reserve", file=sys.stderr)
            return 4
        return reserve_symbol(args)
    if args.command == "release":
        return release_symbol(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
