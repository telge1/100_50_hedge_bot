#!/usr/bin/env python3
"""20-minute public-trades live smoke after queue/spool repair."""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ART = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/public_trades_queue_repair_live_backfill_v1")
PID = int(Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/results/live_collector/collector_service.pid").read_text().split("=")[-1].strip() or Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/results/live_collector/collector_service.pid").read_text().strip())
PROTECTED = {1817696, 1795773, 1780509}
GATES = {1, 5, 10, 15, 20}
PROBE_SYMBOLS = ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def fetch() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8787/api/collector/status", timeout=10) as r:
        return json.load(r)


def ch_max_ts(symbol: str):
    import sys
    sys.path.insert(0, "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src")
    from signal_generator.db.client import ClickHouseClient
    from signal_generator.config import get_clickhouse_settings
    ch = ClickHouseClient.from_settings(get_clickhouse_settings())
    try:
        row = ch.query(
            "SELECT max(trade_ts), count() FROM orderbook_analysis.public_trades_canonical "
            "WHERE symbol={s:String} AND trade_ts >= now() - INTERVAL 5 MINUTE",
            parameters={"s": symbol},
        ).result_rows[0]
        return row[0], int(row[1])
    finally:
        ch.close()


def main() -> None:
    # resolve pid
    global PID
    raw = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/results/live_collector/collector_service.pid").read_text().strip()
    PID = int(raw.replace("pid=", "").strip())
    fields = [
        "sample_i", "utc", "t_plus_min", "pid", "instances", "state",
        "symbols_active", "queue_depth", "queue_maxsize", "queue_hwm",
        "dropped_events", "insert_failures", "rows_received", "rows_inserted",
        "lag_seconds", "writer_alive", "writer_fatal", "spool_written", "spool_replayed",
        "spool_bytes", "reconnect_count", "protected_ok", "is_gate",
    ]
    parity_fields = ["sample_i", "utc", "symbol", "source_last_ts", "db_max_ts_5m", "db_count_5m", "lag_s"]
    with (ART / "health_samples.csv").open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
    with (ART / "source_db_live_parity.csv").open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=parity_fields).writeheader()

    start = time.time()
    baseline_drops = None
    i = 0
    next_min = 0.0
    while next_min <= 20.0:
        while (time.time() - start) / 60.0 < next_min:
            time.sleep(2)
        elapsed = (time.time() - start) / 60.0
        gate = int(round(next_min)) in GATES
        st = fetch()
        m = st.get("public_trade_metrics") or {}
        if baseline_drops is None:
            baseline_drops = int(m.get("dropped_events") or 0)
        n_inst = 1 if alive(PID) else 0
        # count processes
        import subprocess
        n_inst = int(subprocess.check_output(
            ["bash", "-lc", "pgrep -af 'run_live_collector_service.py' | grep -v grep | wc -l"],
            text=True,
        ).strip() or "0")
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        syms = st.get("public_trade_symbols") or st.get("subscribed_symbols") or []
        row = {
            "sample_i": i,
            "utc": utc,
            "t_plus_min": round(elapsed, 3),
            "pid": PID,
            "instances": n_inst,
            "state": st.get("state") or st.get("collector_state"),
            "symbols_active": len(syms) if isinstance(syms, list) else st.get("subscribed_count"),
            "queue_depth": m.get("queue_depth"),
            "queue_maxsize": m.get("queue_maxsize"),
            "queue_hwm": m.get("queue_high_watermark"),
            "dropped_events": m.get("dropped_events"),
            "insert_failures": m.get("insert_failures"),
            "rows_received": m.get("rows_received"),
            "rows_inserted": m.get("rows_inserted"),
            "lag_seconds": m.get("lag_seconds"),
            "writer_alive": m.get("writer_alive"),
            "writer_fatal": m.get("writer_fatal"),
            "spool_written": m.get("spool_batches_written"),
            "spool_replayed": m.get("spool_batches_replayed"),
            "spool_bytes": m.get("spool_bytes_used"),
            "reconnect_count": m.get("reconnect_count"),
            "protected_ok": all(alive(p) for p in PROTECTED),
            "is_gate": gate,
        }
        with (ART / "health_samples.csv").open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writerow(row)
        print(
            f"{utc} t+{elapsed:.1f}m gate={gate} state={row['state']} "
            f"recv={row['rows_received']} ins={row['rows_inserted']} drops={row['dropped_events']} "
            f"lag={row['lag_seconds']} q={row['queue_depth']}/{row['queue_maxsize']}",
            flush=True,
        )
        if gate:
            for sym in PROBE_SYMBOLS:
                try:
                    db_max, db_n = ch_max_ts(sym)
                except Exception as exc:  # noqa: BLE001
                    db_max, db_n = None, -1
                    err = str(exc)
                else:
                    err = ""
                with (ART / "source_db_live_parity.csv").open("a", newline="") as f:
                    csv.DictWriter(f, fieldnames=parity_fields).writerow(
                        {
                            "sample_i": i,
                            "utc": utc,
                            "symbol": sym,
                            "source_last_ts": m.get("last_trade_event_ts"),
                            "db_max_ts_5m": db_max,
                            "db_count_5m": db_n,
                            "lag_s": m.get("lag_seconds"),
                        }
                    )
        i += 1
        next_min += 1.0

    (ART / "smoke_baseline_drops.json").write_text(
        json.dumps({"baseline_dropped_events": baseline_drops}, indent=2) + "\n"
    )
    print("SMOKE_DONE")


if __name__ == "__main__":
    main()
