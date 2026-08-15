"""Atomic stage status so a run is never only snapshot_before.json."""

from __future__ import annotations

import resource
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .jsonio import write_json_atomic


def _rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StageRecorder:
    def __init__(self, run_dir: Path | None, *, run_id: str = "") -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.stages: list[dict[str, Any]] = []
        self.status = "RUNNING"
        self._log_lines: list[str] = []
        self.flush()

    def mark(self, status: str) -> None:
        self.status = status
        self.flush()

    def flush(self) -> None:
        if self.run_dir is None:
            return
        payload = {
            "run_id": self.run_id,
            "status": self.status,
            "updated_at": _now(),
            "peak_rss_kb": _rss_kb(),
            "stages": self.stages,
        }
        write_json_atomic(self.run_dir / "status.json", payload)
        log_path = self.run_dir / "run.log"
        if self._log_lines:
            existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
            log_path.write_text(existing + "".join(self._log_lines), encoding="utf-8")
            self._log_lines.clear()

    @contextmanager
    def stage(self, name: str, *, input_rows: int | None = None) -> Iterator[dict[str, Any]]:
        row: dict[str, Any] = {
            "name": name,
            "started_at": _now(),
            "input_rows": input_rows,
            "output_rows": None,
            "duration_s": None,
            "rss_kb": _rss_kb(),
            "status": "RUNNING",
        }
        self.stages.append(row)
        self._log_lines.append(f"{row['started_at']} START {name} input_rows={input_rows}\n")
        self.flush()
        t0 = time.perf_counter()
        try:
            yield row
            row["status"] = "OK"
        except Exception as exc:
            row["status"] = "FAILED"
            row["error"] = str(exc)
            self.status = "FAILED"
            raise
        finally:
            row["duration_s"] = round(time.perf_counter() - t0, 6)
            row["ended_at"] = _now()
            row["rss_kb"] = _rss_kb()
            self._log_lines.append(
                f"{row['ended_at']} END {name} status={row['status']} "
                f"duration_s={row['duration_s']} output_rows={row.get('output_rows')}\n"
            )
            self.flush()
