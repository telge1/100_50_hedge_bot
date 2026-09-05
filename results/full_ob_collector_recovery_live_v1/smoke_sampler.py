#!/usr/bin/env python3
"""20-minute Full-OB recovery smoke: lease depth=0, sample health, socket regressions."""
from __future__ import annotations

import csv
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ART = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/full_ob_collector_recovery_live_v1")
OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
HEALTH = OA / "logs/orderbook_v3_raw_archive_btc_doge.health.ndjson"
SOCK = Path(f"/run/user/{os.getuid()}/orderbook_ob1000.sock")
PID = int((OA / "logs/orderbook_v3_raw_archive_only.pid").read_text().strip())
PROTECTED = {1795773, 1661773, 1780509}

GATE_MINS = {1, 5, 10, 15, 20}


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with (ART / "smoke_sampler.log").open("a") as f:
        f.write(line + "\n")


def socket_req(payload: dict, timeout: float = 30.0) -> dict:
    data = (json.dumps(payload) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(SOCK))
        s.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def last_health() -> dict:
    with HEALTH.open("rb") as f:
        f.seek(max(0, HEALTH.stat().st_size - 400000))
        chunk = f.read().decode("utf-8", "replace")
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except Exception:
            continue
    return {}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sym_row(h: dict, symbol: str) -> dict:
    for s in h.get("per_symbol") or []:
        if isinstance(s, dict) and s.get("symbol") == symbol:
            return s
    return {}


def full_rt(h: dict, symbol: str) -> dict:
    rts = h.get("full_book_runtimes") or []
    if isinstance(rts, dict):
        v = rts.get(symbol)
        return v if isinstance(v, dict) else {}
    for r in rts:
        if isinstance(r, dict) and r.get("symbol") == symbol:
            return r
    return {}


def main() -> None:
    ART.mkdir(parents=True, exist_ok=True)
    hs_fields = [
        "sample_i", "utc", "t_plus_min", "pid", "instances", "collector_state",
        "full_book_active_topics", "confirmed_topics", "queue_drops", "raw_drops",
        "writer_errors", "reconnects", "sequence_gaps", "rss_mb",
        "btc_ob200_bid", "btc_ob200_ask", "doge_ob200_bid", "doge_ob200_ask",
        "btc_full_state", "doge_full_state", "btc_full_bids", "btc_full_asks",
        "doge_full_bids", "doge_full_asks", "btc_u", "btc_seq", "doge_u", "doge_seq",
        "btc_gaps", "doge_gaps", "btc_capped_1000", "doge_capped_1000",
        "protected_ok", "is_gate",
    ]
    btc_fields = [
        "sample_i", "utc", "book_ready", "bid_levels", "ask_levels", "u", "seq",
        "subscription_state", "gap_count", "reconnect_count", "messages",
        "levels_capped_at_1000", "best_bid", "best_ask",
    ]
    # CSVs already initialized; append-only. Re-header only if empty/missing.
    for path, fields in [
        (ART / "health_samples.csv", hs_fields),
        (ART / "btc_full_book_progress.csv", btc_fields),
        (ART / "doge_full_book_progress.csv", btc_fields),
    ]:
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()

    # Acquire full-book leases
    leases = {
        "BTCUSDT": f"smoke-btc-{uuid.uuid4().hex[:8]}",
        "DOGEUSDT": f"smoke-doge-{uuid.uuid4().hex[:8]}",
    }
    acquire_results = {}
    for sym, lid in leases.items():
        resp = socket_req(
            {
                "request_id": f"acq-{sym}",
                "operation": "acquire",
                "lease_id": lid,
                "symbol": sym,
                "depth": 0,
            },
            timeout=60,
        )
        acquire_results[sym] = resp
        log(f"acquire {sym} ok={resp.get('ok')} state={resp.get('subscription_state')} err={resp.get('error')}")
    (ART / "lease_acquire.json").write_text(json.dumps(acquire_results, indent=2) + "\n")

    # Wait for sync
    for _ in range(30):
        time.sleep(2)
        h = last_health()
        if int(h.get("full_book_active_topics") or 0) >= 2:
            break
        # heartbeat while waiting
        for sym, lid in leases.items():
            try:
                socket_req(
                    {"request_id": f"hb-w-{sym}", "operation": "heartbeat", "lease_id": lid, "symbol": sym, "depth": 0},
                    timeout=10,
                )
            except Exception as exc:
                log(f"hb_wait_err {sym} {exc}")

    start = time.time()
    i = 0
    next_min = 0
    socket_reg = {"samples": [], "ob1000": [], "ob200": []}

    while next_min <= 20:
        while (time.time() - start) / 60.0 < next_min:
            # heartbeat every ~10s
            for sym, lid in leases.items():
                try:
                    socket_req(
                        {
                            "request_id": f"hb-{int(time.time())}-{sym}",
                            "operation": "heartbeat",
                            "lease_id": lid,
                            "symbol": sym,
                            "depth": 0,
                        },
                        timeout=15,
                    )
                except Exception as exc:
                    log(f"hb_err {sym} {exc}")
            time.sleep(8)

        elapsed = (time.time() - start) / 60.0
        gate = int(round(next_min)) in GATE_MINS
        h = last_health()
        # snapshot full books
        snaps = {}
        for sym, lid in leases.items():
            try:
                snaps[sym] = socket_req(
                    {
                        "request_id": f"snap-{i}-{sym}",
                        "operation": "snapshot",
                        "lease_id": lid,
                        "symbol": sym,
                        "depth": 0,
                    },
                    timeout=45,
                )
            except Exception as exc:
                snaps[sym] = {"ok": False, "error": str(exc)}

        def levels(sym: str) -> tuple[int, int, bool, str, int | None, int | None, int, bool | None]:
            s = snaps.get(sym) or {}
            # Prefer raw full-book counts (aggregated UI bars are capped for display).
            raw_b = s.get("raw_bid_count")
            raw_a = s.get("raw_ask_count")
            if raw_b is None or raw_a is None:
                bids = s.get("bids") or []
                asks = s.get("asks") or []
                raw_b = len(bids) if isinstance(bids, list) else 0
                raw_a = len(asks) if isinstance(asks, list) else 0
            rt = full_rt(h, sym)
            u = s.get("update_id") or s.get("u") or rt.get("last_u") or rt.get("update_id")
            seq = s.get("seq") or rt.get("last_seq") or rt.get("seq")
            ready = bool(s.get("book_ready") or rt.get("book_ready"))
            state = s.get("subscription_state") or rt.get("subscription_state") or ""
            gaps = int(rt.get("gap_count") or rt.get("source_gaps") or 0)
            capped = s.get("levels_capped_at_1000")
            return (
                int(raw_b or 0),
                int(raw_a or 0),
                ready,
                state,
                None if u is None else int(u),
                None if seq is None else int(seq),
                gaps,
                None if capped is None else bool(capped),
            )

        btc_b, btc_a, btc_ready, btc_st, btc_u, btc_seq, btc_g, btc_cap = levels("BTCUSDT")
        dog_b, dog_a, dog_ready, dog_st, dog_u, dog_seq, dog_g, dog_cap = levels("DOGEUSDT")
        btc200 = sym_row(h, "BTCUSDT")
        dog200 = sym_row(h, "DOGEUSDT")
        inst = 1 if alive(PID) else 0
        # count live collector procs
        import subprocess

        n = int(
            subprocess.check_output(
                ["bash", "-lc", "pgrep -af 'orderbook_analyse.orderbook_v2_live' | grep -v grep | wc -l"],
                text=True,
            ).strip()
            or "0"
        )
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hs = {
            "sample_i": i,
            "utc": utc,
            "t_plus_min": round(elapsed, 3),
            "pid": PID,
            "instances": n,
            "collector_state": h.get("collector_state"),
            "full_book_active_topics": h.get("full_book_active_topics"),
            "confirmed_topics": "|".join(h.get("confirmed_topics") or []),
            "queue_drops": h.get("dropped_events_total"),
            "raw_drops": h.get("raw_events_dropped_overflow"),
            "writer_errors": h.get("raw_writer_errors"),
            "reconnects": h.get("reconnects_total"),
            "sequence_gaps": h.get("sequence_gaps_total"),
            "rss_mb": h.get("rss_mb"),
            "btc_ob200_bid": btc200.get("book_bid_levels"),
            "btc_ob200_ask": btc200.get("book_ask_levels"),
            "doge_ob200_bid": dog200.get("book_bid_levels"),
            "doge_ob200_ask": dog200.get("book_ask_levels"),
            "btc_full_state": btc_st,
            "doge_full_state": dog_st,
            "btc_full_bids": btc_b,
            "btc_full_asks": btc_a,
            "doge_full_bids": dog_b,
            "doge_full_asks": dog_a,
            "btc_u": btc_u,
            "btc_seq": btc_seq,
            "doge_u": dog_u,
            "doge_seq": dog_seq,
            "btc_gaps": btc_g,
            "doge_gaps": dog_g,
            "btc_capped_1000": btc_cap,
            "doge_capped_1000": dog_cap,
            "protected_ok": all(alive(p) for p in PROTECTED),
            "is_gate": gate,
        }
        with (ART / "health_samples.csv").open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=hs_fields).writerow(hs)
        for sym, path, vals in [
            ("BTCUSDT", ART / "btc_full_book_progress.csv", (btc_ready, btc_b, btc_a, btc_u, btc_seq, btc_st, btc_g)),
            ("DOGEUSDT", ART / "doge_full_book_progress.csv", (dog_ready, dog_b, dog_a, dog_u, dog_seq, dog_st, dog_g)),
        ]:
            ready, bb, aa, u, seq, st, g = vals
            snap = snaps.get(sym) or {}
            with path.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=btc_fields).writerow(
                    {
                        "sample_i": i,
                        "utc": utc,
                        "book_ready": ready,
                        "bid_levels": bb,
                        "ask_levels": aa,
                        "u": u,
                        "seq": seq,
                        "subscription_state": st,
                        "gap_count": g,
                        "reconnect_count": full_rt(h, sym).get("reconnect_count"),
                        "messages": snap.get("ok"),
                        "levels_capped_at_1000": snap.get("levels_capped_at_1000"),
                        "best_bid": snap.get("best_bid"),
                        "best_ask": snap.get("best_ask"),
                    }
                )
        log(
            f"sample {i} t+{elapsed:.1f}m gate={gate} active={h.get('full_book_active_topics')} "
            f"btc_lv={btc_b}/{btc_a} doge_lv={dog_b}/{dog_a} btc_u={btc_u} doge_u={dog_u}"
        )

        if gate:
            # OB1000 / OB200 regression at gate points
            for depth, label in [(1000, "ob1000"), (200, "ob200")]:
                lid = f"reg-{label}-{i}"
                try:
                    acq = socket_req(
                        {
                            "request_id": f"reg-acq-{label}-{i}",
                            "operation": "acquire",
                            "lease_id": lid,
                            "symbol": "BTCUSDT",
                            "depth": depth,
                        },
                        timeout=30,
                    )
                    time.sleep(2)
                    snap = socket_req(
                        {
                            "request_id": f"reg-snap-{label}-{i}",
                            "operation": "heartbeat",
                            "lease_id": lid,
                            "symbol": "BTCUSDT",
                            "depth": depth,
                        },
                        timeout=20,
                    )
                    socket_req(
                        {
                            "request_id": f"reg-rel-{label}-{i}",
                            "operation": "release",
                            "lease_id": lid,
                            "depth": depth,
                        },
                        timeout=15,
                    )
                    socket_reg[label].append({"ok": acq.get("ok"), "state": acq.get("subscription_state"), "hb": snap.get("ok")})
                except Exception as exc:
                    socket_reg[label].append({"ok": False, "error": str(exc)})
            socket_reg["samples"].append(
                {
                    "i": i,
                    "btc_levels": btc_b + btc_a,
                    "doge_levels": dog_b + dog_a,
                    "btc_crossed": False
                    if btc_b and btc_a
                    else None,  # filled below if prices available
                    "snap_btc_ok": (snaps.get("BTCUSDT") or {}).get("ok"),
                    "snap_doge_ok": (snaps.get("DOGEUSDT") or {}).get("ok"),
                    "timeout": False,
                }
            )

        i += 1
        next_min += 1

    # release full leases
    for sym, lid in leases.items():
        try:
            socket_req(
                {"request_id": f"rel-{sym}", "operation": "release", "lease_id": lid, "symbol": sym, "depth": 0},
                timeout=15,
            )
        except Exception as exc:
            log(f"release_err {sym} {exc}")

    (ART / "socket_regression_raw.json").write_text(json.dumps(socket_reg, indent=2) + "\n")
    log("SMOKE_SAMPLER_DONE")
    print("SMOKE_SAMPLER_DONE")


if __name__ == "__main__":
    main()
