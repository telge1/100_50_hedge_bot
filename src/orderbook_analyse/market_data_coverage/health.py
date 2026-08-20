"""Market data health audit."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.dynamic_wall_detector import connect_readonly
from orderbook_analyse.wall_transition_collector.pidfile import cmdline_of, pid_alive, read_pid


def run_health_audit(
    *,
    symbols: list[str],
    lookback_minutes: int = 60,
    stale_seconds: float = 180.0,
    wall_stale_seconds: float = 3600.0,
    output_dir: Path,
    wall_output_dir: Path = Path("data/wall_transitions"),
    pid_dir: Path = Path("data/wall_transitions/pids"),
    fail_on_unhealthy: bool = False,
) -> tuple[dict[str, Any], int]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db = connect_readonly()
    now = datetime.now(timezone.utc)
    source_rows = []
    latest = []
    critical = []
    warning = []

    for sym in symbols:
        for table, tscol, kind in (
            ("orderbook_deltas", "exchange_ts", "ORDERBOOK"),
            ("public_trades", "trade_ts", "TRADES"),
            ("ticker_samples", "exchange_ts", "OI_TICKER"),
            ("liquidations", "liquidation_ts", "LIQUIDATIONS"),
            ("recorder_health", "event_ts", "RECORDER_HEALTH"),
        ):
            q = db.query(
                f"""
                SELECT max({tscol}), countIf({tscol} >= now64(3,'UTC') - INTERVAL {{lb:UInt32}} MINUTE)
                FROM orderbook_analysis.{table}
                WHERE symbol = {{s:String}}
                """,
                parameters={"s": sym, "lb": int(lookback_minutes)},
            )
            mx, cnt = q.result_rows[0]
            age = (now - mx.replace(tzinfo=timezone.utc)).total_seconds() if mx else None
            if kind == "LIQUIDATIONS":
                # sparse OK if recorder health fresh
                hq = db.query(
                    """
                    SELECT max(event_ts) FROM orderbook_analysis.recorder_health
                    WHERE symbol={s:String}
                    """,
                    parameters={"s": sym},
                )
                hmx = hq.result_rows[0][0]
                h_age = (now - hmx.replace(tzinfo=timezone.utc)).total_seconds() if hmx else 1e9
                status = "HEALTHY" if h_age < stale_seconds else ("STALE" if age and age > 86400 else "SOURCE_STALE")
                label = "LIQUIDATIONS_SOURCE_HEALTHY" if status == "HEALTHY" else "LIQUIDATIONS_SOURCE_STALE"
            else:
                status = "NO_DATA" if mx is None else ("STALE" if age is not None and age > stale_seconds else "HEALTHY")
                label = {
                    "ORDERBOOK": "ORDERBOOK_HEALTHY" if status == "HEALTHY" else "ORDERBOOK_STALE",
                    "TRADES": "TRADES_HEALTHY" if status == "HEALTHY" else "TRADES_STALE",
                    "OI_TICKER": "OI_HEALTHY" if status == "HEALTHY" else "OI_STALE",
                    "RECORDER_HEALTH": "RECORDER_HEALTHY" if status == "HEALTHY" else "RECORDER_STALE",
                }[kind]
            if status == "STALE" and kind in {"ORDERBOOK", "TRADES", "OI_TICKER", "RECORDER_HEALTH"}:
                critical.append(f"{sym}:{label}")
            elif status != "HEALTHY":
                warning.append(f"{sym}:{label}")
            source_rows.append(
                {
                    "symbol": sym,
                    "source": table,
                    "kind": kind,
                    "max_ts_utc": mx.isoformat() if mx else "",
                    "age_seconds": age,
                    "rows_lookback": cnt,
                    "status": status,
                    "label": label,
                }
            )
            latest.append({"symbol": sym, "source": table, "max_ts_utc": mx.isoformat() if mx else "", "age_seconds": age})

        # walls
        wall_csv = Path(wall_output_dir) / sym / "execution_wall_transitions.csv"
        pid_path = Path(pid_dir) / f"{sym}.pid"
        pid = read_pid(pid_path)
        running = bool(pid and pid_alive(pid) and f"wall_transition_collector:{sym}" in cmdline_of(pid))
        wall_age = None
        wall_status = "NO_DATA"
        wall_max = ""
        if wall_csv.exists() and wall_csv.stat().st_size > 0:
            import subprocess

            last = subprocess.check_output(["tail", "-n", "1", str(wall_csv)], text=True).strip()
            parts = last.split(",")
            if len(parts) > 1 and parts[1] not in {"transition_ts", ""}:
                from datetime import datetime as dt

                mxw = dt.fromisoformat(parts[1].replace("Z", "+00:00"))
                wall_max = mxw.isoformat()
                wall_age = (now - mxw).total_seconds()
                wall_status = "STALE" if wall_age > wall_stale_seconds else "HEALTHY"
        if not running:
            warning.append(f"{sym}:WALL_COLLECTOR_NOT_RUNNING")
            if wall_status == "STALE" or wall_status == "NO_DATA":
                critical.append(f"{sym}:WALLS_STALE_OR_MISSING")
        elif wall_status == "STALE":
            warning.append(f"{sym}:WALLS_CATCHUP_OR_STALE")
        elif wall_status == "NO_DATA":
            warning.append(f"{sym}:WALLS_NO_DATA")
        label = "WALLS_HEALTHY" if wall_status == "HEALTHY" and running else "WALLS_STALE"
        source_rows.append(
            {
                "symbol": sym,
                "source": "wall_transitions",
                "kind": "WALLS",
                "max_ts_utc": wall_max,
                "age_seconds": wall_age,
                "rows_lookback": "",
                "status": wall_status,
                "label": label,
                "collector_running": running,
                "pid": pid,
            }
        )

    if critical:
        decision = "CRITICAL"
        code = 2
    elif warning:
        decision = "WARNING"
        code = 1
    else:
        decision = "HEALTHY"
        code = 0

    summary = {
        "decision": decision,
        "exit_code": code,
        "generated_at_utc": now.isoformat(),
        "critical": critical,
        "warning": warning,
        "symbols": symbols,
    }
    with (output_dir / "source_health.csv").open("w", newline="", encoding="utf-8") as fh:
        keys = sorted({k for r in source_rows for k in r})
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(source_rows)
    with (output_dir / "latest_timestamps.csv").open("w", newline="", encoding="utf-8") as fh:
        keys = sorted({k for r in latest for k in r})
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(latest)
    (output_dir / "market_data_health.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (output_dir / "market_data_health.md").write_text(
        f"# Market data health\n\nDecision: `{decision}`\n\nCritical: {critical}\n\nWarning: {warning}\n",
        encoding="utf-8",
    )
    if fail_on_unhealthy and code != 0:
        return summary, code
    return summary, code
