"""Throughput + bootstrap semantics for Full-OB edge flight recorder."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import zstandard as zstd

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import NonBlockingDeltaSink
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
    new_event_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.record_envelope import (
    build_delta_envelope,
    level_update_count,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import replay_event_directory
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import (
    EdgeLevel,
    EdgeWatcher,
    SymbolLifecycle,
)

RECOVERY_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/"
    "btc_doge_full_ob_edge_flight_recorder_v1/edge_capture_1h_v1/legacy_recovery"
)
BTC_RECOVERED = RECOVERY_ROOT / "BTCUSDT_20260903T184212Z_4a22a89fe6" / "recovered_deltas.jsonl.zst"
DOGE_RECOVERED = RECOVERY_ROOT / "DOGEUSDT_20260903T184212Z_f5d68293cd" / "recovered_deltas.jsonl.zst"

# Phase-0 measured peak ~17 u/s/symbol during live sampling; use conservative peak pack rate.
MEASURED_PEAK_MSG_PER_SEC_PER_SYMBOL = 20.0


def _load_delta_templates(path: Path, limit: int = 2500) -> list[dict]:
    dctx = zstd.ZstdDecompressor()
    out: list[dict] = []
    with open(path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while len(out) < limit:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf and len(out) < limit:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    out.append(json.loads(line))
    assert out, f"no templates from {path}"
    return out


@pytest.fixture(scope="module")
def real_templates():
    if not BTC_RECOVERED.exists() or not DOGE_RECOVERED.exists():
        pytest.skip("legacy recovery deltas unavailable")
    return {
        "BTCUSDT": _load_delta_templates(BTC_RECOVERED, 3000),
        "DOGEUSDT": _load_delta_templates(DOGE_RECOVERED, 3000),
    }


def _settings(tmp_path: Path, **over) -> FlightRecorderSettings:
    kw = dict(
        enabled=True,
        symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
        capture_root=tmp_path,
        ringbuffer_minutes=10.0,
        minimum_post_capture_minutes=60.0,
        reclaim_post_capture_minutes=10.0,
        maximum_event_minutes=180.0,
        extension_minutes=30.0,
        segment_minutes=30.0,
        cooldown_minutes=5.0,
        profile_poll_sec=9999,
        queue_size=16384,
        writer_batch_max_messages=64,
        writer_batch_max_bytes=256 * 1024,
        writer_flush_interval_sec=0.25,
        min_free_disk_gb=0.0,
        warn_free_disk_gb=0.0,
        max_parallel_events=2,
    )
    kw.update(over)
    return FlightRecorderSettings(**kw)


def _edges(cutoff, *, vah=100.0, val=90.0):
    return (
        EdgeLevel("TPO_VAH", vah, "p1", cutoff),
        EdgeLevel("TPO_VAL", val, "p1", cutoff),
    )


def _fake_full(symbols=("BTCUSDT", "DOGEUSDT")):
    class Book:
        def __init__(self, sym):
            self.symbol = sym
            self.book_ready = True
            self.update_id = 50
            self.seq = 500
            self.last_receive_time_ns = 1_000_000_000
            self.gap_count = 0
            self.reconnect_count = 0

        def mid(self):
            return 100.05 if self.symbol == "BTCUSDT" else 0.089

    class RT:
        def __init__(self, sym):
            self.book = Book(sym)
            self.gap_count = 0
            self.reconnect_count = 0
            self.last_rest_snapshot = {
                "s": sym,
                "b": [["100", "1"], ["99", "1"]],
                "a": [["101", "1"], ["102", "1"]],
                "u": 50,
                "seq": 500,
                "ts": 1000,
                "cts": 999,
            }

    class Full:
        def __init__(self):
            self.runtimes = {s: RT(s) for s in symbols}

        def add_observer(self, cb):
            self.cb = cb

        def _acquire(self, **kwargs):
            return None, True

        def _release(self, lease_id):
            return None, True

    return Full()


def test_one_queue_item_per_bybit_delta_packet():
    payload = {
        "topic": "orderbook.1.BTCUSDT",
        "type": "delta",
        "ts": 123,
        "cts": 122,
        "data": {
            "s": "BTCUSDT",
            "b": [["100", "1"], ["99", "2"], ["98", "3"]],
            "a": [["101", "1"], ["102", "4"]],
            "u": 7,
            "seq": 70,
        },
    }
    env = build_delta_envelope(payload, receive_time_ns=999, phase="live", outcome="applied")
    assert env["data"]["u"] == 7
    assert env["data"]["seq"] == 70
    assert env["ts"] == 123
    assert env["cts"] == 122
    assert env["local_receive_time_ns"] == 999
    assert env["level_update_count"] == 5
    assert level_update_count(env) == 5
    # Must not embed full book dumps.
    assert "bids" not in env
    assert "asks" not in env
    assert "full_bids" not in env


def test_bootstrap_upper_no_persistent_file(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, symbols=frozenset({"BTCUSDT"})), full_book_manager=_fake_full(("BTCUSDT",)))
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    fr.watcher.set_edges("BTCUSDT", _edges(t0), {"profile_id": "p1", "cutoff": t0.isoformat()})
    d = fr.watcher.evaluate("BTCUSDT", 99.9, now=t0)  # inside upper zone
    assert d.action == "bootstrap_observe"
    fr._note_bootstrap_observation("BTCUSDT", d, t0)
    assert "BTCUSDT" not in fr._writers
    assert list(tmp_path.rglob("*.tmp")) == []
    assert fr.bootstrap_observation_count == 1
    assert fr.signal_count == 0
    fr.shutdown()


def test_bootstrap_lower_no_persistent_file(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, symbols=frozenset({"BTCUSDT"})), full_book_manager=_fake_full(("BTCUSDT",)))
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    fr.watcher.set_edges("BTCUSDT", _edges(t0), {"profile_id": "p1"})
    d = fr.watcher.evaluate("BTCUSDT", 90.05, now=t0)  # inside lower zone
    assert d.action == "bootstrap_observe"
    fr.tick = lambda *a, **k: None
    fr._note_bootstrap_observation("BTCUSDT", d, t0)
    assert "BTCUSDT" not in fr._writers
    assert list(tmp_path.rglob("full_ob_raw_deltas*")) == []
    fr.shutdown()


def _bootstrap_exit_rearm_cross(fr: FullObEdgeFlightRecorder, symbol: str, *, cross_mid: float, t0: datetime):
    cutoff = t0 - timedelta(minutes=5)
    fr.watcher.set_edges(symbol, _edges(cutoff), {"profile_id": "p1", "cutoff": cutoff.isoformat(), "session_start": cutoff.isoformat()})
    d0 = fr.watcher.evaluate(symbol, cross_mid, now=t0)
    assert d0.action == "bootstrap_observe"
    fr._note_bootstrap_observation(symbol, d0, t0)
    assert symbol not in fr._writers
    # Exit far outside.
    fr.watcher.evaluate(symbol, 80.0, now=t0 + timedelta(seconds=1))
    assert fr.watcher.state(symbol).saw_outside is True
    # Approach + CROSS_IN
    fr.watcher.evaluate(symbol, 99.6, now=t0 + timedelta(seconds=2))
    d = fr.watcher.evaluate(symbol, cross_mid, now=t0 + timedelta(seconds=3))
    assert d.action == "trigger"
    assert d.trigger_source == "CROSS_IN"
    assert d.edge_entry_crossed is True
    fr._start_or_merge_event(symbol, d, t0 + timedelta(seconds=3), book_ready=True)
    assert symbol in fr._writers
    assert fr.signal_count == 1
    assert fr.bootstrap_observation_count >= 1
    return t0 + timedelta(seconds=3)


def test_bootstrap_exit_rearm_upper_cross_in_one_event(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, symbols=frozenset({"BTCUSDT"})), full_book_manager=_fake_full(("BTCUSDT",)))
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _bootstrap_exit_rearm_cross(fr, "BTCUSDT", cross_mid=99.9, t0=t0)
    assert fr._plans["BTCUSDT"].trigger_quality == "REAL_CROSS_IN"
    assert fr._plans["BTCUSDT"].bootstrap_persistent_capture is False
    assert fr._plans["BTCUSDT"].research_eligible is True
    files = list(tmp_path.rglob("full_ob_raw_deltas.jsonl.zst.tmp"))
    assert len(files) == 1
    fr.shutdown()


def test_bootstrap_exit_rearm_lower_cross_in_one_event(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, symbols=frozenset({"BTCUSDT"})), full_book_manager=_fake_full(("BTCUSDT",)))
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _bootstrap_exit_rearm_cross(fr, "BTCUSDT", cross_mid=90.05, t0=t0)
    assert fr._plans["BTCUSDT"].edge_type == "LOWER"
    fr.shutdown()


def test_real_trigger_after_warmup_has_prebuffer(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, symbols=frozenset({"BTCUSDT"})), full_book_manager=_fake_full(("BTCUSDT",)))
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    fr._started_at = t0 - timedelta(seconds=800)
    base_ns = int((t0 - timedelta(seconds=650)).timestamp() * 1e9)
    for i in range(30):
        fr.on_full_ob_message(
            symbol="BTCUSDT",
            payload={
                "type": "delta",
                "ts": int((t0 - timedelta(seconds=650 - i)).timestamp() * 1000),
                "data": {"u": 10 + i, "seq": 100 + i, "b": [["100", "1"]], "a": [["101", "1"]]},
            },
            received_at=t0,
            receive_time_ns=base_ns + i * 1_000_000_000,
            phase="live",
            outcome="applied",
        )
    assert fr._buffers["BTCUSDT"].coverage_seconds(int(t0.timestamp() * 1e9)) >= 600 - 5
    # outside then cross
    fr.watcher.set_edges("BTCUSDT", _edges(t0 - timedelta(minutes=5)), {"profile_id": "p1", "cutoff": t0.isoformat()})
    fr.watcher.evaluate("BTCUSDT", 80.0, now=t0)
    fr.watcher.evaluate("BTCUSDT", 99.6, now=t0 + timedelta(seconds=1))
    d = fr.watcher.evaluate("BTCUSDT", 99.9, now=t0 + timedelta(seconds=2))
    fr._start_or_merge_event("BTCUSDT", d, t0 + timedelta(seconds=2), book_ready=True)
    assert fr._plans["BTCUSDT"].pre_trigger_seconds_actual >= 600 - 2
    assert fr._plans["BTCUSDT"].first_persisted_ts is not None
    fr.shutdown()


def test_no_signal_no_persistent_full_ob_file(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake_full())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    fr.watcher.set_edges("BTCUSDT", _edges(t0), {"profile_id": "p1"})
    fr.watcher.set_edges("DOGEUSDT", _edges(t0, vah=0.1, val=0.08), {"profile_id": "p2"})
    for i in range(50):
        for sym in ("BTCUSDT", "DOGEUSDT"):
            fr.on_full_ob_message(
                symbol=sym,
                payload={"type": "delta", "ts": i, "data": {"u": i, "seq": i, "b": [], "a": []}},
                received_at=t0,
                receive_time_ns=i,
                phase="live",
            )
        # far from edges
        fr.watcher.evaluate("BTCUSDT", 150.0, now=t0 + timedelta(seconds=i))
        fr.watcher.evaluate("DOGEUSDT", 0.2, now=t0 + timedelta(seconds=i))
    assert fr.signal_count == 0
    assert list(tmp_path.rglob("full_ob_raw_deltas*")) == []
    fr.shutdown()


def test_queue_drop_marks_research_ineligible(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(
        _settings(tmp_path, symbols=frozenset({"BTCUSDT"}), queue_size=2),
        full_book_manager=_fake_full(("BTCUSDT",)),
    )
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _bootstrap_exit_rearm_cross(fr, "BTCUSDT", cross_mid=99.9, t0=t0)
    sink = fr._sinks["BTCUSDT"]
    # Stop writer thread processing by replacing with blocked sink path: fill queue.
    sink.stop()
    # Re-install a tiny stopped-like full queue via new sink with blocked writer.
    gate = threading.Event()

    class Slow:
        symbol = "BTCUSDT"
        continuation_index = 0
        queue_drops = 0

        def append_delta_batch(self, records):
            gate.wait(timeout=5)
            return 0, 0

        def flush_pending(self):
            return None

        def mark_incomplete(self, reason):
            pass

    blocked = NonBlockingDeltaSink(Slow(), queue_size=1)
    fr._sinks["BTCUSDT"] = blocked
    assert blocked.try_put({"type": "delta", "data": {"u": 1, "seq": 1, "b": [], "a": []}, "local_receive_time_ns": 1})
    # Wait until worker has taken the first item (queue empty) or it's sitting in queue.
    time.sleep(0.05)
    assert blocked.try_put({"type": "delta", "data": {"u": 2, "seq": 2, "b": [], "a": []}, "local_receive_time_ns": 2})
    # Third must drop.
    fr.on_full_ob_message(
        symbol="BTCUSDT",
        payload={"type": "delta", "data": {"u": 3, "seq": 3, "b": [["1", "1"]], "a": []}},
        received_at=t0,
        receive_time_ns=3,
        phase="live",
    )
    assert fr._plans["BTCUSDT"].research_eligible is False
    assert "QUEUE_DROP" in fr._plans["BTCUSDT"].incomplete_reasons
    gate.set()
    blocked.stop()
    fr.shutdown()


def test_multi_symbol_order_preserved(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake_full())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    for sym, mid in (("BTCUSDT", 99.9), ("DOGEUSDT", 90.05)):
        # independent cross-ins
        fr.watcher.set_edges(sym, _edges(t0), {"profile_id": "p1", "cutoff": t0.isoformat()})
        fr.watcher.evaluate(sym, 80.0, now=t0)
        fr.watcher.evaluate(sym, 99.6 if sym == "BTCUSDT" else 90.5, now=t0 + timedelta(seconds=1))
        d = fr.watcher.evaluate(sym, mid, now=t0 + timedelta(seconds=2))
        if d.action != "trigger":
            # DOGE edges use same 100/90; use BTC-like path for DOGE with matching mid on VAL
            if sym == "DOGEUSDT":
                d = fr.watcher.evaluate(sym, 90.05, now=t0 + timedelta(seconds=3))
        if d.action == "trigger":
            fr._start_or_merge_event(sym, d, t0 + timedelta(seconds=3), book_ready=True)
    # Force both open with explicit decisions if needed.
    for sym in ("BTCUSDT", "DOGEUSDT"):
        if sym not in fr._writers:
            edge = _edges(t0)[0] if sym == "BTCUSDT" else _edges(t0)[1]
            d = type(
                "D",
                (),
                {
                    "reason": "cross_in",
                    "edge": edge,
                    "sample": type("S", (), {"mid": 99.9, "distance_bps": 5.0})(),
                    "trigger_source": "CROSS_IN",
                    "edge_entry_crossed": True,
                    "bootstrap_status": "N/A",
                    "prior_zone_state": "OUT",
                    "trigger_zone_state": "IN",
                },
            )()
            fr._start_or_merge_event(sym, d, t0 + timedelta(seconds=4), book_ready=True)
    for i in range(100):
        for sym in ("BTCUSDT", "DOGEUSDT"):
            fr.on_full_ob_message(
                symbol=sym,
                payload={
                    "type": "delta",
                    "ts": 1000 + i,
                    "data": {"u": 100 + i, "seq": 1000 + i, "b": [[str(100 + i), "1"]], "a": []},
                },
                received_at=t0,
                receive_time_ns=i,
                phase="live",
                outcome="applied",
            )
    deadline = time.time() + 5
    while time.time() < deadline and any(s.backlog for s in fr._sinks.values()):
        time.sleep(0.01)
    for sink in fr._sinks.values():
        sink.stop()
    for sym in ("BTCUSDT", "DOGEUSDT"):
        fr._finalize_event(sym, t0 + timedelta(seconds=10), "COMPLETE_MIN_POST_ELAPSED")
        root = next(tmp_path.rglob(f"{sym}_*/manifest.json")).parent
        # cont may exist; find event root with deltas
        roots = [p.parent for p in tmp_path.rglob("full_ob_raw_deltas.jsonl.zst") if sym in str(p)]
        assert roots
        # verify u monotonic in file
        path = roots[0] / "full_ob_raw_deltas.jsonl.zst"
        if not path.exists():
            path = roots[0] / "cont_001" / "full_ob_raw_deltas.jsonl.zst"
        # Prefer primary segment if present after finalize rename
        cands = list(tmp_path.rglob("full_ob_raw_deltas.jsonl.zst"))
        cands = [c for c in cands if sym in str(c)]
        assert cands
        us = []
        dctx = zstd.ZstdDecompressor()
        data = dctx.decompress(cands[0].read_bytes(), max_output_size=50_000_000)
        for line in data.splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            kind = rec.get("record_kind")
            if kind in {
                "INITIAL_CHECKPOINT",
                "RESYNC_CHECKPOINT",
                "RESYNC_BOUNDARY",
                "EVENT_MARKER",
                "EVENT_END",
            }:
                continue
            if rec.get("channel") in {"marker", "full_ob_continuity"} and kind != "BOOK_DELTA":
                continue
            if (rec.get("data") or {}).get("u") is None:
                continue
            us.append(int(rec["data"]["u"]))
        assert us == sorted(us)
        assert us == list(range(us[0], us[0] + len(us)))


def _run_load(tmp_path: Path, templates: dict, *, rate_mult: float, duration_sec: float, segment_minutes: float = 30.0):
    settings = _settings(tmp_path, segment_minutes=segment_minutes, max_open_tmp_bytes=8 * 1024 * 1024)
    fake = _fake_full()
    # REST snapshot continuity: u just before first synthetic delta (1000).
    for sym in ("BTCUSDT", "DOGEUSDT"):
        tmpl0 = templates[sym][0]
        data0 = tmpl0.get("data") or {}
        bids = list(data0.get("b") or [["100", "1"]])[:20]
        asks = list(data0.get("a") or [["101", "1"]])[:20]
        if not bids:
            bids = [["100", "1"]]
        if not asks:
            asks = [["101", "1"]]
        # Ensure non-crossed seed book.
        try:
            bb = float(bids[0][0])
            ba = float(asks[0][0])
            if ba <= bb:
                asks = [[str(bb + 1.0), "1"]]
        except Exception:
            bids, asks = [["100", "1"]], [["101", "1"]]
        fake.runtimes[sym].last_rest_snapshot = {
            "s": sym,
            "b": bids,
            "a": asks,
            "u": 999,
            "seq": 9999,
            "ts": 1,
            "cts": 1,
        }
        fake.runtimes[sym].book.update_id = 999
        fake.runtimes[sym].book.seq = 9999
    fr = FullObEdgeFlightRecorder(settings, full_book_manager=fake)
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    # Open both symbols with real CROSS_IN decisions.
    for sym, edge in (("BTCUSDT", _edges(t0)[0]), ("DOGEUSDT", _edges(t0)[1])):
        d = type(
            "D",
            (),
            {
                "reason": "cross_in",
                "edge": edge,
                "sample": type("S", (), {"mid": 99.9 if sym == "BTCUSDT" else 90.05, "distance_bps": 5.0})(),
                "trigger_source": "CROSS_IN",
                "edge_entry_crossed": True,
                "bootstrap_status": "N/A",
                "prior_zone_state": "OUT",
                "trigger_zone_state": "IN",
            },
        )()
        fr.watcher.set_edges(sym, _edges(t0), {"profile_id": "p1", "cutoff": t0.isoformat()})
        fr.watcher.state(sym).saw_outside = True
        fr._start_or_merge_event(sym, d, t0, book_ready=True)

    target_rate = MEASURED_PEAK_MSG_PER_SEC_PER_SYMBOL * rate_mult
    interval = 1.0 / target_rate
    sent = {"BTCUSDT": 0, "DOGEUSDT": 0}
    levels_sent = {"BTCUSDT": 0, "DOGEUSDT": 0}
    idx = {"BTCUSDT": 0, "DOGEUSDT": 0}
    start = time.perf_counter()
    next_t = {"BTCUSDT": start, "DOGEUSDT": start}
    # Burst: send dual-symbol as fast as schedule allows.
    while time.perf_counter() - start < duration_sec:
        now = time.perf_counter()
        for sym in ("BTCUSDT", "DOGEUSDT"):
            if now < next_t[sym]:
                continue
            tmpl = templates[sym][idx[sym] % len(templates[sym])]
            idx[sym] += 1
            # Remap u/seq to be continuous for this run.
            payload = {
                "topic": tmpl.get("topic") or f"orderbook.1.{sym}",
                "type": "delta",
                "ts": int(now * 1000),
                "cts": int(now * 1000) - 1,
                "data": {
                    "s": sym,
                    "b": (tmpl.get("data") or {}).get("b") or [],
                    "a": (tmpl.get("data") or {}).get("a") or [],
                    "u": 1000 + sent[sym],
                    "seq": 10_000 + sent[sym],
                },
            }
            fr.on_full_ob_message(
                symbol=sym,
                payload=payload,
                received_at=t0,
                receive_time_ns=time.time_ns(),
                phase="live",
                outcome="applied",
            )
            sent[sym] += 1
            levels_sent[sym] += level_update_count(payload)
            next_t[sym] += interval
        # Force segment rotation mid-run once for higher multiples.
        if rate_mult >= 2.0 and (time.perf_counter() - start) > duration_sec * 0.4:
            for sym, w in list(fr._writers.items()):
                if w.continuation_index == 0:
                    w.started_at = t0 - timedelta(minutes=31)
                    fr._maybe_rotate_segment(sym, datetime.now(timezone.utc))
        time.sleep(0.0005)

    # Drain
    deadline = time.time() + 30
    while time.time() < deadline and any(s.backlog for s in fr._sinks.values()):
        time.sleep(0.01)
    health = fr.health_dict()
    for sink in list(fr._sinks.values()):
        sink.stop()
    for sym in ("BTCUSDT", "DOGEUSDT"):
        fr._finalize_event(sym, datetime.now(timezone.utc), "COMPLETE_MIN_POST_ELAPSED", outcome_status="RESOLVED")

    result = {
        "rate_mult": rate_mult,
        "target_msg_per_sec_per_symbol": target_rate,
        "sent": sent,
        "levels_sent": levels_sent,
        "queue_drop_count": health["queue_drop_count"],
        "writer_messages_total": health["writer_messages_total"],
        "ingress_messages_total": health["ingress_messages_total"],
        "health": {k: health[k] for k in (
            "ingress_messages_per_second",
            "ingress_level_updates_per_second",
            "writer_messages_per_second",
            "writer_bytes_per_second",
            "queue_high_watermark",
            "queue_drop_count",
            "writer_batch_size",
            "writer_flush_count",
        )},
        "symbols": {},
    }
    for sym in ("BTCUSDT", "DOGEUSDT"):
        files = [p for p in tmp_path.rglob("full_ob_raw_deltas.jsonl.zst") if sym in str(p)]
        assert files, f"missing finalized deltas for {sym}"
        # Concatenate all segments for count
        n_msgs = 0
        n_levels = 0
        last_u = None
        gaps = 0
        dctx = zstd.ZstdDecompressor()
        for fp in sorted(files):
            raw = dctx.decompress(fp.read_bytes(), max_output_size=200_000_000)
            for line in raw.splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("channel") == "marker":
                    continue
                # Continuity control records (INITIAL/RESYNC checkpoints, boundaries) are not deltas.
                kind = rec.get("record_kind")
                if kind in {
                    "INITIAL_CHECKPOINT",
                    "RESYNC_CHECKPOINT",
                    "RESYNC_BOUNDARY",
                    "EVENT_MARKER",
                    "EVENT_END",
                }:
                    continue
                if rec.get("channel") == "full_ob_continuity" and kind != "BOOK_DELTA":
                    continue
                n_msgs += 1
                n_levels += int(rec.get("level_update_count") or level_update_count(rec))
                u = int((rec.get("data") or {}).get("u"))
                if last_u is not None and u > last_u + 1:
                    gaps += 1
                last_u = u
        # Replay first segment directory (event root)
        event_roots = sorted({p.parent if p.parent.name.startswith("cont_") else p.parent for p in files})
        # Prefer top-level event dir
        roots = []
        for p in files:
            root = p.parent
            if root.name.startswith("cont_"):
                root = root.parent
            roots.append(root)
        root = sorted(set(roots))[0]
        replay = replay_event_directory(root)
        # Replay may be incomplete across segmented files depending on helper root selection;
        # require no crossed book when ok, otherwise still require count/order integrity above.
        result["symbols"][sym] = {
            "persisted_messages": n_msgs,
            "persisted_levels": n_levels,
            "u_gap_count": gaps,
            "replay_ok": bool(replay.get("ok")),
            "book_crossed": bool(replay.get("crossed")),
            "replay_status": replay.get("status"),
            "replay_detail": {k: replay.get(k) for k in ("applied_deltas", "u_gap_count", "error", "status")},
        }
    fr.shutdown()
    return result


@pytest.mark.parametrize("mult", [1.0, 2.0, 3.0])
def test_load_peak_multiples_drop_free(tmp_path: Path, real_templates, mult):
    # Duration keeps CI bounded while still exercising burst + drain.
    duration = 8.0 if mult < 3 else 6.0
    res = _run_load(tmp_path, real_templates, rate_mult=mult, duration_sec=duration)
    assert res["queue_drop_count"] == 0, res
    for sym, info in res["symbols"].items():
        assert info["persisted_messages"] == res["sent"][sym], (sym, info, res["sent"])
        assert info["persisted_levels"] == res["levels_sent"][sym], (sym, info, res["levels_sent"])
        assert info["u_gap_count"] == 0, info
        assert info["replay_ok"] is True, info
        assert info["book_crossed"] is False, info
    assert res["health"]["queue_drop_count"] == 0
    # Persist benchmark artifact for the report (best-effort).
    out = Path(
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/"
        "btc_doge_full_ob_edge_flight_recorder_v1/edge_capture_1h_v1"
    )
    out.mkdir(parents=True, exist_ok=True)
    bench_path = out / "WRITER_THROUGHPUT_BENCH.jsonl"
    with open(bench_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"mult": mult, **{k: res[k] for k in res if k != "health"}, "health": res["health"]}) + "\n")


def test_slow_disk_fail_closed_visible(tmp_path: Path):
    gate = threading.Event()

    class SlowDiskWriter(ActiveEventWriter):
        def append_delta_batch(self, records):
            gate.wait(timeout=5)
            return super().append_delta_batch(records)

    # Build a real writer then monkeypatch sink to slow path.
    event_id = new_event_id("BTCUSDT", datetime.now(timezone.utc))
    directory = tmp_path / event_id
    writer = SlowDiskWriter(
        event_id=event_id,
        symbol="BTCUSDT",
        directory=directory,
        started_at=datetime.now(timezone.utc),
        trigger_reason="test",
    )
    writer.open()
    sink = NonBlockingDeltaSink(writer, queue_size=2, batch_max_messages=1, flush_interval_sec=10.0)
    try:
        assert sink.try_put(build_delta_envelope(
            {"type": "delta", "ts": 1, "data": {"u": 1, "seq": 1, "b": [["1", "1"]], "a": []}},
            receive_time_ns=1,
            phase="live",
        ))
        # Fill queue while worker blocked.
        deadline = time.time() + 1
        while sink.backlog == 0 and time.time() < deadline:
            time.sleep(0.001)
        assert sink.try_put(build_delta_envelope(
            {"type": "delta", "ts": 2, "data": {"u": 2, "seq": 2, "b": [["1", "1"]], "a": []}},
            receive_time_ns=2,
            phase="live",
        ))
        dropped = sink.try_put(build_delta_envelope(
            {"type": "delta", "ts": 3, "data": {"u": 3, "seq": 3, "b": [["1", "1"]], "a": []}},
            receive_time_ns=3,
            phase="live",
        ))
        assert dropped is False
        assert sink.drops >= 1
        assert sink.dropped_price_level_updates >= 1
    finally:
        gate.set()
        sink.stop()
        writer.finalize(ended_at=datetime.now(timezone.utc), status="INCOMPLETE_QUEUE_DROP", report_md="#x\n")


def test_segment_rollover_under_burst_no_drop(tmp_path: Path, real_templates):
    res = _run_load(
        tmp_path,
        real_templates,
        rate_mult=2.0,
        duration_sec=5.0,
        segment_minutes=0.01,  # force rollover quickly via age after monkeypatch in _run_load
    )
    assert res["queue_drop_count"] == 0, res
    # At least one symbol should have continuation segments from forced rotation.
    conts = list(tmp_path.rglob("cont_*"))
    assert conts, "expected segment continuation dirs"
