"""Live Orderbook Runner Manager — dashboard-side process control (research-only).

Starts/stops the existing orderbook_analyse live level watch as a subprocess.
Also starts a temporary Bybit→ClickHouse recorder for the session symbol and
stops that recorder on Stop (no permanent feed). Does not duplicate analysis;
only orchestrates and reads output files.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_orderbook_copy import enrich_ob_grid_for_display, enrich_view_display

logger = logging.getLogger(__name__)

SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
ALLOWED_REPORT_INTERVALS = frozenset({60, 120, 300})

ORDERBOOK_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
ORDERBOOK_VENV_PYTHON = ORDERBOOK_ROOT / ".venv" / "bin" / "python"
ORDERBOOK_SCRIPT = ORDERBOOK_ROOT / "scripts" / "run_live_level_watch.py"
ORDERBOOK_RECORDER_SCRIPT = ORDERBOOK_ROOT / "scripts" / "run_recorder.py"
# Live watch only reads ClickHouse. Dashboard sessions therefore start a
# temporary Bybit→CH recorder for the selected symbol and stop it on Stop.
FEED_WARMUP_SECONDS = 6.0

STATUSES = (
    "STOPPED",
    "STARTING",
    "BOOTSTRAP_OK",
    "LIVE",
    "WAITING_FOR_DATA",
    "STALE_DATA",
    "GAP_DETECTED",
    "RECONNECTING",
    "STOPPING",
    "FAILED",
    "COMPLETED",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def validate_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if not SYMBOL_RE.fullmatch(s):
        raise ValueError("invalid symbol format")
    if any(ch in s for ch in ";|&`$<>\\\"'"):
        raise ValueError("invalid symbol characters")
    return s


def validate_report_interval(seconds: int | float) -> int:
    val = int(seconds)
    if val not in ALLOWED_REPORT_INTERVALS:
        raise ValueError("report interval must be 60, 120, or 300")
    return val


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _proc_environ_symbol(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for part in raw.split(b"\0"):
        if part.startswith(b"SYMBOL="):
            return part.split(b"=", 1)[1].decode("utf-8", "replace").strip().upper() or None
    return None


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def find_recorder_pids_for_symbol(symbol: str) -> list[int]:
    """Return live run_recorder.py PIDs whose SYMBOL env matches."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return []
    out: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            cmd = _proc_cmdline(pid)
            if "run_recorder.py" not in cmd:
                continue
            if _proc_environ_symbol(pid) == sym:
                out.append(pid)
    except OSError:
        return out
    return sorted(out)


def _terminate_pid(pid: int, *, timeout_seconds: float = 15.0) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _tail_jsonl(path: Path, n: int = 2) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        # Efficient enough for research outputs; read last ~256KB
        raw = path.read_bytes()
        chunk = raw[-262144:] if len(raw) > 262144 else raw
        lines = [ln for ln in chunk.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        out: list[dict[str, Any]] = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        return []


def _tail_text(path: Path, n: int = 150) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        raw = path.read_bytes()
        chunk = raw[-200000:] if len(raw) > 200000 else raw
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _tail_csv_rows(path: Path, n: int = 20) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        text = path.read_text(encoding="utf-8")
        reader = csv.DictReader(text.splitlines())
        rows = list(reader)
        return rows[-n:]
    except Exception:
        return []


@dataclass
class RunnerRecord:
    runner_id: str
    symbol: str
    pid: int | None = None
    status: str = "STOPPED"
    started_at: str | None = None
    stopped_at: str | None = None
    output_dir: str | None = None
    report_interval_seconds: int = 60
    sample_interval_seconds: int = 5
    last_sample_ts: str | None = None
    last_report_ts: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    feed_recorder_pid: int | None = None
    feed_recorder_owned: bool = False
    log_buffer: list[str] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("log_buffer", None)
        return d


class LiveOrderbookRunnerManager:
    """Single-runner manager (v1). Thread-safe."""

    def __init__(
        self,
        *,
        orderbook_root: Path = ORDERBOOK_ROOT,
        python_bin: Path = ORDERBOOK_VENV_PYTHON,
        script_path: Path = ORDERBOOK_SCRIPT,
        state_file: Path | None = None,
    ) -> None:
        self.orderbook_root = orderbook_root
        self.python_bin = python_bin
        self.script_path = script_path
        self.recorder_script = ORDERBOOK_RECORDER_SCRIPT
        self.state_file = state_file or (
            Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
            / "data"
            / "state"
            / "live_orderbook_runner.json"
        )
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._feed_proc: subprocess.Popen | None = None
        self._record: RunnerRecord | None = None
        self._last_start_mono = 0.0
        self._rate_limit_seconds = 3.0
        self._load_state()

    def _append_log(self, msg: str) -> None:
        ts = _utc_now().strftime("%H:%M:%S")
        line = f"{ts} {msg}"
        if self._record is not None:
            self._record.log_buffer.append(line)
            self._record.log_buffer = self._record.log_buffer[-200:]

    def _persist(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "runner": None if self._record is None else self._record.public_dict(),
            }
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as exc:
            logger.warning("live_orderbook state persist failed: %s", exc)

    def _load_state(self) -> None:
        data = _read_json(self.state_file)
        if not data or not data.get("runner"):
            return
        r = data["runner"]
        rec = RunnerRecord(
            runner_id=str(r.get("runner_id") or "unknown"),
            symbol=str(r.get("symbol") or ""),
            pid=r.get("pid"),
            status=str(r.get("status") or "STOPPED"),
            started_at=r.get("started_at"),
            stopped_at=r.get("stopped_at"),
            output_dir=r.get("output_dir"),
            report_interval_seconds=int(r.get("report_interval_seconds") or 60),
            sample_interval_seconds=int(r.get("sample_interval_seconds") or 5),
            last_sample_ts=r.get("last_sample_ts"),
            last_report_ts=r.get("last_report_ts"),
            exit_code=r.get("exit_code"),
            error_message=r.get("error_message"),
            feed_recorder_pid=r.get("feed_recorder_pid"),
            feed_recorder_owned=bool(r.get("feed_recorder_owned")),
        )
        if rec.pid and _pid_alive(int(rec.pid)) and rec.status not in {"STOPPED", "FAILED", "COMPLETED"}:
            self._record = rec
        else:
            if rec.status not in {"STOPPED", "FAILED", "COMPLETED"}:
                rec.status = "STOPPED"
                rec.stopped_at = rec.stopped_at or _iso(_utc_now())
            # Orphaned session feed from a previous dashboard process
            if rec.feed_recorder_owned and rec.feed_recorder_pid:
                _terminate_pid(int(rec.feed_recorder_pid))
                rec.feed_recorder_pid = None
                rec.feed_recorder_owned = False
            self._record = rec

    def _refresh_runtime_status(self) -> None:
        if self._record is None:
            return
        # Process exited?
        if self._proc is not None:
            code = self._proc.poll()
            if code is not None:
                self._record.exit_code = int(code)
                self._record.pid = self._proc.pid
                if self._record.status not in {"STOPPING", "STOPPED", "COMPLETED", "FAILED"}:
                    self._record.status = "COMPLETED" if code == 0 else "FAILED"
                    self._record.error_message = None if code == 0 else f"exit_code={code}"
                    self._append_log("STOPPED CLEANLY" if code == 0 else f"FAILED exit={code}")
                elif self._record.status == "STOPPING":
                    self._record.status = "STOPPED"
                    self._append_log("STOPPED CLEANLY")
                self._record.stopped_at = self._record.stopped_at or _iso(_utc_now())
                self._proc = None
                self._stop_owned_session_feed()
                self._persist()
                return

        if self._record.pid and not _pid_alive(int(self._record.pid)):
            if self._record.status not in {"STOPPED", "FAILED", "COMPLETED", "STOPPING"}:
                self._record.status = "FAILED"
                self._record.error_message = "process ended unexpectedly"
                self._record.stopped_at = _iso(_utc_now())
                self._append_log("FAILED unexpected exit")
                self._stop_owned_session_feed()
                self._persist()
            return

        # Map runner_status from summary/sample
        out = Path(self._record.output_dir) if self._record.output_dir else None
        summary = _read_json(out / "live_runner_summary.json") if out else None
        samples = _tail_jsonl(out / "live_samples.jsonl", 1) if out else []
        reports = _tail_jsonl(out / "five_minute_reports.jsonl", 1) if out else []
        if samples:
            self._record.last_sample_ts = samples[-1].get("sample_ts")
            rs = str(samples[-1].get("runner_status") or "")
            if self._record.status not in {"STOPPING", "STOPPED", "FAILED", "COMPLETED"}:
                if rs in {"BOOTSTRAP_OK", "WAITING_FOR_DATA", "STALE_DATA", "GAP_DETECTED", "RECONNECTING"}:
                    # Promote BOOTSTRAP_OK → LIVE once samples flow
                    if rs == "BOOTSTRAP_OK" and samples:
                        self._record.status = "LIVE"
                    else:
                        self._record.status = rs
                elif rs:
                    self._record.status = "LIVE"
        if reports:
            self._record.last_report_ts = reports[-1].get("report_ts")
        if summary and self._record.status == "STARTING":
            st = str(summary.get("status") or "")
            if st:
                self._record.status = "LIVE" if st == "BOOTSTRAP_OK" else st

    def _stop_owned_session_feed(self) -> None:
        """Stop temporary session recorder only if this manager started it."""
        if self._record is None:
            return
        if not self._record.feed_recorder_owned:
            self._feed_proc = None
            return
        pid = self._record.feed_recorder_pid
        if pid:
            self._append_log(f"FEED STOP {pid}")
            _terminate_pid(int(pid))
        if self._feed_proc is not None:
            try:
                self._feed_proc.wait(timeout=2)
            except Exception:
                pass
            self._feed_proc = None
        self._record.feed_recorder_pid = None
        self._record.feed_recorder_owned = False

    def _ensure_session_feed(self, symbol: str) -> dict[str, Any]:
        """Ensure Bybit→CH feed for symbol; own a temporary recorder if needed."""
        existing = find_recorder_pids_for_symbol(symbol)
        if existing:
            pid = int(existing[0])
            self._append_log(f"FEED REUSE {pid}")
            return {"success": True, "pid": pid, "owned": False}

        recorder = Path(self.recorder_script)
        if not self.python_bin.exists():
            return {"success": False, "error": "orderbook python venv not found"}
        if not recorder.exists():
            return {"success": False, "error": "orderbook recorder script not found"}

        logs = self.orderbook_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
        log_path = logs / f"dashboard_feed_{symbol}_{stamp}.log"
        cmd = [
            str(self.python_bin),
            "-u",
            str(recorder),
            "--duration",
            "0",
            "--log-level",
            "INFO",
        ]
        env = {
            **os.environ,
            "SYMBOL": symbol,
            "PYTHONPATH": str(self.orderbook_root / "src"),
        }
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
            self._feed_proc = subprocess.Popen(
                cmd,
                cwd=str(self.orderbook_root),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except Exception:
            logger.exception("session feed recorder start failed")
            return {"success": False, "error": "feed recorder start failed"}

        pid = int(self._feed_proc.pid)
        self._append_log(f"FEED START {pid}")
        self._append_log(f"FEED LOG {log_path.name}")
        # Short warmup so live watch can bootstrap from fresh CH rows.
        time.sleep(FEED_WARMUP_SECONDS)
        if self._feed_proc.poll() is not None:
            code = self._feed_proc.poll()
            self._feed_proc = None
            return {"success": False, "error": f"feed recorder exited early exit_code={code}"}
        return {"success": True, "pid": pid, "owned": True}

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_runtime_status()
            if self._record is None:
                return {
                    "active": False,
                    "status": "STOPPED",
                    "runner": None,
                    "runtime_seconds": None,
                }
            runtime = None
            if self._record.started_at and self._record.status not in {"STOPPED", "FAILED", "COMPLETED"}:
                try:
                    started = datetime.fromisoformat(self._record.started_at)
                    runtime = max(0.0, (_utc_now() - started).total_seconds())
                except Exception:
                    runtime = None
            return {
                "active": self._record.status not in {"STOPPED", "FAILED", "COMPLETED"},
                "status": self._record.status,
                "runner": self._record.public_dict(),
                "runtime_seconds": runtime,
                "log_tail": list(self._record.log_buffer[-50:]),
            }

    def start_runner(self, *, symbol: str, report_interval_seconds: int) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if now - self._last_start_mono < self._rate_limit_seconds:
                return {"success": False, "error": "rate limited"}
            self._refresh_runtime_status()
            if self._record and self._record.status in {
                "STARTING",
                "LIVE",
                "BOOTSTRAP_OK",
                "WAITING_FOR_DATA",
                "STALE_DATA",
                "RECONNECTING",
                "STOPPING",
            }:
                return {"success": False, "error": "runner already active"}

            try:
                symbol = validate_symbol(symbol)
                interval = validate_report_interval(report_interval_seconds)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}

            if not self.python_bin.exists():
                return {"success": False, "error": "orderbook python venv not found"}
            if not self.script_path.exists():
                return {"success": False, "error": "live level watch script not found"}

            stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
            out_dir = self.orderbook_root / "results" / f"dashboard_live_{symbol}_{stamp}"
            out_dir.mkdir(parents=True, exist_ok=True)

            runner_id = f"lob_{symbol}_{stamp}"
            self._record = RunnerRecord(
                runner_id=runner_id,
                symbol=symbol,
                status="STARTING",
                started_at=_iso(_utc_now()),
                output_dir=str(out_dir),
                report_interval_seconds=interval,
            )
            self._append_log(f"START {symbol}")
            self._append_log(f"WINDOW {interval}s")

            feed = self._ensure_session_feed(symbol)
            if not feed.get("success"):
                self._record.status = "FAILED"
                self._record.error_message = str(feed.get("error") or "feed start failed")
                self._record.stopped_at = _iso(_utc_now())
                self._append_log(f"FAILED feed: {self._record.error_message}")
                self._persist()
                return {"success": False, "error": self._record.error_message}
            self._record.feed_recorder_pid = feed.get("pid")
            self._record.feed_recorder_owned = bool(feed.get("owned"))
            self._persist()

            cmd = [
                str(self.python_bin),
                "-u",
                str(self.script_path),
                "--symbol",
                symbol,
                "--sample-interval-seconds",
                "5",
                "--report-interval-seconds",
                str(interval),
                "--zone-approach-distance-bps",
                "25",
                "--zone-release-distance-bps",
                "35",
                "--zone-activation-samples",
                "2",
                "--zone-release-samples",
                "3",
                "--output-dir",
                str(out_dir),
                "--log-level",
                "INFO",
                "--no-color",
            ]
            env = {
                **os.environ,
                "PYTHONPATH": str(self.orderbook_root / "src"),
            }
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.orderbook_root),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except Exception as exc:
                self._stop_owned_session_feed()
                self._record.status = "FAILED"
                self._record.error_message = "start failed"
                self._append_log("FAILED start")
                self._persist()
                logger.exception("live orderbook start failed")
                return {"success": False, "error": "start failed"}

            self._record.pid = self._proc.pid
            self._append_log(f"PROCESS {self._proc.pid}")
            self._append_log(f"OUTPUT {out_dir.name}")
            self._last_start_mono = now
            self._persist()
            # Soft promote after short delay via status refresh
            return {
                "success": True,
                "runner": self._record.public_dict(),
                "message": f"started {symbol}",
            }

    def stop_runner(self, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        with self._lock:
            self._refresh_runtime_status()
            if self._record is None or self._record.status in {"STOPPED", "FAILED", "COMPLETED"}:
                # Still drop a leftover session feed if we own one.
                if self._record is not None and self._record.feed_recorder_owned:
                    self._stop_owned_session_feed()
                    self._persist()
                return {"success": True, "message": "already stopped", "runner": None if self._record is None else self._record.public_dict()}

            self._record.status = "STOPPING"
            self._append_log("STOP requested")
            self._persist()
            proc = self._proc
            pid = self._record.pid
            # Verify ownership
            if proc is not None and pid and proc.pid != pid:
                return {"success": False, "error": "pid mismatch"}

            target_pid = proc.pid if proc is not None else pid
            if target_pid and _pid_alive(int(target_pid)):
                try:
                    os.kill(int(target_pid), signal.SIGTERM)
                except OSError as exc:
                    self._record.status = "FAILED"
                    self._record.error_message = "stop failed"
                    self._persist()
                    return {"success": False, "error": "stop failed"}

                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    if proc is not None and proc.poll() is not None:
                        break
                    if not _pid_alive(int(target_pid)):
                        break
                    time.sleep(0.2)
                else:
                    try:
                        os.kill(int(target_pid), signal.SIGKILL)
                        self._append_log("STOP hard kill")
                    except OSError:
                        pass

            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                self._record.exit_code = proc.poll()
            self._proc = None
            self._stop_owned_session_feed()
            self._record.status = "STOPPED"
            self._record.stopped_at = _iso(_utc_now())
            self._append_log("STOPPED CLEANLY")
            self._persist()
            return {"success": True, "runner": self._record.public_dict()}

    def restart_runner(self, *, symbol: str | None = None, report_interval_seconds: int | None = None) -> dict[str, Any]:
        with self._lock:
            cur_symbol = symbol
            cur_interval = report_interval_seconds
            if self._record is not None:
                cur_symbol = cur_symbol or self._record.symbol
                cur_interval = cur_interval or self._record.report_interval_seconds
        if not cur_symbol or cur_interval is None:
            return {"success": False, "error": "no previous runner config"}
        stop = self.stop_runner()
        if not stop.get("success"):
            return stop
        return self.start_runner(symbol=cur_symbol, report_interval_seconds=int(cur_interval))

    def read_recent_logs(self, *, limit: int = 150) -> list[str]:
        with self._lock:
            self._refresh_runtime_status()
            buf = list(self._record.log_buffer) if self._record else []
            out_dir = Path(self._record.output_dir) if self._record and self._record.output_dir else None
        file_lines: list[str] = []
        if out_dir is not None:
            file_lines = _tail_text(out_dir / "live_runner.log", limit)
        # Prefer structured buffer + file tail
        merged = buf + [ln for ln in file_lines if ln not in buf]
        return merged[-limit:]

    def get_latest_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_runtime_status()
            rec = self._record
            status = self.get_status()

        if rec is None or not rec.output_dir:
            return {
                "status": status,
                "sample": None,
                "previous_sample": None,
                "report": None,
                "previous_report": None,
                "transitions": [],
                "view": enrich_view_display(self._empty_view()),
            }

        out = Path(rec.output_dir)
        samples = _tail_jsonl(out / "live_samples.jsonl", 2)
        reports = _tail_jsonl(out / "five_minute_reports.jsonl", 2)
        transitions = _tail_csv_rows(out / "zone_state_transitions.csv", 40)
        summary = _read_json(out / "live_runner_summary.json")
        sample = samples[-1] if samples else None
        prev = samples[-2] if len(samples) > 1 else None
        report = reports[-1] if reports else None
        prev_report = reports[-2] if len(reports) > 1 else None
        view = self._build_view(
            sample=sample,
            previous=prev,
            report=report,
            previous_report=prev_report,
            status=status,
            summary=summary,
            transitions=transitions,
            sample_interval_seconds=int(rec.sample_interval_seconds or 5),
        )
        return {
            "status": status,
            "sample": sample,
            "previous_sample": prev,
            "report": report,
            "previous_report": prev_report,
            "summary": summary,
            "transitions": transitions,
            "view": view,
        }

    def _empty_view(self) -> dict[str, Any]:
        return {
            "symbol": None,
            "state": "NO_ACTIVE_ZONE",
            "setup": "NO_TRADE",
            "mid_price": None,
            "data_age_seconds": None,
            "sample_ts": None,
            "sample_interval_seconds": 5,
            "report_window_seconds": None,
            "level_source": "sample",
            "window_source": "report",
            "resistance": None,
            "support": None,
            "support2": None,
            "wall_follow": [],
            "market_flow": None,
            "liquidations": None,
            "absorption": None,
            "near_price": None,
            "level_quality": None,
            "money_flow": None,
            "overall": {"reading": "N/A", "decision": "NO_TRADE"},
            "readings": [],
            "ob_grid": None,
        }

    @staticmethod
    def _zone_center(zone: dict[str, Any] | None) -> float | None:
        if not zone:
            return None
        if zone.get("zone_center") is not None:
            try:
                return float(zone["zone_center"])
            except (TypeError, ValueError):
                pass
        lo, hi = zone.get("zone_low"), zone.get("zone_high")
        if lo is None or hi is None:
            return None
        try:
            return (float(lo) + float(hi)) / 2.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _notional_change_pct(current: dict[str, Any] | None, previous: dict[str, Any] | None) -> float | None:
        if not current or not previous:
            return None
        try:
            cur = float(current.get("notional"))
            prev = float(previous.get("notional"))
        except (TypeError, ValueError):
            return None
        if prev == 0:
            return None
        return (cur - prev) / abs(prev) * 100.0

    def _zone_card(
        self,
        *,
        key: str,
        current: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        mid: float | None,
        wall: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if current is None:
            return None
        cur_low = current.get("zone_low")
        cur_high = current.get("zone_high")
        prev_low = None if previous is None else previous.get("zone_low")
        prev_high = None if previous is None else previous.get("zone_high")
        cur_mid = self._zone_center(current)
        prev_mid = self._zone_center(previous)
        move_pct = None
        direction = "unchanged"
        if cur_mid is not None and prev_mid is not None and prev_mid != 0:
            move_pct = (cur_mid - prev_mid) / abs(prev_mid) * 100.0
            if move_pct > 0.01:
                direction = "up"
            elif move_pct < -0.01:
                direction = "down"
        dist_pct = None
        if mid and cur_low is not None and cur_high is not None and mid > 0:
            if key == "resistance":
                dist_pct = (float(cur_low) - mid) / mid * 100.0
            else:
                dist_pct = (mid - float(cur_high)) / mid * 100.0
        behaviour = None if wall is None else wall.get("reading")
        # Prefer sample-to-sample notional change; fall back to report wall_follow
        nchg = self._notional_change_pct(current, previous)
        if nchg is None and wall is not None:
            nchg = wall.get("notional_change_pct")
        return {
            "zone_low": cur_low,
            "zone_high": cur_high,
            "previous_low": prev_low,
            "previous_high": prev_high,
            "previous_center": prev_mid,
            "current_center": cur_mid,
            "move_pct": move_pct,
            "direction": direction,
            "strength": current.get("strength"),
            "notional": current.get("notional"),
            "notional_change_pct": nchg,
            "distance_pct": dist_pct,
            "behaviour": behaviour,
            "persistence_samples": current.get("persistence_samples"),
            "side": current.get("side"),
            "role": current.get("role"),
        }

    @staticmethod
    def _band_notional(walls: list[dict[str, Any]], lo_bps: float, hi_bps: float) -> float:
        total = 0.0
        for w in walls:
            try:
                dist = float(w.get("distance_to_mid_bps"))
                notion = float(w.get("wall_notional") or 0.0)
            except (TypeError, ValueError):
                continue
            if lo_bps <= dist < hi_bps:
                total += notion
        return total

    def _near_price_from_sample(self, sample: dict[str, Any] | None) -> dict[str, Any] | None:
        if not sample:
            return None
        bids = sample.get("strongest_bid_walls") or []
        asks = sample.get("strongest_ask_walls") or []
        if not isinstance(bids, list):
            bids = []
        if not isinstance(asks, list):
            asks = []
        if not bids and not asks:
            return None

        bid_0_10 = self._band_notional(bids, 0.0, 10.0)
        ask_0_10 = self._band_notional(asks, 0.0, 10.0)
        bid_10_25 = self._band_notional(bids, 10.0, 25.0)
        ask_10_25 = self._band_notional(asks, 10.0, 25.0)

        def shares(b: float, a: float) -> tuple[float | None, float | None]:
            tot = b + a
            if tot <= 0:
                return None, None
            return 100.0 * b / tot, 100.0 * a / tot

        b0, a0 = shares(bid_0_10, ask_0_10)
        b1, a1 = shares(bid_10_25, ask_10_25)
        if b0 is None and a0 is None and b1 is None and a1 is None:
            # Walls exist but none inside 25 bps — still publish empty bands honestly
            return {
                "bid_share_0_10": None,
                "ask_share_0_10": None,
                "bid_share_10_25": None,
                "ask_share_10_25": None,
                "bias": "NONE_NEAR",
            }
        return {
            "bid_share_0_10": b0,
            "ask_share_0_10": a0,
            "bid_share_10_25": b1,
            "ask_share_10_25": a1,
        }

    @staticmethod
    def _level_quality_from_ladder(
        *,
        state: str,
        support: dict[str, Any] | None,
        resistance: dict[str, Any] | None,
        transitions: list[dict[str, str]],
        sample_interval_seconds: int,
    ) -> dict[str, Any] | None:
        st = (state or "").upper()
        zone: dict[str, Any] | None = None
        if st.startswith("SUPPORT"):
            zone = support
        elif st.startswith("RESISTANCE"):
            zone = resistance
        else:
            zone = support or resistance
        if zone is None:
            return None
        try:
            persistence = int(float(zone.get("persistence_samples") or 0))
        except (TypeError, ValueError):
            persistence = 0
        age_s = max(0, persistence * max(1, sample_interval_seconds))
        if age_s < 60:
            age_display = f"{age_s}s"
        else:
            age_display = f"{age_s // 60}m {age_s % 60}s"

        tests = 0
        for row in transitions or []:
            new_st = str(row.get("new_state") or "").upper()
            if "TEST" in new_st or "RETEST" in new_st:
                tests += 1

        if persistence >= 60 or tests >= 5:
            fatigue = "HIGH"
        elif persistence >= 12 or tests >= 2:
            fatigue = "MEDIUM"
        else:
            fatigue = "LOW"

        return {
            "tests": tests,
            "age_display": age_display,
            "age_seconds": age_s,
            "persistence_samples": persistence,
            "fatigue": fatigue,
        }

    @staticmethod
    def _flow_bundle(flow: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if not flow:
            return {"status": "collecting"}, {"status": "collecting"}
        liq = {
            "buy_notional": flow.get("buy_liquidation_notional"),
            "sell_notional": flow.get("sell_liquidation_notional"),
            "count": flow.get("liquidation_count"),
            "complete": flow.get("data_complete"),
        }
        money = {
            "delta_notional": flow.get("delta_notional"),
            "delta_ratio": flow.get("delta_ratio"),
            "buy_notional": flow.get("buy_notional"),
            "sell_notional": flow.get("sell_notional"),
            "oi_change_pct": flow.get("oi_change_pct"),
            "price_change_pct": flow.get("price_change_pct"),
            "complete": flow.get("data_complete"),
        }
        return liq, money

    def _build_view(
        self,
        *,
        sample: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        report: dict[str, Any] | None,
        previous_report: dict[str, Any] | None = None,
        status: dict[str, Any],
        summary: dict[str, Any] | None,
        transitions: list[dict[str, str]] | None = None,
        sample_interval_seconds: int = 5,
    ) -> dict[str, Any]:
        view = self._empty_view()
        runner = status.get("runner") or {}
        view["symbol"] = runner.get("symbol")
        view["report_window_seconds"] = runner.get("report_interval_seconds")
        view["sample_interval_seconds"] = sample_interval_seconds
        view["runner_status"] = status.get("status")

        # Levels / mid / state: prefer latest 5s sample (volatile-coin path)
        level_src = sample or report
        if level_src is None:
            return enrich_view_display(view)

        ladder = (level_src.get("ladder") or {}) if isinstance(level_src.get("ladder"), dict) else {}
        prev_ladder: dict[str, Any] = {}
        if previous and isinstance(previous.get("ladder"), dict):
            prev_ladder = previous["ladder"]

        mid = level_src.get("mid_price")
        view["mid_price"] = mid
        view["sample_ts"] = level_src.get("sample_ts") or (report or {}).get("report_ts")
        view["level_source"] = "sample" if sample is not None else "report"

        state = level_src.get("state") or (summary or {}).get("zone_state")
        if not state and report:
            state = report.get("state")
        view["state"] = state or "NO_ACTIVE_ZONE"

        setup = level_src.get("setup")
        if setup is None and report is not None:
            setup = report.get("setup")
        if isinstance(setup, dict):
            view["setup"] = setup.get("setup") or "NO_TRADE"
        else:
            view["setup"] = "NO_TRADE"

        view["data_age_seconds"] = level_src.get("data_age_seconds")
        if sample and view["data_age_seconds"] is None:
            view["data_age_seconds"] = sample.get("data_age_seconds")

        # Window aggregates stay on report cadence
        window_src = report
        view["window_source"] = "report" if report is not None else "none"
        view["price_change_pct"] = None if window_src is None else window_src.get("price_change_pct")
        view["readings"] = [] if window_src is None else (window_src.get("readings") or [])
        view["decision"] = (
            (None if window_src is None else window_src.get("decision")) or view["setup"]
        )

        wall_rows = [] if window_src is None else (window_src.get("wall_follow") or [])
        wall_by_label = {str(w.get("label") or ""): w for w in wall_rows}
        view["wall_follow"] = wall_rows

        mid_f = float(mid) if mid is not None else None
        view["resistance"] = self._zone_card(
            key="resistance",
            current=ladder.get("resistance"),
            previous=prev_ladder.get("resistance"),
            mid=mid_f,
            wall=wall_by_label.get("ASK / RESISTANCE"),
        )
        view["support"] = self._zone_card(
            key="support",
            current=ladder.get("support"),
            previous=prev_ladder.get("support"),
            mid=mid_f,
            wall=wall_by_label.get("BID / SUPPORT"),
        )
        view["support2"] = self._zone_card(
            key="support2",
            current=ladder.get("support2"),
            previous=prev_ladder.get("support2"),
            mid=mid_f,
            wall=wall_by_label.get("BID / SUPPORT 2"),
        )

        flow = None
        if window_src and isinstance(window_src.get("market_flow"), dict):
            flow = window_src.get("market_flow")
        view["market_flow"] = flow
        liq, money = self._flow_bundle(flow)
        prev_flow = None
        if previous_report and isinstance(previous_report.get("market_flow"), dict):
            prev_flow = previous_report.get("market_flow")
        prev_liq, _ = self._flow_bundle(prev_flow)
        if liq.get("status") != "collecting" and prev_liq.get("status") != "collecting":
            try:
                cur_total = float(liq.get("buy_notional") or 0) + float(liq.get("sell_notional") or 0)
                prev_total = float(prev_liq.get("buy_notional") or 0) + float(prev_liq.get("sell_notional") or 0)
                liq["rising"] = cur_total > prev_total * 1.15 and cur_total - prev_total >= 500
            except (TypeError, ValueError):
                liq["rising"] = None
        view["liquidations"] = liq
        view["money_flow"] = money

        # Derived from live sample walls / ladder — no invented absorption
        view["absorption"] = None
        view["near_price"] = self._near_price_from_sample(sample)
        view["level_quality"] = self._level_quality_from_ladder(
            state=str(view["state"]),
            support=ladder.get("support"),
            resistance=ladder.get("resistance"),
            transitions=transitions or [],
            sample_interval_seconds=sample_interval_seconds,
        )
        view["overall"] = {
            "reading": "; ".join(view["readings"][:3]) if view["readings"] else "N/A",
            "decision": view.get("decision") or "NO_TRADE",
        }
        # Causal OB grid from live sample (symbol-independent; display-only)
        grid = None
        if sample and isinstance(sample.get("ob_grid"), dict):
            grid = enrich_ob_grid_for_display(sample.get("ob_grid"), sample)
        view["ob_grid"] = grid
        return enrich_view_display(view)


# Process-wide singleton for the dashboard app
live_orderbook_manager = LiveOrderbookRunnerManager()
