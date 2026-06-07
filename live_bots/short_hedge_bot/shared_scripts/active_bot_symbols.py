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
BLACKLIST_FILE_NAME = "blacklisted_symbols.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage unique symbol reservations for hedge bots.")
    parser.add_argument("--bot-name", required=True)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("--bot-group-dir", type=Path)
    parser.add_argument("--cleanup-script", type=Path)
    parser.add_argument("command", choices=["reserve", "release"])
    parser.add_argument("--best-coin-file", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("--source", default="start_short_bot")
    parser.add_argument("--pid", type=int, default=None)
    return parser.parse_args()


def _blacklist_file_path(state_dir: Path) -> Path:
    path = state_dir / BLACKLIST_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_blacklisted_symbols(state_dir: Path) -> dict[str, dict[str, object]]:
    path = _blacklist_file_path(state_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _validate_bot_name(name: str) -> bool:
    if not name or not isinstance(name, str):
        return False

    parts = name.split("_")
    if len(parts) != 3:
        return False

    side, label, number = parts
    return side in {"long", "short"} and label == "bot" and number.isdigit()


def _normalize_symbol(symbol: str | None) -> str | None:
    if not symbol or not isinstance(symbol, str):
        return None
    symbol = symbol.strip().upper()
    if not symbol.endswith("USDT"):
        return None
    return symbol


def _read_best_coin_candidates(path: Path) -> tuple[list[str], str, int]:
    if not path.exists():
        return [], "", 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return [], "", 0
    reason = payload.get("reason") or ""
    top_symbol = _normalize_symbol(payload.get("symbol"))
    candidates = payload.get("candidates") or []
    symbols: list[str] = []
    seen: set[str] = set()

    def _add(symbol: str | None) -> None:
        normalized = _normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            symbols.append(normalized)

    if reason == "no_good_candidates":
        return [], reason, len(symbols)
    _add(top_symbol)
    if isinstance(candidates, list):
        for entry in candidates:
            if isinstance(entry, dict):
                _add(entry.get("symbol"))
    return symbols, reason, len(symbols)


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

            blacklist = _load_blacklisted_symbols(state_path.parent)
            candidate_symbols, reason, candidate_count = _read_best_coin_candidates(best_coin_path)
            if not candidate_symbols:
                if reason:
                    msg = f"{reason}; waiting..."
                else:
                    msg = "best_coin.json missing/invalid; waiting..."
                print(f"[{bot_name}] {msg}", flush=True)
                time.sleep(poll_seconds)
                continue
            symbol_info = f"candidate_count={candidate_count} candidates={candidate_symbols}"
            print(f"[{bot_name}] evaluating candidates {symbol_info}", flush=True)

            _lock_file(lock_fd)
            try:
                state = _load_state(state_path)
                reserved_by_symbol: dict[str, str] = {}
                for other_bot, meta in state.items():
                    if not isinstance(meta, dict):
                        continue
                    status = meta.get("status")
                    sym = meta.get("symbol")
                    if status == "reserved" and sym:
                        reserved_by_symbol[sym] = other_bot
                current_entry = state.get(bot_name)
                if current_entry and current_entry.get("status") == "reserved":
                    current_symbol = current_entry.get("symbol")
                    if current_symbol in candidate_symbols:
                        print(
                            f"[{bot_name}] already reserved {current_symbol}; keeping reservation",
                            flush=True,
                        )
                        return 0
                checked_candidates = 0
                occupying_bot = None
                preferred_index = min(int(bot_name.split("_")[-1]) - 1, len(candidate_symbols) - 1)
                ordered_candidates = candidate_symbols[preferred_index:] + candidate_symbols[:preferred_index]
                for symbol in ordered_candidates:
                    if symbol in blacklist:
                        entry = blacklist.get(symbol, {})
                        detail = entry.get("reason") or "blacklisted"
                        print(
                            f"[{bot_name}] dynamic_symbol_blacklisted_symbol_skipped symbol={symbol} reason={detail}",
                            flush=True,
                        )
                        continue
                    checked_candidates += 1
                    occupant = reserved_by_symbol.get(symbol)
                    if occupant and occupant != bot_name:
                        occ_meta = state.get(occupant, {})
                        occ_pid = occ_meta.get("pid")
                        occ_source = occ_meta.get("source")
                        print(
                            f"[{bot_name}] candidate {symbol} blocked by {occupant} pid={occ_pid} source={occ_source}",
                            flush=True,
                        )
                        continue
                    entry = {
                        "symbol": symbol,
                        "status": "reserved",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "source": args.source,
                        "pid": args.pid,
                    }
                    state[bot_name] = entry
                    _write_state(state_path, state)
                    print(
                        f"[{bot_name}] reserved {symbol} (checked={checked_candidates} candidate_count={candidate_count})",
                        flush=True,
                    )
                    return 0
                blocked = ", ".join(candidate_symbols[:checked_candidates])
                print(
                    f"[{bot_name}] no free candidate; candidates_checked={checked_candidates} blocked={blocked}",
                    flush=True,
                )
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
