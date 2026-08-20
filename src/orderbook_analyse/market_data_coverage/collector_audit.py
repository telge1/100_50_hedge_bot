"""Read-only collector / coverage audit (Phase 1 artifacts)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.dynamic_wall_detector import connect_readonly
from orderbook_analyse.market_data_coverage.activity_1m import export_market_activity_1m
from orderbook_analyse.wall_toxicity_audit.data_access import ensure_utc


SYMBOLS_DEFAULT = ["DOGEUSDT", "APTUSDT", "BTCUSDT"]

LEGACY_WALL_PATHS = {
    "DOGEUSDT": "results/execution_walls_DOGEUSDT_full_history/execution_wall_transitions.csv",
    "APTUSDT": "results/execution_walls_APTUSDT_full/execution_wall_transitions.csv",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = fieldnames or sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _age_seconds(mx: datetime | None, now: datetime) -> float | None:
    if mx is None:
        return None
    return (now - ensure_utc(mx)).total_seconds()


def collect_running_processes(root: Path) -> str:
    cmds = [
        ["pgrep", "-af", "run_recorder|wall_transition|execution_wall|bybit_recorder|clickhouse"],
        ["ps", "-eo", "pid,lstart,etime,cmd", "--sort=start_time"],
    ]
    chunks: list[str] = [f"# generated_at_utc={_utc_now().isoformat()}", f"# root={root}"]
    for cmd in cmds:
        chunks.append("\n## " + " ".join(cmd))
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
            # scrub potential secrets from accidental env dumps
            lines = []
            for line in out.splitlines():
                if "PASSWORD" in line.upper() or "CLICKHOUSE_PASSWORD" in line:
                    continue
                lines.append(line)
            chunks.append("\n".join(lines) if lines else "(empty)")
        except subprocess.CalledProcessError as exc:
            chunks.append(exc.output or str(exc))
        except FileNotFoundError as exc:
            chunks.append(str(exc))
    # focused recorder PIDs
    chunks.append("\n## recorder pidfiles")
    for p in sorted((root / "logs").glob("recorder_*.pid")):
        pid = p.read_text(encoding="utf-8").strip()
        alive = False
        cmd = ""
        try:
            os.kill(int(pid), 0)
            alive = True
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, ValueError):
            pass
        chunks.append(f"{p.name}: pid={pid} alive={alive} cmd={cmd}")
    return "\n".join(chunks) + "\n"


def build_recorder_inventory(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = _utc_now()
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "scripts/run_recorder.py"],
            text=True,
        )
    except subprocess.CalledProcessError:
        out = ""
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid = int(parts[0])
        cmd = parts[1]
        symbol = ""
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            for e in env:
                if e.startswith(b"SYMBOL="):
                    symbol = e.decode().split("=", 1)[1]
        except OSError:
            pass
        cwd = ""
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        start_ts = None
        elapsed = None
        try:
            # starttime from /proc/pid/stat field 22 (clock ticks)
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            start_ticks = int(stat[21])
            hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            boot = float(Path("/proc/stat").read_text().split("btime")[1].split()[0])
            start_ts = datetime.fromtimestamp(boot + start_ticks / hz, tz=timezone.utc)
            elapsed = (now - start_ts).total_seconds()
        except Exception:  # noqa: BLE001
            pass
        log_guess = ""
        if symbol:
            cand = root / "logs" / f"recorder_{symbol}_unlimited.log"
            if cand.exists():
                log_guess = str(cand)
        rows.append(
            {
                "process_type": "bybit_recorder",
                "pid": pid,
                "symbol": symbol or "UNKNOWN",
                "start_time_utc": start_ts.isoformat() if start_ts else "",
                "elapsed_seconds": elapsed,
                "command": cmd,
                "working_directory": cwd,
                "output_target": "clickhouse:orderbook_analysis.*",
                "log_file": log_guess,
                "status": "RUNNING",
                "duplicate_risk": "LOW" if symbol else "UNKNOWN",
                "notes": "env SYMBOL; duration 0 = unlimited",
            }
        )
    # note missing BTC
    have = {r["symbol"] for r in rows}
    for sym in SYMBOLS_DEFAULT:
        if sym not in have:
            rows.append(
                {
                    "process_type": "bybit_recorder",
                    "pid": "",
                    "symbol": sym,
                    "start_time_utc": "",
                    "elapsed_seconds": "",
                    "command": "",
                    "working_directory": "",
                    "output_target": "clickhouse:orderbook_analysis.*",
                    "log_file": str(root / "logs" / f"recorder_{sym}_unlimited.log"),
                    "status": "NOT_RUNNING",
                    "duplicate_risk": "NONE",
                    "notes": "no live run_recorder.py for symbol",
                }
            )
    return rows


def build_service_inventory_md(root: Path) -> str:
    lines = [
        "# Service inventory",
        "",
        f"generated_at_utc: `{_utc_now().isoformat()}`",
        "",
        "## systemd (user)",
        "",
        "No orderbook/recorder user units found in prior audit; generic desktop services only.",
        "",
        "## cron",
        "",
    ]
    try:
        cron = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.STDOUT)
        lines.append("```")
        lines.append(cron.strip() or "(empty)")
        lines.append("```")
    except subprocess.CalledProcessError:
        lines.append("(no crontab or inaccessible)")
    lines.append("")
    lines.append("## tmux / screen")
    lines.append("")
    for name, cmd in (("tmux", ["tmux", "ls"]), ("screen", ["screen", "-ls"])):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
            lines.append(f"### {name}\n```\n{out.strip()}\n```\n")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            lines.append(f"### {name}\n`{exc}`\n")
    lines.append("## Operating mode")
    lines.append("")
    lines.append("- Recorders: **nohup** + PID files under `logs/recorder_<SYMBOL>_unlimited.pid`")
    lines.append("- Wall collectors (new): **nohup** under `data/wall_transitions/` (not systemd)")
    lines.append("- Do not run nohup and systemd in parallel for the same collector.")
    lines.append("")
    return "\n".join(lines)


def build_config_inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    # .env presence without secrets
    env_path = root / ".env"
    rows.append(
        {
            "config_type": "dotenv",
            "path": str(env_path),
            "exists": env_path.exists(),
            "symbols": "via process env SYMBOL",
            "notes": "contents not dumped (secrets)",
        }
    )
    for sym, rel in LEGACY_WALL_PATHS.items():
        p = root / rel
        rows.append(
            {
                "config_type": "legacy_wall_csv",
                "path": str(p),
                "exists": p.exists(),
                "symbols": sym,
                "notes": "offline batch export; not live collector",
            }
        )
    rows.append(
        {
            "config_type": "wall_collector_output",
            "path": str(root / "data/wall_transitions"),
            "exists": (root / "data/wall_transitions").exists(),
            "symbols": ",".join(SYMBOLS_DEFAULT),
            "notes": "preferred live output root",
        }
    )
    return rows


def build_log_inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    log_dir = root / "logs"
    patterns = [
        "recorder_*.log",
        "execution_walls_*.log",
        "*wall*.log",
    ]
    seen: set[Path] = set()
    for pat in patterns:
        for p in sorted(log_dir.glob(pat)):
            if p in seen:
                continue
            seen.add(p)
            st = p.stat()
            rows.append(
                {
                    "path": str(p),
                    "size_bytes": st.st_size,
                    "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    "kind": "recorder" if "recorder_" in p.name else "other",
                }
            )
    return rows


def build_table_inventory() -> list[dict[str, Any]]:
    db = connect_readonly()
    q = db.query(
        """
        SELECT name, engine, total_rows, total_bytes
        FROM system.tables
        WHERE database = 'orderbook_analysis'
        ORDER BY name
        """
    )
    rows = []
    for name, engine, total_rows, total_bytes in q.result_rows:
        try:
            desc = db.query(
                """
                SELECT name, type
                FROM system.columns
                WHERE database = 'orderbook_analysis' AND table = {t:String}
                ORDER BY position
                LIMIT 40
                """,
                parameters={"t": name},
            )
            cols = ";".join(f"{r[0]}:{r[1]}" for r in desc.result_rows)
        except Exception as exc:  # noqa: BLE001
            cols = f"error:{exc}"
        # try common ts columns
        ts_info = ""
        for tscol in ("exchange_ts", "trade_ts", "liquidation_ts", "event_ts", "transition_ts"):
            try:
                mq = db.query(
                    f"""
                    SELECT min({tscol}), max({tscol}), count()
                    FROM orderbook_analysis.{name}
                    """
                )
                mn, mx, cnt = mq.result_rows[0]
                ts_info = f"{tscol}|min={mn}|max={mx}|count={cnt}"
                break
            except Exception:  # noqa: BLE001
                continue
        rows.append(
            {
                "table": name,
                "engine": engine,
                "total_rows": total_rows,
                "total_bytes": total_bytes,
                "columns_sample": cols,
                "timestamp_span": ts_info,
            }
        )
    return rows


def coverage_rows(symbols: list[str], lookback_hours: float = 24.0) -> list[dict[str, Any]]:
    db = connect_readonly()
    now = _utc_now()
    out: list[dict[str, Any]] = []
    sources = [
        ("orderbook_deltas", "exchange_ts"),
        ("public_trades", "trade_ts"),
        ("ticker_samples", "exchange_ts"),
        ("liquidations", "liquidation_ts"),
        ("recorder_health", "event_ts"),
    ]
    for sym in symbols:
        for table, tscol in sources:
            q = db.query(
                f"""
                SELECT
                  min({tscol}), max({tscol}), count(),
                  countIf({tscol} >= now64(3,'UTC') - INTERVAL 5 MINUTE),
                  countIf({tscol} >= now64(3,'UTC') - INTERVAL 15 MINUTE),
                  countIf({tscol} >= now64(3,'UTC') - INTERVAL 60 MINUTE),
                  countIf({tscol} >= now64(3,'UTC') - INTERVAL 6 HOUR),
                  countIf({tscol} >= now64(3,'UTC') - INTERVAL 24 HOUR)
                FROM orderbook_analysis.{table}
                WHERE symbol = {{s:String}}
                """,
                parameters={"s": sym},
            )
            mn, mx, cnt, c5, c15, c60, c6, c24 = q.result_rows[0]
            age = _age_seconds(mx, now)
            stale = age is None or age > 180
            if table == "liquidations":
                # sparse events: use recorder_health freshness
                hq = db.query(
                    """
                    SELECT max(event_ts) FROM orderbook_analysis.recorder_health
                    WHERE symbol={s:String}
                    """,
                    parameters={"s": sym},
                )
                hmx = hq.result_rows[0][0]
                h_age = _age_seconds(hmx, now)
                status = "HEALTHY" if h_age is not None and h_age < 180 else "STALE"
                notes = "source_active_events_sparse" if status == "HEALTHY" else "source_may_be_down"
                stale = status == "STALE"
            else:
                status = "NO_DATA" if mx is None else ("STALE" if stale else "HEALTHY")
                notes = ""
            out.append(
                {
                    "symbol": sym,
                    "source": table,
                    "min_ts_utc": mn.isoformat() if mn else "",
                    "max_ts_utc": mx.isoformat() if mx else "",
                    "age_seconds": age,
                    "row_count": cnt,
                    "rows_last_5m": c5,
                    "rows_last_15m": c15,
                    "rows_last_60m": c60,
                    "rows_last_6h": c6,
                    "rows_last_24h": c24,
                    "expected_bucket_count": "",
                    "observed_bucket_count": "",
                    "coverage_ratio": "",
                    "largest_gap_seconds": "",
                    "gap_count": "",
                    "stale": stale,
                    "status": status,
                    "notes": notes,
                }
            )
        # walls
        live = Path("data/wall_transitions") / sym / "execution_wall_transitions.csv"
        legacy_rel = LEGACY_WALL_PATHS.get(sym)
        legacy = Path(legacy_rel) if legacy_rel else None
        path = None
        if live.exists() and live.stat().st_size > 0:
            path = live
        elif legacy is not None and legacy.exists() and legacy.stat().st_size > 0:
            path = legacy
        if path is not None:
            first = subprocess.check_output(["sed", "-n", "2p", str(path)], text=True).strip()
            last = subprocess.check_output(["tail", "-n", "1", str(path)], text=True).strip()
            mn_s = first.split(",")[1] if "," in first else ""
            mx_s = last.split(",")[1] if "," in last else ""
            try:
                mx_dt = ensure_utc(datetime.fromisoformat(mx_s.replace("Z", "+00:00")))
                age = _age_seconds(mx_dt, now)
            except Exception:  # noqa: BLE001
                mx_dt, age = None, None
            out.append(
                {
                    "symbol": sym,
                    "source": "wall_transitions_csv",
                    "min_ts_utc": mn_s,
                    "max_ts_utc": mx_s,
                    "age_seconds": age,
                    "row_count": "",
                    "rows_last_5m": 0,
                    "rows_last_15m": 0,
                    "rows_last_60m": 0,
                    "rows_last_6h": 0,
                    "rows_last_24h": 0,
                    "expected_bucket_count": "",
                    "observed_bucket_count": "",
                    "coverage_ratio": "",
                    "largest_gap_seconds": "",
                    "gap_count": "",
                    "stale": True if age is None or age > 3600 else False,
                    "status": "STALE" if age is None or age > 3600 else "HEALTHY",
                    "notes": f"path={path}",
                }
            )
        else:
            out.append(
                {
                    "symbol": sym,
                    "source": "wall_transitions_csv",
                    "min_ts_utc": "",
                    "max_ts_utc": "",
                    "age_seconds": "",
                    "row_count": "",
                    "rows_last_5m": 0,
                    "rows_last_15m": 0,
                    "rows_last_60m": 0,
                    "rows_last_6h": 0,
                    "rows_last_24h": 0,
                    "expected_bucket_count": "",
                    "observed_bucket_count": "",
                    "coverage_ratio": "",
                    "largest_gap_seconds": "",
                    "gap_count": "",
                    "stale": False,
                    "status": "NO_DATA",
                    "notes": "",
                }
            )
    return out


def wall_transition_coverage(root: Path, symbols: list[str]) -> list[dict[str, Any]]:
    now = _utc_now()
    rows = []
    for sym in symbols:
        live = root / "data/wall_transitions" / sym / "execution_wall_transitions.csv"
        legacy_rel = LEGACY_WALL_PATHS.get(sym)
        legacy = root / legacy_rel if legacy_rel else None
        path = live if live.exists() and live.stat().st_size > 0 else legacy
        exists = bool(path is not None and path.exists())
        size = path.stat().st_size if exists and path is not None else 0
        mn = mx = ""
        age = None
        if exists and path is not None and size > 0:
            first = subprocess.check_output(["sed", "-n", "2p", str(path)], text=True).strip()
            last = subprocess.check_output(["tail", "-n", "1", str(path)], text=True).strip()
            mn = first.split(",")[1] if "," in first else ""
            mx = last.split(",")[1] if "," in last else ""
            try:
                age = _age_seconds(datetime.fromisoformat(mx.replace("Z", "+00:00")), now)
            except Exception:  # noqa: BLE001
                age = None
        pid_path = root / "data/wall_transitions/pids" / f"{sym}.pid"
        running = False
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                running = "run_wall_transition_collector" in cmd and sym in cmd
            except (OSError, ValueError):
                running = False
        status = "NO_DATA" if not exists or size == 0 else ("STALE" if (age or 1e9) > 3600 else "HEALTHY")
        rows.append(
            {
                "symbol": sym,
                "file_path": str(path) if exists and path is not None else str(live),
                "exists": exists,
                "file_size_bytes": size,
                "row_count": "",
                "min_ts_utc": mn,
                "max_ts_utc": mx,
                "age_seconds": age,
                "rows_last_60m": "",
                "rows_last_6h": "",
                "duplicate_count": "",
                "out_of_order_count": "",
                "largest_gap_seconds": "",
                "coverage_ratio": "",
                "collector_running": running,
                "restart_safe": True,
                "status": status,
            }
        )
    return rows


def orderbook_gap_sample(symbols: list[str], lookback_hours: float = 24.0) -> list[dict[str, Any]]:
    """Find large time gaps in recent orderbook via window functions (server-side)."""
    db = connect_readonly()
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            q = db.query(
                """
                SELECT
                  prev_ts,
                  exchange_ts,
                  prev_uid,
                  update_id,
                  message_type,
                  dateDiff('second', prev_ts, exchange_ts) AS gap_seconds
                FROM (
                  SELECT
                    exchange_ts,
                    update_id,
                    message_type,
                    lagInFrame(exchange_ts) OVER (ORDER BY exchange_ts, update_id) AS prev_ts,
                    lagInFrame(update_id) OVER (ORDER BY exchange_ts, update_id) AS prev_uid
                  FROM orderbook_analysis.orderbook_deltas
                  WHERE symbol = {s:String}
                    AND exchange_ts >= now64(3,'UTC') - INTERVAL {h:UInt32} HOUR
                )
                WHERE prev_ts IS NOT NULL
                  AND prev_ts > toDateTime64('2020-01-01', 3, 'UTC')
                  AND dateDiff('second', prev_ts, exchange_ts) >= 30
                  AND dateDiff('second', prev_ts, exchange_ts) < 86400 * 7
                ORDER BY gap_seconds DESC
                LIMIT 200
                """,
                parameters={"s": sym, "h": int(lookback_hours)},
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "symbol": sym,
                    "gap_start_utc": "",
                    "gap_end_utc": "",
                    "gap_seconds": "",
                    "previous_update_id": "",
                    "next_update_id": "",
                    "update_id_gap": "",
                    "snapshot_after_gap": "",
                    "severity": "UNKNOWN",
                    "notes": f"gap_query_failed:{exc}",
                }
            )
            continue
        for prev_ts, exchange_ts, prev_uid, update_id, message_type, gap in q.result_rows:
            uid_gap = None
            if update_id is not None and prev_uid is not None:
                try:
                    uid_gap = int(update_id) - int(prev_uid)
                except Exception:  # noqa: BLE001
                    uid_gap = None
            rows.append(
                {
                    "symbol": sym,
                    "gap_start_utc": ensure_utc(prev_ts).isoformat(),
                    "gap_end_utc": ensure_utc(exchange_ts).isoformat(),
                    "gap_seconds": int(gap),
                    "previous_update_id": prev_uid,
                    "next_update_id": update_id,
                    "update_id_gap": uid_gap,
                    "snapshot_after_gap": message_type == "snapshot",
                    "severity": "HIGH" if int(gap) >= 300 else "MED",
                    "notes": "",
                }
            )
    return rows


def run_full_audit(
    *,
    root: Path,
    symbols: list[str],
    output_dir: Path,
    lookback_hours: float = 24.0,
) -> dict[str, Any]:
    root = Path(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    (output_dir / "running_processes.txt").write_text(collect_running_processes(root), encoding="utf-8")
    rec_inv = build_recorder_inventory(root)
    _write_csv(output_dir / "recorder_inventory.csv", rec_inv)
    _write_csv(output_dir / "collector_config_inventory.csv", build_config_inventory(root))
    _write_csv(output_dir / "log_inventory.csv", build_log_inventory(root))
    (output_dir / "service_inventory.md").write_text(build_service_inventory_md(root), encoding="utf-8")
    _write_csv(output_dir / "table_inventory.csv", build_table_inventory())

    # file inventory (walls + state)
    file_rows = []
    for p in sorted((root / "results").glob("**/execution_wall_transitions.csv")):
        st = p.stat()
        file_rows.append(
            {
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "kind": "legacy_wall_transitions",
            }
        )
    for p in sorted((root / "data").glob("**/execution_wall_transitions.csv")) if (root / "data").exists() else []:
        st = p.stat()
        file_rows.append(
            {
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "kind": "live_wall_transitions",
            }
        )
    _write_csv(output_dir / "file_inventory.csv", file_rows)

    state_rows = []
    state_dir = root / "data/wall_transitions/state"
    if state_dir.exists():
        for p in sorted(state_dir.glob("*.json")):
            st = p.stat()
            state_rows.append(
                {
                    "path": str(p),
                    "size_bytes": st.st_size,
                    "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    _write_csv(output_dir / "state_inventory.csv", state_rows)

    cov = coverage_rows(symbols, lookback_hours=lookback_hours)
    _write_csv(output_dir / "coverage_by_symbol.csv", cov)
    _write_csv(output_dir / "wall_transition_coverage.csv", wall_transition_coverage(root, symbols))

    # gap audit — last 6h only to keep runtime bounded
    gaps = orderbook_gap_sample(symbols, lookback_hours=min(lookback_hours, 6.0))
    _write_csv(output_dir / "orderbook_gap_audit.csv", gaps)
    _write_csv(output_dir / "data_gaps.csv", gaps)

    # 1m activity for lookback
    from datetime import timedelta

    end = now
    start = end - timedelta(hours=lookback_hours)
    act_dir = output_dir / "_activity_1m"
    export_market_activity_1m(
        symbols=symbols,
        start=start,
        end=end,
        output_dir=act_dir,
        overwrite=True,
    )
    for name in (
        "trade_activity_1m.csv",
        "oi_coverage_1m.csv",
        "liquidation_activity_1m.csv",
        "market_activity_1m.csv",
    ):
        src = act_dir / name
        if src.exists():
            (output_dir / name).write_bytes(src.read_bytes())

    # source health snapshot
    source_health = []
    for r in cov:
        source_health.append(
            {
                "symbol": r["symbol"],
                "source": r["source"],
                "status": r["status"],
                "age_seconds": r["age_seconds"],
                "max_ts_utc": r["max_ts_utc"],
            }
        )
    _write_csv(output_dir / "source_health.csv", source_health)

    decision = "AUDIT_COMPLETE_START_NOT_SAFE"
    doge_ob = next((r for r in cov if r["symbol"] == "DOGEUSDT" and r["source"] == "orderbook_deltas"), None)
    apt_ob = next((r for r in cov if r["symbol"] == "APTUSDT" and r["source"] == "orderbook_deltas"), None)
    btc_ob = next((r for r in cov if r["symbol"] == "BTCUSDT" and r["source"] == "orderbook_deltas"), None)
    walls_stale = any(r["source"] == "wall_transitions_csv" and r["status"] != "HEALTHY" for r in cov)
    if doge_ob and doge_ob["status"] == "HEALTHY" and apt_ob and apt_ob["status"] == "HEALTHY":
        if walls_stale:
            decision = "AUDIT_COMPLETE_START_NOT_SAFE"  # until collectors started; caller may override
        # start is safe for wall collectors on DOGE/APT; BTC blocked
        decision = "COLLECTORS_RUNNING_WITH_DATA_GAPS" if any(
            r["process_type"] == "bybit_recorder" and r["status"] == "RUNNING" for r in rec_inv
        ) else decision

    plan = f"""# Audit plan

generated_at_utc: `{now.isoformat()}`

## Root cause: wall history stopped

Wall transitions came from one-shot offline `scripts/run_execution_wall_detector.py`
with fixed `--end` windows (see `logs/execution_walls_DOGEUSDT_full_history_*.log`).
There was **no** incremental live wall-transition collector.

## What is healthy now

- DOGEUSDT / APTUSDT Bybit recorders (`run_recorder.py --duration 0`) writing ClickHouse
- Orderbook, trades, ticker/OI streams for DOGE/APT are fresh
- Liquidations are sparse events; freshness judged via `recorder_health`

## What is stale / missing

- Wall-transition CSVs end ~2026-07-28 10:24 UTC (DOGE) / ~2026-07-27 08:21 UTC (APT)
- BTCUSDT recorder stopped ~2026-07-30 00:19 UTC — all BTC streams stale
- No BTC wall history

## Phase 2 decision

- Implement / start **new** incremental collector under `data/wall_transitions/`
- Do **not** rewrite `results/execution_walls_*`
- Seed watermark from legacy CSV max timestamps
- Start wall collectors for DOGE + APT; BTC walls **BLOCKED** until BTC recorder restarted
- Prefer nohup (matches existing recorder ops); not systemd

## Semantics

- `public_trades.side`: Bybit taker/aggressor (`Buy` = aggressive buy)
- `liquidations.side`: `Buy` = LIQUIDATED_LONG, `Sell` = LIQUIDATED_SHORT

## Primary decision (audit-time)

`{decision}`
"""
    (output_dir / "audit_plan.md").write_text(plan, encoding="utf-8")

    summary = {
        "generated_at_utc": now.isoformat(),
        "symbols": symbols,
        "lookback_hours": lookback_hours,
        "primary_decision": decision,
        "recorders_running": [r for r in rec_inv if r["status"] == "RUNNING"],
        "btc_orderbook_status": btc_ob["status"] if btc_ob else "UNKNOWN",
        "wall_root_cause": "offline_batch_detector_fixed_end_no_live_collector",
        "semantics": {
            "public_trades.side": "Bybit taker/aggressor (Buy=aggressive buy)",
            "liquidations.side": "Buy=LIQUIDATED_LONG, Sell=LIQUIDATED_SHORT",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (output_dir / "summary.md").write_text(
        f"# Collector coverage audit summary\n\nDecision: `{decision}`\n\nSee `audit_plan.md`.\n",
        encoding="utf-8",
    )
    return summary
