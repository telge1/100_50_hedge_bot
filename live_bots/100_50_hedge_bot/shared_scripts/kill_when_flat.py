#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

LOGGER = logging.getLogger(__name__)
FLAT_TOLERANCE = 1e-9


def _append_log(log_path: Path, entry: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(log_path, "a", encoding="utf-8") as out:
        out.write(f"{timestamp} {entry}\n")


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_open_qty(state_path: Path) -> tuple[float, float] | None:
    payload = _read_json(state_path)
    if not payload:
        return None
    strategy_state = payload.get("strategy_state")
    if not isinstance(strategy_state, dict):
        return None
    long_qty = strategy_state.get("open_long_qty")
    short_qty = strategy_state.get("open_short_qty")
    if long_qty is None or short_qty is None:
        return None
    try:
        return float(long_qty), float(short_qty)
    except (TypeError, ValueError):
        return None


def _flat_values(qtys: tuple[float, float]) -> bool:
    return abs(qtys[0]) <= FLAT_TOLERANCE and abs(qtys[1]) <= FLAT_TOLERANCE


def _fetch_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        value = pid_path.read_text(encoding="utf-8").strip()
        return int(value) if value.isdigit() else None
    except Exception:
        return None


def _validate_cmdline(pid: int, bot_name: str) -> bool:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():
        return False
    try:
        raw = cmdline_path.read_bytes().decode("utf-8").replace("\x00", " ")
    except Exception:
        return False
    return (
        "fixed_cycle_hedge_bot.runner" in raw
        and f"--bot-name {bot_name}" in raw
    )


def _write_status_file(status_path: Path, bot_name: str, reason: str | None = None) -> None:
    data = _read_json(status_path) or {}
    updated = {
        "bot_name": bot_name,
        "status": "stopped",
        "start_requested": False,
        "pid": None,
        "symbol": data.get("symbol", ""),
        "reason": reason or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_completion(
    completion_path: Path,
    reason: str,
    pid: int,
    long_qty: float,
    short_qty: float,
) -> None:
    payload = {
        "completed": True,
        "reason": reason,
        "killed_at": datetime.now(timezone.utc).isoformat(),
        "flat_detection_source": "state_strategy_open_qty",
        "pid": pid,
        "open_long_qty": long_qty,
        "open_short_qty": short_qty,
    }
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _kill_process(pid: int, log_path: Path, bot_name: str) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        _append_log(log_path, f"{bot_name} kill_when_flat_sigterm_sent pid={pid}")
    except ProcessLookupError:
        return
    except PermissionError:
        _append_log(log_path, f"{bot_name} kill_when_flat_sigterm_permission_denied pid={pid}")
        return

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.05)

    if Path(f"/proc/{pid}").exists():
        try:
            os.kill(pid, signal.SIGKILL)
            _append_log(log_path, f"{bot_name} kill_when_flat_sigkill_sent pid={pid}")
        except ProcessLookupError:
            return
        except PermissionError:
            _append_log(log_path, f"{bot_name} kill_when_flat_sigkill_permission_denied pid={pid}")
            return


def _handle_bot(
    bot_name: str,
    project_root: Path,
    reason: str,
    interval: float,
) -> None:
    bot_dir = project_root / "live_bots" / "100_50_hedge_bot" / bot_name
    if not bot_dir.exists():
        LOGGER.warning("Bot dir missing %s", bot_dir)
        return
    run_dir = bot_dir / "run"
    lock_path = run_dir / "kill_when_flat.lock"
    log_path = bot_dir / "logs" / "kill_when_flat.log"
    completion_path = run_dir / "kill_when_flat_completed.json"
    state_path = bot_dir / "state" / "fixed_cycle_state.json"
    status_path = run_dir / "status.json"
    pid_path = run_dir / "bot.pid"

    if completion_path.exists():
        _append_log(log_path, f"{bot_name} kill_when_flat_completed already present - skipping")
        return

    try:
        lock_file = lock_path.open("x")
        lock_file.write(datetime.now(timezone.utc).isoformat())
        lock_file.flush()
    except FileExistsError:
        _append_log(log_path, f"{bot_name} kill_when_flat_lock_already_acquired")
        return

    try:
        _append_log(log_path, f"{bot_name} kill_when_flat_started reason={reason}")
        first = _load_open_qty(state_path)
        if not first or not _flat_values(first):
            _append_log(
                log_path,
                f"{bot_name} kill_when_flat_waiting_not_flat first_read={first}",
            )
            return

        time.sleep(0.05)
        second = _load_open_qty(state_path)
        if not second or not _flat_values(second):
            _append_log(
                log_path,
                f"{bot_name} kill_when_flat_waiting_not_flat second_read={second}",
            )
            return

        _append_log(
            log_path,
            (
                f"{bot_name} kill_when_flat_confirmed long={second[0]} "
                f"short={second[1]}"
            ),
        )

        pid = _fetch_pid(pid_path)
        if pid is None:
            _append_log(log_path, f"{bot_name} kill_when_flat_state_unknown missing_pid")
            return

        if not _validate_cmdline(pid, bot_name):
            _append_log(log_path, f"{bot_name} kill_when_flat_state_unknown cmdline_mismatch pid={pid}")
            return

        _append_log(log_path, f"{bot_name} kill_when_flat_pid_validated pid={pid}")
        _kill_process(pid, log_path, bot_name)
        _write_status_file(status_path, bot_name, reason=reason)
        _write_completion(completion_path, reason, pid, second[0], second[1])
        _append_log(log_path, f"{bot_name} kill_when_flat_completed pid={pid}")
    finally:
        try:
            lock_path.unlink()
        except Exception:
            pass


def main(args: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kill flat bots without restart")
    parser.add_argument("--bots", nargs="+", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--reason", type=str, default="kill_when_flat")
    parser.add_argument("--loop", action="store_true")
    namespace = parser.parse_args(args=args)
    project_root = Path(__file__).resolve().parents[2]

    while True:
        for bot in namespace.bots:
            _handle_bot(bot, project_root, namespace.reason, namespace.interval)
        if not namespace.loop:
            break
        time.sleep(namespace.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
