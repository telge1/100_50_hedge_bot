#!/usr/bin/env python3
"""Detached read-only monitor: CROSS_IN / T+5m / segment / finalize / reconnects.
Does not stop/restart collector or modify collector files.
"""
from __future__ import annotations
import json, time, os
from pathlib import Path
from datetime import datetime, timezone

OUT = Path(__file__).resolve().parents[1]
MON = OUT / "monitor"
HEALTH = Path("/home/telgenbuescher/projects/orderbook_analyse/logs/orderbook_v3_raw_archive_btc_doge.health.ndjson")
FR_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder")
PID_FILE = OUT / "analysis" / "PHASE_C_RESTART.json"
TIMEOUT_SEC = 4 * 3600
INTERVAL = 15

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def last_health():
    last = None
    size = HEALTH.stat().st_size
    with open(HEALTH, "rb") as f:
        f.seek(max(0, size - 3_000_000))
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("writer_mode") == "BATCH_STREAMING_ZSTD":
                last = j
    return last

def list_new_events(since_mtime: float):
    evs = []
    for man in FR_ROOT.rglob("event_manifest.json"):
        try:
            if man.stat().st_mtime < since_mtime - 5:
                continue
            j = json.loads(man.read_text())
        except Exception:
            continue
        if j.get("trigger_source") == "CROSS_IN" or j.get("trigger_quality") == "REAL_CROSS_IN":
            evs.append({"path": str(man), "mtime": man.stat().st_mtime, "manifest": {k: j.get(k) for k in (
                "fight_event_id","symbol","trigger_ts","trigger_source","research_eligible","queue_drop_count",
                "extension_count","retouch_count","u_gap_count","source_feed_u_gap_count","persisted_capture_u_gap_count",
                "finalization_reason","data_quality","segment_count","pre_trigger_seconds_actual","post_trigger_seconds_actual"
            )}})
    return evs

def main():
    MON.mkdir(parents=True, exist_ok=True)
    meta = json.loads(PID_FILE.read_text())
    pid = meta.get("new_pid")
    start = time.time()
    start_mtime = start
    state = {
        "started_at": utc(),
        "pid": pid,
        "checkpoints": [],
        "events_seen": [],
        "reconnect_samples": [],
        "t5m_checks": [],
        "segment_checks": [],
        "status": "RUNNING",
    }
    (MON / "WATCHDOG_STATE.json").write_text(json.dumps(state, indent=2) + "\n")
    seen_events = set()
    last_reconn = None
    capturing_since = {}
    last_cont = {}

    while time.time() - start < TIMEOUT_SEC:
        alive = Path(f"/proc/{pid}").exists() if pid else False
        if not alive:
            state["status"] = "COLLECTOR_DEAD"
            state["ended_at"] = utc()
            break
        h = last_health()
        if h:
            rec = h.get("reconnects_total")
            if last_reconn is not None and rec is not None and rec > last_reconn:
                state["reconnect_samples"].append({
                    "ts": utc(), "reconnects_total": rec, "delta": rec - last_reconn,
                    "seq_gaps": h.get("sequence_gaps_total"),
                    "runtimes": {s: (h.get("full_book_runtimes") or []) for s in ("x",)},
                    "per_symbol_gaps": [
                        {k: x.get(k) for k in ("symbol","gap_count","reconnect_count","book_ready","update_id","seq")}
                        for x in (h.get("full_book_runtimes") or []) if isinstance(x, dict)
                    ],
                    "fr_capturing": {s: (h.get("runtimes") or {}).get(s, {}).get("capturing") for s in ("BTCUSDT","DOGEUSDT")},
                    "lifetime_drops": h.get("process_lifetime_queue_drops"),
                    "werr": h.get("writer_error_count"),
                })
            last_reconn = rec
            for s in ("BTCUSDT", "DOGEUSDT"):
                rt = (h.get("runtimes") or {}).get(s) or {}
                if rt.get("capturing"):
                    if s not in capturing_since:
                        capturing_since[s] = time.time()
                        state["checkpoints"].append({"ts": utc(), "kind": "CAPTURE_START", "symbol": s, "cont": rt.get("continuation_index"),
                            "drops": rt.get("queue_drops"), "lifetime": h.get("process_lifetime_queue_drops"), "werr": h.get("writer_error_count"),
                            "ext": rt.get("extension_count"), "writer_alive": rt.get("writer_alive")})
                    age = time.time() - capturing_since[s]
                    # T+5m
                    if age >= 300 and not any(c.get("kind")=="T+5m" and c.get("symbol")==s for c in state["t5m_checks"]):
                        state["t5m_checks"].append({"ts": utc(), "symbol": s, "age_s": age,
                            "lifetime_drops": h.get("process_lifetime_queue_drops"), "event_drops": rt.get("queue_drops"),
                            "werr": h.get("writer_error_count"), "ext": rt.get("extension_count"),
                            "cont": rt.get("continuation_index"), "writer_alive": rt.get("writer_alive"),
                            "prebuf": rt.get("prebuffer_coverage_seconds")})
                    cont = rt.get("continuation_index")
                    if s in last_cont and cont is not None and last_cont[s] is not None and cont > last_cont[s]:
                        state["segment_checks"].append({"ts": utc(), "symbol": s, "from": last_cont[s], "to": cont,
                            "lifetime_drops": h.get("process_lifetime_queue_drops"), "event_drops": rt.get("queue_drops"),
                            "werr": h.get("writer_error_count"), "writer_alive": rt.get("writer_alive")})
                    last_cont[s] = cont
                else:
                    if s in capturing_since:
                        state["checkpoints"].append({"ts": utc(), "kind": "CAPTURE_END", "symbol": s,
                            "lifetime": h.get("process_lifetime_queue_drops"), "werr": h.get("writer_error_count")})
                        capturing_since.pop(s, None)
        # manifests
        for ev in list_new_events(start_mtime):
            eid = (ev.get("manifest") or {}).get("fight_event_id") or ev["path"]
            if eid in seen_events:
                continue
            seen_events.add(eid)
            state["events_seen"].append(ev)
            state["checkpoints"].append({"ts": utc(), "kind": "EVENT_MANIFEST", "event_id": eid, "manifest": ev.get("manifest")})
            man = ev.get("manifest") or {}
            if man.get("actual_final_ts") or man.get("finalization_reason"):
                # finalized
                state["status"] = "EVENT_FINALIZED"
                state["ended_at"] = utc()
                (MON / "WATCHDOG_STATE.json").write_text(json.dumps(state, indent=2) + "\n")
                (MON / "WATCHDOG_DONE.json").write_text(json.dumps({"ts": utc(), "reason": "EVENT_FINALIZED", "event": man}, indent=2) + "\n")
                return
        # periodic persist
        state["updated_at"] = utc()
        state["alive"] = alive
        (MON / "WATCHDOG_STATE.json").write_text(json.dumps(state, indent=2) + "\n")
        time.sleep(INTERVAL)

    if state["status"] == "RUNNING":
        state["status"] = "TIMEOUT_NO_FINAL_EVENT"
        state["ended_at"] = utc()
    (MON / "WATCHDOG_STATE.json").write_text(json.dumps(state, indent=2) + "\n")
    (MON / "WATCHDOG_DONE.json").write_text(json.dumps({"ts": utc(), "reason": state["status"]}, indent=2) + "\n")

if __name__ == "__main__":
    main()
