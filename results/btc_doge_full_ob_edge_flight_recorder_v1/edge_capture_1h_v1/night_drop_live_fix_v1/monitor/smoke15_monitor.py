#!/usr/bin/env python3
"""15-minute live smoke sampler for night-drop fix. Read-only vs collector."""
from __future__ import annotations
import json, time, os, signal
from pathlib import Path
from datetime import datetime, timezone

OUT = Path(__file__).resolve().parents[1]
SMOKE = OUT / "smoke"
HEALTH = Path("/home/telgenbuescher/projects/orderbook_analyse/logs/orderbook_v3_raw_archive_btc_doge.health.ndjson")
PID_FILE = OUT / "analysis" / "PHASE_C_RESTART.json"
DURATION = 15 * 60
INTERVAL = 20

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def last_health():
    if not HEALTH.exists():
        return None
    last = None
    size = HEALTH.stat().st_size
    with open(HEALTH, "rb") as f:
        f.seek(max(0, size - 2_000_000))
        for line in f:
            if b"BATCH_STREAMING" not in line and b"process_lifetime_queue_drops" not in line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("writer_mode") == "BATCH_STREAMING_ZSTD":
                last = j
    return last

def extract(j):
    if not j:
        return None
    rt = j.get("runtimes") or {}
    fbr = {x.get("symbol"): x for x in (j.get("full_book_runtimes") or []) if isinstance(x, dict)}
    return {
        "ts": utc(),
        "uptime": j.get("uptime_seconds"),
        "rss_mb": j.get("rss_mb"),
        "reconnects": j.get("reconnects_total"),
        "seq_gaps": j.get("sequence_gaps_total"),
        "full_book_active_topics": j.get("full_book_active_topics"),
        "process_lifetime_queue_drops": j.get("process_lifetime_queue_drops"),
        "symbol_lifetime_queue_drops": j.get("symbol_lifetime_queue_drops"),
        "current_event_queue_drops": j.get("current_event_queue_drops"),
        "queue_drop_count": j.get("queue_drop_count"),
        "writer_error_count": j.get("writer_error_count"),
        "current_writer_alive": j.get("current_writer_alive"),
        "queue_backlog": j.get("queue_backlog_items") or j.get("writer_backlog"),
        "queue_hwm": j.get("queue_high_watermark"),
        "bootstrap_obs": j.get("bootstrap_observation_count"),
        "signal_count": j.get("signal_count"),
        "symbols": {
            s: {
                "life": (rt.get(s) or {}).get("lifecycle"),
                "capturing": (rt.get(s) or {}).get("capturing"),
                "cont": (rt.get(s) or {}).get("continuation_index"),
                "prebuf": (rt.get(s) or {}).get("prebuffer_coverage_seconds"),
                "buf": (rt.get(s) or {}).get("buffer_messages"),
                "drops": (rt.get(s) or {}).get("queue_drops"),
                "sym_life_drops": (rt.get(s) or {}).get("symbol_lifetime_queue_drops"),
                "w_alive": (rt.get(s) or {}).get("writer_alive"),
                "werr": (rt.get(s) or {}).get("writer_error_count"),
                "ext": (rt.get(s) or {}).get("extension_count"),
                "src_gap": (rt.get(s) or {}).get("source_feed_u_gap_count"),
                "pers_gap": (rt.get(s) or {}).get("persisted_capture_u_gap_count"),
                "book_ready": (fbr.get(s) or {}).get("book_ready"),
                "u": (fbr.get(s) or {}).get("update_id"),
                "seq": (fbr.get(s) or {}).get("seq"),
                "gap": (fbr.get(s) or {}).get("gap_count"),
                "reconn": (fbr.get(s) or {}).get("reconnect_count"),
            }
            for s in ("BTCUSDT", "DOGEUSDT")
        },
    }

def main():
    SMOKE.mkdir(parents=True, exist_ok=True)
    meta = json.loads(PID_FILE.read_text()) if PID_FILE.exists() else {}
    pid = meta.get("new_pid")
    start = time.time()
    samples = []
    fail = []
    (SMOKE / "SMOKE15_STARTED.json").write_text(json.dumps({"ts": utc(), "pid": pid, "duration_sec": DURATION}, indent=2) + "\n")
    while time.time() - start < DURATION:
        alive = pid and Path(f"/proc/{pid}").exists()
        row = extract(last_health())
        if row:
            row["pid_alive"] = bool(alive)
            samples.append(row)
            # gates
            if row.get("process_lifetime_queue_drops", 0) not in (0, None):
                fail.append({"ts": utc(), "reason": "lifetime_drops", "v": row["process_lifetime_queue_drops"]})
            if row.get("writer_error_count", 0) not in (0, None):
                fail.append({"ts": utc(), "reason": "writer_error", "v": row["writer_error_count"]})
            if row.get("full_book_active_topics") not in (2, None):
                fail.append({"ts": utc(), "reason": "topics", "v": row.get("full_book_active_topics")})
            for s, v in (row.get("symbols") or {}).items():
                if v.get("book_ready") is False:
                    fail.append({"ts": utc(), "reason": "book_not_ready", "symbol": s})
                if (v.get("gap") or 0) > 0:
                    fail.append({"ts": utc(), "reason": "source_gap", "symbol": s, "v": v.get("gap")})
        with open(SMOKE / "SMOKE15_SAMPLES.jsonl", "a") as fh:
            if row:
                fh.write(json.dumps(row) + "\n")
        time.sleep(INTERVAL)
    # final
    last = samples[-1] if samples else None
    prebufs = [ (last or {}).get("symbols", {}).get(s, {}).get("prebuf") or 0 for s in ("BTCUSDT","DOGEUSDT") ]
    verdict = {
        "ts": utc(),
        "pid": pid,
        "n_samples": len(samples),
        "failures": fail[:50],
        "n_failures": len(fail),
        "last": last,
        "prebuffer_near_600": all(p >= 500 for p in prebufs) if last else False,
        "lifetime_drops_zero": (last or {}).get("process_lifetime_queue_drops") == 0,
        "writer_errors_zero": (last or {}).get("writer_error_count") == 0,
        "topics_2": (last or {}).get("full_book_active_topics") == 2,
        "pass": len(fail) == 0 and last is not None and (last.get("process_lifetime_queue_drops") == 0) and (last.get("writer_error_count") == 0),
    }
    (SMOKE / "SMOKE15_RESULT.json").write_text(json.dumps(verdict, indent=2) + "\n")

if __name__ == "__main__":
    main()
