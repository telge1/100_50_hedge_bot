#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean stale symbol reservations for long bots.")
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("--bot-group-dir", required=True, type=Path)
    parser.add_argument("--log-prefix", default="")
    return parser.parse_args()


def _log(prefix: str, message: str) -> None:
    if prefix:
        print(f"[cleanup:{prefix}] {message}", flush=True)
    else:
        print(f"[cleanup] {message}", flush=True)


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


def _lock_file(fd) -> None:
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_file(fd) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_pid(pid_file: Path) -> tuple[int | None, str]:
    if not pid_file.exists():
        return None, "missing_pid_file"
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except Exception:
        return None, "pid_read_error"
    if not raw:
        return None, "empty_pid"
    try:
        return int(raw), "pid_valid"
    except ValueError:
        return None, "pid_invalid"


def _is_process_running(pid: int) -> tuple[bool, str]:
    try:
        os.kill(pid, 0)
        return True, "process_alive"
    except ProcessLookupError:
        return False, "process_missing"
    except PermissionError:
        return False, "permission_denied"


def _read_cmdline(pid: int) -> tuple[str | None, str]:
    cmd_path = Path("/proc") / str(pid) / "cmdline"
    try:
        data = cmd_path.read_bytes()
    except Exception:
        return None, "cmdline_missing"
    if not data:
        return None, "cmdline_empty"
    return data.replace(b"\x00", b" ").decode("utf-8", "ignore").strip(), "cmdline_loaded"


def _cmdline_matches(cmdline: str, bot_name: str, config_path: Path, state_path: Path) -> bool:
    if "fixed_cycle_hedge_bot.runner" not in cmdline:
        return False
    if f"--bot-name {bot_name}" not in cmdline:
        return False
    for token in (str(config_path), str(state_path), bot_name):
        if token and token in cmdline:
            return True
    return True


def _get_pid_sources(bot_dir: Path) -> list[Path]:
    return [
        bot_dir / "run" / "bot.pid",
        bot_dir / "pids" / "fixed_cycle_bot.pid",
    ]


def _inspect_bot(bot_name: str, bot_group_dir: Path) -> tuple[bool, str]:
    bot_dir = bot_group_dir / bot_name
    if not bot_dir.is_dir():
        return False, "bot_dir_missing"
    config_path = bot_dir / "config" / "fixed_cycle_config.json"
    state_path = bot_dir / "state" / "fixed_cycle_state.json"
    pid_sources = _get_pid_sources(bot_dir)
    reasons: list[str] = []
    pid_seen = False
    for pid_file in pid_sources:
        if not pid_file.exists():
            reasons.append(f"no_pid_file:{pid_file}")
            continue
        pid_seen = True
        pid, pid_status = _read_pid(pid_file)
        if pid is None:
            reasons.append(f"invalid_pid_file:{pid_status}:{pid_file}")
            continue
        running, run_status = _is_process_running(pid)
        if not running:
            reasons.append(f"process_not_alive:{run_status}:{pid_file}")
            continue
        cmdline, cmd_status = _read_cmdline(pid)
        if not cmdline:
            reasons.append(f"cmdline_not_reader:{cmd_status}:{pid_file}")
            continue
        if not _cmdline_matches(cmdline, bot_name, config_path, state_path):
            reasons.append(f"cmdline_bot_mismatch:{cmd_status}:{pid_file}")
            continue
        return True, "alive"
    if not pid_seen:
        reasons.append("no_pid_sources")
    return False, ";".join(reasons or ["no_pid_file"])


def main() -> int:
    args = parse_args()
    state_path = args.state_file
    lock_path = args.lock_file
    bot_group_dir = args.bot_group_dir
    prefix = args.log_prefix.strip()

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(lock_path, "a+", encoding="utf-8") as lock_fd:
            _lock_file(lock_fd)
            try:
                state = _load_state(state_path)
                changed = False
                for bot_name, meta in list(state.items()):
                    if not isinstance(meta, dict):
                        continue
                    if meta.get("status") != "reserved":
                        continue
                    symbol = meta.get("symbol")
                    pid_value = meta.get("pid")
                    if not pid_value:
                        state.pop(bot_name, None)
                        changed = True
                        _log(
                            prefix,
                            f"stale_symbol_reservation_removed bot_name={bot_name} symbol={symbol} reason=missing_pid",
                        )
                        continue
                    alive, reason = _inspect_bot(bot_name, bot_group_dir)
                    if alive:
                        _log(prefix, f"active_symbol_reservation_kept bot_name={bot_name} symbol={symbol}")
                        continue
                    state.pop(bot_name)
                    changed = True
                    _log(
                        prefix,
                        f"stale_symbol_reservation_removed bot_name={bot_name} symbol={symbol} reason={reason}",
                    )
                if changed:
                    _write_state(state_path, state)
            finally:
                _unlock_file(lock_fd)
    except Exception as exc:
        print(f"[cleanup] failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
