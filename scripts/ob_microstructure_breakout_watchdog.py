#!/usr/bin/env python3
"""Watchdog for the ob_microstructure_breakout_bot live collectors.

The script monitors the collector health files and restarts stale or dead
collector processes with the same launch arguments used for the current
multi-collector setup.

Typical usage:

  python scripts/ob_microstructure_breakout_watchdog.py --loop --interval 15
  python scripts/ob_microstructure_breakout_watchdog.py --once

The watchdog only uses the local health files, pid files, lock files and
socket files. It does not talk to ClickHouse directly.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ORDERBOOK_REPO = Path(
    os.environ.get("ORDERBOOK_REPO")
    or "/home/telgenbuescher/projects/orderbook_analyse"
)
ORDERBOOK_PYTHON = Path(
    os.environ.get("ORDERBOOK_PYTHON")
    or (ORDERBOOK_REPO / ".venv" / "bin" / "python")
)
ORDERBOOK_SRC = Path(
    os.environ.get("ORDERBOOK_SRC")
    or (ORDERBOOK_REPO / "src")
)


@dataclass(frozen=True)
class CollectorSpec:
    name: str
    symbols: tuple[str, ...]
    health_file: Path
    log_file: Path
    lock_file: Path
    pid_file: Path
    socket_file: Path

    @property
    def symbol_label(self) -> str:
        return ",".join(self.symbols)

    @property
    def symbol_key(self) -> str:
        return "_".join(s.lower() for s in self.symbols)

    def command(self) -> list[str]:
        return [
            str(ORDERBOOK_PYTHON),
            "-m",
            "orderbook_analyse.orderbook_v2_live",
            "--mode",
            "universe51",
            "--confirm-universe-51",
            "--symbols",
            self.symbol_label,
            "--health-file",
            str(self.health_file),
            "--log-level",
            "INFO",
        ]

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ORDERBOOK_SRC)
        env["OB_V3_LIVE_LOCK_PATH"] = str(self.lock_file)
        env["OB_V3_LIVE_PID_PATH"] = str(self.pid_file)
        env["OB_V3_ON_DEMAND_SOCKET_PATH"] = str(self.socket_file)
        return env


SPECIAL_COLLECTORS: tuple[CollectorSpec, ...] = (
    CollectorSpec(
        name="doge_ondo",
        symbols=("DOGEUSDT", "ONDOUSDT"),
        health_file=Path("/tmp/doge_ondo_ch_health.ndjson"),
        log_file=Path("/tmp/doge_ondo_ch.log"),
        lock_file=Path(
            "/home/telgenbuescher/projects/orderbook_analyse/logs/"
            "orderbook_v3_live_collector.lock"
        ),
        pid_file=Path(
            "/home/telgenbuescher/projects/orderbook_analyse/logs/"
            "orderbook_v3_live_collector.pid"
        ),
        socket_file=Path("/run/user/1000/orderbook_ob1000.sock"),
    ),
    CollectorSpec(
        name="render",
        symbols=("RENDERUSDT",),
        health_file=Path("/tmp/render_ch_health.ndjson"),
        log_file=Path("/tmp/render_ch.log"),
        lock_file=Path("/tmp/render_live_collector.lock"),
        pid_file=Path("/tmp/render_live_collector.pid"),
        socket_file=Path("/run/user/1000/orderbook_ob1000_render.sock"),
    ),
    CollectorSpec(
        name="xrp",
        symbols=("XRPUSDT",),
        health_file=Path("/tmp/xrp_ch_health.ndjson"),
        log_file=Path("/tmp/xrp_ch.log"),
        lock_file=Path("/tmp/xrp_live_collector.lock"),
        pid_file=Path("/tmp/xrp_live_collector.pid"),
        socket_file=Path("/run/user/1000/orderbook_ob1000_xrp.sock"),
    ),
)


SINGLE_SYMBOLS: tuple[str, ...] = (
    "ETHUSDT",
    "QQQUSDT",
    "XAUUSDT",
    "EURUSDUSDT",
    "SOLUSDT",
    "HYPEUSDT",
    "XAUTUSDT",
    "ZECUSDT",
    "LINKUSDT",
    "BNBUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "PUMPFUNUSDT",
    "1000PEPEUSDT",
    "AAVEUSDT",
    "LTCUSDT",
    "TAOUSDT",
    "DOTUSDT",
    "WLDUSDT",
    "TRXUSDT",
    "ENAUSDT",
    "NEARUSDT",
    "SHIB1000USDT",
    "HBARUSDT",
    "ARBUSDT",
    "PAXGUSDT",
    "APTUSDT",
    "PENGUUSDT",
    "UNIUSDT",
    "OPUSDT",
    "FARTCOINUSDT",
    "KAITOUSDT",
    "ALGOUSDT",
    "XMRUSDT",
    "XLMUSDT",
    "MNTUSDT",
    "WLFIUSDT",
    "CRVUSDT",
    "INJUSDT",
    "JTOUSDT",
    "XPLUSDT",
    "ICPUSDT",
    "LITUSDT",
    "TIAUSDT",
    "1000BONKUSDT",
    "TRUMPUSDT",
    "WIFUSDT",
    "ATOMUSDT",
)


def build_collectors() -> list[CollectorSpec]:
    collectors = list(SPECIAL_COLLECTORS)
    for symbol in SINGLE_SYMBOLS:
        lower = symbol.lower()
        collectors.append(
            CollectorSpec(
                name=lower,
                symbols=(symbol,),
                health_file=Path(f"/tmp/{lower}_ch_health.ndjson"),
                log_file=Path(f"/tmp/{lower}_ch.log"),
                lock_file=Path(f"/tmp/orderbook_v3_live_{lower}.lock"),
                pid_file=Path(f"/tmp/orderbook_v3_live_{lower}.pid"),
                socket_file=Path(f"/tmp/orderbook_ob1000_{lower}.sock"),
            )
        )
    return collectors


def read_last_json_line(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return None
            chunk_size = min(size, 65536)
            handle.seek(-chunk_size, os.SEEK_END)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(data.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def pid_alive(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        raw = pid_file.read_text().strip()
        pid = int(raw)
    except Exception:
        return False
    return Path(f"/proc/{pid}").exists()


def pid_value(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text().strip()
        pid = int(raw)
    except Exception:
        return None
    return pid


def file_age_seconds(path: Path) -> float | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return time.time() - stat.st_mtime


def terminate_pid(pid: int, timeout_seconds: float = 10.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return

    deadline = time.time() + timeout_seconds
    while time.time() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.5)

    if Path(f"/proc/{pid}").exists():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            return


def cleanup_stale_runtime(spec: CollectorSpec) -> None:
    pid = pid_value(spec.pid_file)
    if pid is not None and Path(f"/proc/{pid}").exists():
        terminate_pid(pid)

    for path in (spec.pid_file, spec.socket_file):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass
        except PermissionError:
            pass


def start_collector(spec: CollectorSpec, *, dry_run: bool = False) -> int | None:
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
    spec.lock_file.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        return None

    cleanup_stale_runtime(spec)

    with spec.log_file.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            spec.command(),
            cwd=str(ORDERBOOK_REPO),
            env=spec.env(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        spec.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
        return process.pid


def collector_state(spec: CollectorSpec, *, stale_seconds: int, startup_grace_seconds: int) -> tuple[str, str]:
    health = read_last_json_line(spec.health_file)
    pid_is_alive = pid_alive(spec.pid_file)
    pid_age = file_age_seconds(spec.pid_file)
    health_age = file_age_seconds(spec.health_file)

    if health is None:
        if pid_is_alive and (pid_age is not None and pid_age < startup_grace_seconds):
            return "starting", "health file not written yet"
        if pid_is_alive:
            return "stale", "health file missing"
        return "down", "health file missing"

    writer_state = str(health.get("writer_state") or "")
    last_error = str(health.get("last_error") or "")
    insert_failures = int(health.get("insert_failures_total") or 0)
    dropped_events = int(health.get("dropped_events_total") or 0)

    if not pid_is_alive:
        return "down", "pid not running"

    if health_age is not None and health_age > stale_seconds:
        if pid_age is not None and pid_age < startup_grace_seconds:
            return "starting", f"health stale but pid age={pid_age:.1f}s"
        return "stale", f"health stale age={health_age:.1f}s"

    if writer_state != "RUNNING":
        if pid_age is not None and pid_age < startup_grace_seconds:
            return "starting", f"writer_state={writer_state}"
        return "stale", f"writer_state={writer_state}"

    if insert_failures or dropped_events or last_error:
        return "stale", (
            f"writer_state={writer_state} insert_failures={insert_failures} "
            f"dropped_events={dropped_events} last_error={last_error!r}"
        )

    return "healthy", "ok"


def reconcile(specs: Iterable[CollectorSpec], *, stale_seconds: int, startup_grace_seconds: int, dry_run: bool = False) -> None:
    healthy = 0
    starting = 0
    restarted = 0
    stale = 0
    down = 0

    for spec in specs:
        state, reason = collector_state(
            spec,
            stale_seconds=stale_seconds,
            startup_grace_seconds=startup_grace_seconds,
        )

        if state == "healthy":
            healthy += 1
            continue
        if state == "starting":
            starting += 1
            print(f"[starting] {spec.name}: {reason}")
            continue

        if state in {"stale", "down"}:
            stale += 1 if state == "stale" else 0
            down += 1 if state == "down" else 0
            print(f"[restart] {spec.name}: {reason}")
            pid = pid_value(spec.pid_file)
            if pid is not None and Path(f"/proc/{pid}").exists():
                terminate_pid(pid)
            if dry_run:
                continue
            cleanup_stale_runtime(spec)
            new_pid = start_collector(spec, dry_run=False)
            restarted += 1
            print(f"[started] {spec.name}: pid={new_pid}")

    print(
        "[summary] "
        f"healthy={healthy} starting={starting} stale={stale} down={down} "
        f"restarted={restarted}"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument("--once", action="store_true", help="Run one reconciliation pass and exit.")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between loop iterations.")
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=30,
        help="Mark a collector stale if its health file is older than this.",
    )
    parser.add_argument(
        "--startup-grace-seconds",
        type=int,
        default=60,
        help="Allow a collector this long to produce its first health update.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report restarts without actually starting anything.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    specs = build_collectors()

    if args.once:
        reconcile(
            specs,
            stale_seconds=args.stale_seconds,
            startup_grace_seconds=args.startup_grace_seconds,
            dry_run=args.dry_run,
        )
        return 0

    if not args.loop:
        args.loop = True

    print(
        f"[watchdog] monitoring {len(specs)} collectors; "
        f"interval={args.interval}s stale={args.stale_seconds}s grace={args.startup_grace_seconds}s"
    )
    try:
        while True:
            reconcile(
                specs,
                stale_seconds=args.stale_seconds,
                startup_grace_seconds=args.startup_grace_seconds,
                dry_run=args.dry_run,
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("[watchdog] stopped")
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
