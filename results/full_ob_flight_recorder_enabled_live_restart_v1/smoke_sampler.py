#!/usr/bin/env python3
"""12+ minute FR-enabled Full-OB live smoke."""
from __future__ import annotations

import csv
import json
import os
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ART = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/full_ob_flight_recorder_enabled_live_restart_v1")
OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
HEALTH = OA / "logs/orderbook_v3_raw_archive_btc_doge.health.ndjson"
LOG = OA / "logs/orderbook_v3_raw_archive_btc_doge.nohup.log"
SOCK = Path(f"/run/user/{os.getuid()}/orderbook_ob1000.sock")
FR_ROOT = OA / "data/orderbook_raw_shadow/full_ob_edge_flight_recorder"
PID = int((OA / "logs/orderbook_v3_raw_archive_only.pid").read_text().strip())
PROTECTED = {1795773, 1661773, 1780509}
GATE_MINS = {1, 5, 10, 12}
RESTART_TS = time.time()  # approx; refined below


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with (ART / "smoke.log").open("a") as f:
        f.write(line + "\n")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def last_health() -> dict:
    with HEALTH.open("rb") as f:
        f.seek(max(0, HEALTH.stat().st_size - 500000))
        chunk = f.read().decode("utf-8", "replace")
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except Exception:
            continue
    return {}


def socket_req(payload: dict, timeout: float = 30.0) -> dict:
    data = (json.dumps(payload) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(SOCK))
        s.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def full_rt(h: dict, symbol: str) -> dict:
    for r in h.get("full_book_runtimes") or []:
        if isinstance(r, dict) and r.get("symbol") == symbol:
            return r
    return {}


def fr_sym(h: dict, symbol: str) -> dict:
    # per-symbol FR block if present
    block = h.get("full_ob_fr_per_symbol") or h.get("flight_recorder_per_symbol") or {}
    if isinstance(block, dict) and symbol in block:
        return block[symbol] if isinstance(block[symbol], dict) else {}
    # sometimes list
    if isinstance(block, list):
        for x in block:
            if isinstance(x, dict) and x.get("symbol") == symbol:
                return x
    # watcher states nested
    ws = h.get("watcher_states") or h.get("edge_watcher") or {}
    if isinstance(ws, dict) and symbol in ws:
        return ws[symbol] if isinstance(ws[symbol], dict) else {}
    return {}


def coverage(h: dict, symbol: str) -> float:
    pb = h.get("prebuffer_coverage_seconds") or {}
    if isinstance(pb, dict):
        try:
            return float(pb.get(symbol) or 0)
        except Exception:
            return 0.0
    return 0.0


def new_fr_files_since(ts: float) -> list[str]:
    out = []
    if not FR_ROOT.exists():
        return out
    for p in FR_ROOT.rglob("*"):
        if p.is_file() and p.stat().st_mtime >= ts and "INCOMPLETE" not in p.name:
            # exclude pre-existing stale event dirs from 20260903
            if "20260903" in str(p):
                continue
            out.append(str(p.relative_to(FR_ROOT)))
    return out


def indexerror_in_log_since(start_size: int) -> int:
    data = LOG.read_bytes()[start_size:]
    text = data.decode("utf-8", "replace")
    return text.count("IndexError") + text.count("_evict_if_needed")


def main() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    log_start_size = LOG.stat().st_size if LOG.exists() else 0
    start = time.time()

    hs_fields = [
        "sample_i", "utc", "t_plus_min", "pid", "instances", "collector_state",
        "fr_enabled", "full_book_active_topics", "confirmed_topics",
        "queue_drops", "writer_errors", "observer_errors", "reconnects", "sequence_gaps",
        "rss_mb", "btc_bids", "btc_asks", "doge_bids", "doge_asks",
        "btc_u", "btc_seq", "doge_u", "doge_seq", "btc_gaps", "doge_gaps",
        "btc_ready", "doge_ready", "btc_cov", "doge_cov",
        "btc_capped", "doge_capped", "signal_count", "bootstrap_obs",
        "protected_ok", "is_gate", "indexerrors",
    ]
    rb_fields = [
        "sample_i", "utc", "t_plus_min", "btc_coverage_s", "doge_coverage_s",
        "btc_buf_msgs", "doge_buf_msgs", "ingress_messages_total", "ringbuffer_growing",
    ]
    res_fields = ["sample_i", "utc", "t_plus_min", "rss_mb", "cpu_pct", "mem_avail_gb", "disk_free_gb", "projected_daily_bytes"]

    for path, fields in [
        (ART / "health_samples.csv", hs_fields),
        (ART / "ringbuffer_progress.csv", rb_fields),
        (ART / "resource_usage.csv", res_fields),
    ]:
        with path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()

    socket_reg = {"samples": [], "ob1000": [], "ob200": [], "timeouts": 0}
    watcher_samples = []
    i = 0
    next_min = 0.0

    while next_min <= 12.0:
        while (time.time() - start) / 60.0 < next_min:
            time.sleep(2)

        elapsed = (time.time() - start) / 60.0
        gate = int(round(next_min)) in GATE_MINS and abs(elapsed - round(next_min)) < 0.6 or int(round(elapsed)) in GATE_MINS and abs(elapsed - int(round(next_min))) < 0.35
        # simpler: gate if target minute matches GATE_MINS
        gate = int(round(next_min)) in GATE_MINS

        h = last_health()
        snaps = {}
        for sym in ("BTCUSDT", "DOGEUSDT"):
            try:
                snaps[sym] = socket_req(
                    {
                        "request_id": f"snap-{i}-{sym}",
                        "operation": "snapshot",
                        "lease_id": f"fr-smoke-{sym}",
                        "symbol": sym,
                        "depth": 0,
                    },
                    timeout=45,
                )
            except Exception as exc:
                socket_reg["timeouts"] += 1
                snaps[sym] = {"ok": False, "error": str(exc)}

        def levels(sym: str):
            s = snaps.get(sym) or {}
            rt = full_rt(h, sym)
            return (
                int(s.get("raw_bid_count") or rt.get("raw_bids") or 0),
                int(s.get("raw_ask_count") or rt.get("raw_asks") or 0),
                bool(s.get("book_ready") or rt.get("book_ready")),
                None if s.get("update_id") is None and rt.get("update_id") is None else int(s.get("update_id") or rt.get("update_id")),
                None if s.get("seq") is None and rt.get("seq") is None else int(s.get("seq") or rt.get("seq")),
                int(rt.get("gap_count") or 0),
                bool(s.get("levels_capped_at_1000")) if "levels_capped_at_1000" in s else False,
                s.get("best_bid"),
                s.get("best_ask"),
            )

        btc = levels("BTCUSDT")
        dog = levels("DOGEUSDT")
        n_inst = int(
            subprocess.check_output(
                ["bash", "-lc", "pgrep -af 'orderbook_analyse.orderbook_v2_live' | grep -v grep | wc -l"],
                text=True,
            ).strip()
            or "0"
        )
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        btc_cov = coverage(h, "BTCUSDT")
        dog_cov = coverage(h, "DOGEUSDT")
        idxerr = indexerror_in_log_since(log_start_size)

        # ringbuffer msg counts if exposed
        per = h.get("full_ob_fr_symbols") or h.get("per_symbol_fr") or {}
        btc_msgs = None
        dog_msgs = None
        if isinstance(per, dict):
            btc_msgs = (per.get("BTCUSDT") or {}).get("ringbuffer_messages") if isinstance(per.get("BTCUSDT"), dict) else None
            dog_msgs = (per.get("DOGEUSDT") or {}).get("ringbuffer_messages") if isinstance(per.get("DOGEUSDT"), dict) else None

        hs = {
            "sample_i": i,
            "utc": utc,
            "t_plus_min": round(elapsed, 3),
            "pid": PID,
            "instances": n_inst,
            "collector_state": h.get("collector_state"),
            "fr_enabled": h.get("full_ob_flight_recorder_enabled"),
            "full_book_active_topics": h.get("full_book_active_topics"),
            "confirmed_topics": "|".join(h.get("confirmed_topics") or []),
            "queue_drops": h.get("writer_queue_drops") or h.get("queue_drop_count") or h.get("dropped_events_total") or 0,
            "writer_errors": h.get("writer_error_count") or h.get("raw_writer_errors") or 0,
            "observer_errors": h.get("observer_error_count") or h.get("fr_observer_errors") or 0,
            "reconnects": h.get("reconnects_total"),
            "sequence_gaps": h.get("sequence_gaps_total"),
            "rss_mb": h.get("rss_mb"),
            "btc_bids": btc[0],
            "btc_asks": btc[1],
            "doge_bids": dog[0],
            "doge_asks": dog[1],
            "btc_u": btc[3],
            "btc_seq": btc[4],
            "doge_u": dog[3],
            "doge_seq": dog[4],
            "btc_gaps": btc[5],
            "doge_gaps": dog[5],
            "btc_ready": btc[2],
            "doge_ready": dog[2],
            "btc_cov": btc_cov,
            "doge_cov": dog_cov,
            "btc_capped": btc[6],
            "doge_capped": dog[6],
            "signal_count": h.get("GENUINE_PARENT_SIGNAL_COUNT") or h.get("signal_count"),
            "bootstrap_obs": h.get("bootstrap_observation_count"),
            "protected_ok": all(alive(p) for p in PROTECTED),
            "is_gate": gate,
            "indexerrors": idxerr,
        }
        with (ART / "health_samples.csv").open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=hs_fields).writerow(hs)

        # growing: coverage increases vs first sample tracked externally later
        with (ART / "ringbuffer_progress.csv").open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=rb_fields).writerow(
                {
                    "sample_i": i,
                    "utc": utc,
                    "t_plus_min": round(elapsed, 3),
                    "btc_coverage_s": btc_cov,
                    "doge_coverage_s": dog_cov,
                    "btc_buf_msgs": btc_msgs,
                    "doge_buf_msgs": dog_msgs,
                    "ingress_messages_total": h.get("ingress_messages_total"),
                    "ringbuffer_growing": "",
                }
            )

        # resources
        mem = Path("/proc/meminfo").read_text()
        mem_avail = None
        for line in mem.splitlines():
            if line.startswith("MemAvailable:"):
                mem_avail = round(int(line.split()[1]) / 1e6, 3)
        import shutil

        cpu = None
        try:
            # rough: /proc/pid/stat
            cpu = None
        except Exception:
            pass
        with (ART / "resource_usage.csv").open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=res_fields).writerow(
                {
                    "sample_i": i,
                    "utc": utc,
                    "t_plus_min": round(elapsed, 3),
                    "rss_mb": h.get("rss_mb"),
                    "cpu_pct": cpu,
                    "mem_avail_gb": mem_avail,
                    "disk_free_gb": round(shutil.disk_usage(str(OA)).free / 1e9, 2),
                    "projected_daily_bytes": h.get("projected_daily_bytes"),
                }
            )

        # watcher snapshot
        watcher_samples.append(
            {
                "i": i,
                "utc": utc,
                "fr_enabled": h.get("full_ob_flight_recorder_enabled"),
                "prebuffer_coverage_seconds": h.get("prebuffer_coverage_seconds"),
                "signal_count": h.get("signal_count"),
                "bootstrap_observation_count": h.get("bootstrap_observation_count"),
                "PARENT_CAPTURE_COUNT": h.get("PARENT_CAPTURE_COUNT"),
                "NESTED_PROFILE_EDGE_SIGNAL_COUNT": h.get("NESTED_PROFILE_EDGE_SIGNAL_COUNT"),
                "nested_profile_signals_enabled": h.get("nested_profile_signals_enabled"),
                "symbols": h.get("symbols"),
                "btc_rt": full_rt(h, "BTCUSDT"),
                "doge_rt": full_rt(h, "DOGEUSDT"),
                "fr_sym_btc": fr_sym(h, "BTCUSDT"),
                "fr_sym_doge": fr_sym(h, "DOGEUSDT"),
                # dump keys that look watcher-related
                "watcher_keys": {k: h.get(k) for k in h if any(x in k.lower() for x in ["watch", "profile", "lifecycle", "bootstrap", "registry", "edge"])},
            }
        )

        log(
            f"sample {i} t+{elapsed:.1f}m gate={gate} fr={h.get('full_ob_flight_recorder_enabled')} "
            f"active={h.get('full_book_active_topics')} btc={btc[0]}/{btc[1]} doge={dog[0]}/{dog[1]} "
            f"cov={btc_cov:.1f}/{dog_cov:.1f}u={btc[3]}/{dog[3]}"
        )

        if gate:
            for depth, label in ((1000, "ob1000"), (200, "ob200")):
                lid = f"reg-{label}-{i}"
                try:
                    acq = socket_req(
                        {
                            "request_id": f"acq-{label}-{i}",
                            "operation": "acquire",
                            "lease_id": lid,
                            "symbol": "BTCUSDT",
                            "depth": depth,
                        },
                        timeout=30,
                    )
                    time.sleep(1)
                    socket_req(
                        {
                            "request_id": f"hb-{label}-{i}",
                            "operation": "heartbeat",
                            "lease_id": lid,
                            "symbol": "BTCUSDT",
                            "depth": depth,
                        },
                        timeout=20,
                    )
                    socket_req(
                        {"request_id": f"rel-{label}-{i}", "operation": "release", "lease_id": lid, "depth": depth},
                        timeout=15,
                    )
                    socket_reg[label].append({"ok": acq.get("ok"), "state": acq.get("subscription_state")})
                except Exception as exc:
                    socket_reg["timeouts"] += 1
                    socket_reg[label].append({"ok": False, "error": str(exc)})
            # crossed?
            crossed = False
            for bb, ba in ((btc[7], btc[8]), (dog[7], dog[8])):
                try:
                    if bb is not None and ba is not None and float(bb) >= float(ba):
                        crossed = True
                except Exception:
                    pass
            socket_reg["samples"].append(
                {
                    "i": i,
                    "btc_levels": btc[0] + btc[1],
                    "doge_levels": dog[0] + dog[1],
                    "btc_capped": btc[6],
                    "doge_capped": dog[6],
                    "crossed": crossed,
                    "snap_ok": (snaps.get("BTCUSDT") or {}).get("ok") and (snaps.get("DOGEUSDT") or {}).get("ok"),
                }
            )

        i += 1
        next_min += 1.0

    # bootstrap audit
    new_files = new_fr_files_since(start - 5)
    bootstrap = {
        "new_fr_files_during_smoke": new_files,
        "BOOTSTRAP_FILE_CREATED": any(
            (".jsonl.zst" in f or "event" in f.lower()) and "20260903" not in f for f in new_files
        ),
        "genuine_signal_observed": False,
        "note": "bootstrap must not create persistent capture; ringbuffer growth tracked in CSV",
    }
    # if signal_count > 0 at end, mark
    h = last_health()
    if int(h.get("GENUINE_PARENT_SIGNAL_COUNT") or h.get("signal_count") or 0) > 0:
        bootstrap["genuine_signal_observed"] = True
        bootstrap["signal_count"] = h.get("GENUINE_PARENT_SIGNAL_COUNT") or h.get("signal_count")
    (ART / "bootstrap_signal_audit.json").write_text(json.dumps(bootstrap, indent=2) + "\n")
    (ART / "socket_regression.json").write_text(json.dumps(socket_reg, indent=2) + "\n")
    (ART / "watcher_profile_status.json").write_text(json.dumps({"samples": watcher_samples}, indent=2) + "\n")
    log("SMOKE_DONE")
    print("SMOKE_DONE")


if __name__ == "__main__":
    main()
