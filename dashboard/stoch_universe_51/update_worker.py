#!/usr/bin/env python3
"""Sequential candle-only universe update worker. Never starts the signal pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

from stoch_universe_51.coverage import bump_coverage_generation, iso_z  # noqa: E402
from stoch_universe_51.jsonio import read_json, write_json_atomic  # noqa: E402
from stoch_universe_51.update_jobs import (  # noqa: E402
    FORBIDDEN_SCRIPTS,
    backfill_argv_from_plan,
    public_message,
)


def _spawn_backfill(argv: list[str], cwd: Path, log_path: Path) -> int:
    if os.environ.get("STOCH_UNIVERSE_51_BACKFILL_STUB") == "1":
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("STUB " + " ".join(argv) + "\n")
        return 0
    joined = " ".join(argv)
    if any(tok in joined for tok in FORBIDDEN_SCRIPTS) or "--cleanup-first" in argv:
        raise RuntimeError("forbidden backfill argv")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            shell=False,
        )
        return int(proc.wait())


def run_job(job_dir: Path) -> int:
    request = read_json(job_dir / "request.json")
    if request.get("jobs_root"):
        os.environ["STOCH_UNIVERSE_51_JOBS_ROOT"] = str(request["jobs_root"])
    status = read_json(job_dir / "status.json")
    coins = {c["symbol"]: c for c in status.get("coins") or []}
    python = str(request["sg_python"])
    script = str(request["backfill_script"])
    universe_file = str(job_dir / request["universe_file"])
    sg_root = Path(str(request["sg_root"]))
    log_path = job_dir / "update.log"
    failed = 0
    success = int(status.get("already_current_count") or 0)
    completed = int(status.get("already_current_count") or 0)
    total = int(status.get("total_symbols") or len(request["symbols"]))

    def flush(current: str | None, message: str) -> None:
        status["current_symbol"] = current
        status["message"] = public_message(message)
        status["completed_symbols"] = completed
        status["success_count"] = success
        status["failed_count"] = failed
        status["coins"] = list(coins.values())
        write_json_atomic(job_dir / "status.json", status)
        write_json_atomic(job_dir / "progress.json", {"coins": status["coins"]})

    status["state"] = "RUNNING"
    status["started_at"] = status.get("started_at") or iso_z(datetime.now(timezone.utc))
    flush(None, "Datenaktualisierung läuft")

    for plan in request.get("plans") or []:
        symbol = plan["symbol"]
        if plan.get("action") == "ALREADY_CURRENT":
            coins[symbol] = {
                "symbol": symbol,
                "state": "ALREADY_CURRENT",
                "message": "bereits aktuell",
            }
            continue
        coins[symbol] = {"symbol": symbol, "state": "UPDATING", "message": f"{symbol} wird aktualisiert"}
        flush(symbol, f"{symbol} wird aktualisiert")
        ok = True
        for index, call in enumerate(plan.get("calls") or []):
            out_dir = str(job_dir / "backfill" / symbol)
            checkpoint = str(Path(out_dir) / "checkpoint.json")
            argv = backfill_argv_from_plan(
                call,
                python=python,
                script=script,
                universe_file=universe_file,
                symbol=symbol,
                out_dir=out_dir,
                checkpoint=checkpoint,
            )
            rc = _spawn_backfill(argv, sg_root, log_path)
            if rc != 0:
                ok = False
                coins[symbol] = {
                    "symbol": symbol,
                    "state": "FAILED",
                    "message": public_message(f"{symbol} fehlgeschlagen"),
                }
                failed += 1
                completed += 1
                flush(symbol, f"{symbol} fehlgeschlagen")
                break
            _ = index
        if ok:
            coins[symbol] = {"symbol": symbol, "state": "COMPLETED", "message": "aktualisiert"}
            success += 1
            completed += 1
            flush(symbol, f"{symbol} aktualisiert")

    if failed and success == 0 and completed == total:
        status["state"] = "FAILED"
        status["return_code"] = 1
        msg = "Aktualisierung fehlgeschlagen"
    elif failed:
        status["state"] = "COMPLETED_WITH_ERRORS"
        status["return_code"] = 1
        msg = "Aktualisierung mit Fehlern beendet"
    else:
        status["state"] = "COMPLETED"
        status["return_code"] = 0
        msg = "Aktualisierung abgeschlossen"
    status["finished_at"] = iso_z(datetime.now(timezone.utc))
    status["current_symbol"] = None
    flush(None, msg)
    bump_coverage_generation()
    lock = Path(str(request.get("jobs_root") or job_dir.parent)) / "ACTIVE.lock"
    try:
        if lock.exists():
            data = read_json(lock)
            if str(data.get("job_id")) == str(request.get("job_id")):
                lock.unlink()
    except OSError:
        pass
    try:
        import stoch_heavy_job_gate as heavy_gate

        heavy_gate.release(str(request.get("job_id") or ""))
    except Exception:  # noqa: BLE001
        pass
    return int(status.get("return_code") or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: update_worker.py JOB_ID JOB_DIR", file=sys.stderr)
        return 2
    job_dir = Path(args[1])
    return run_job(job_dir)


if __name__ == "__main__":
    raise SystemExit(main())
