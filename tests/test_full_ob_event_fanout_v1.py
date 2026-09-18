"""Phase-2 tests: Full-OB event fanout (bounded queue, poll_events, fail-closed)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orderbook_analyse.orderbook_v2_live.full_ob_event_fanout import FullObEventFanout
from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.on_demand_full import FullBookOnDemandManager


def _payload(u: int, seq: int, *, bids=None, asks=None, ts: int = 1_700_000_000_000) -> dict:
    return {
        "topic": "orderbook.full.BTCUSDT",
        "type": "delta",
        "ts": ts,
        "cts": ts,
        "data": {
            "s": "BTCUSDT",
            "b": bids if bids is not None else [["100.0", "1.5"]],
            "a": asks if asks is not None else [["100.1", "2.0"]],
            "u": u,
            "seq": seq,
        },
    }


def _runtime(ready: bool = True) -> SimpleNamespace:
    book = FullBookState(symbol="BTCUSDT")
    book.book_ready = ready
    return SimpleNamespace(book=book, gap_count=0)


def test_observer_receives_delta_and_order():
    hub = FullObEventFanout(default_queue_size=64)
    hub.note_snapshot_ready("BTCUSDT")
    created = hub.create_subscriber(symbol="BTCUSDT", max_queue=64)
    assert created["ok"]
    sid = created["subscriber_id"]
    rt = _runtime(True)
    for i in range(1, 4):
        hub.on_full_ob_message(
            symbol="BTCUSDT",
            payload=_payload(10 + i, 100 + i),
            received_at=datetime.now(timezone.utc),
            receive_time_ns=time.time_ns(),
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    polled = hub.poll_events(subscriber_id=sid, limit=100)
    assert polled["ok"]
    events = polled["events"]
    assert len(events) >= 3
    ordinals = [e["record_ordinal"] for e in events]
    assert ordinals == sorted(ordinals)
    assert all(e.get("valid_for_analysis") for e in events)
    assert events[0]["update_id"] is not None
    assert events[0]["sequence_id"] is not None
    hub.remove_subscriber(sid)


def test_subscribers_isolated():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    a = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    b = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    rt = _runtime(True)
    hub.on_full_ob_message(
        symbol="BTCUSDT",
        payload=_payload(2, 2),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="live",
        outcome="applied",
        runtime=rt,
    )
    pa = hub.poll_events(subscriber_id=a, limit=10)
    assert pa["ok"] and len(pa["events"]) >= 1
    pb = hub.poll_events(subscriber_id=b, limit=10)
    assert pb["ok"] and len(pb["events"]) >= 1
    # draining a does not empty b before its own poll (already polled independently)
    assert pa["events"][0]["subscriber_id"] == a
    hub.remove_subscriber(a)
    hub.remove_subscriber(b)


def test_slow_subscriber_does_not_block_hotpath():
    hub = FullObEventFanout(default_queue_size=8)
    hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=8)["subscriber_id"]
    rt = _runtime(True)
    t0 = time.perf_counter()
    for i in range(50):
        hub.on_full_ob_message(
            symbol="BTCUSDT",
            payload=_payload(i + 1, i + 1),
            received_at=datetime.now(timezone.utc),
            receive_time_ns=i,
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0  # non-blocking enqueue
    st = hub.heartbeat(sid)
    assert st["ok"]
    assert st["coverage"]["overflow"] is True
    assert st["coverage"]["coverage_valid"] is False
    assert st["coverage"]["overflow_count"] > 0
    hub.remove_subscriber(sid)


def test_subscriber_exception_does_not_break_manager_notify():
    mgr = FullBookOnDemandManager(
        market="linear",
        send_chunk=lambda *_a, **_k: None,
        confirmed_topics=[],
        settings={
            "enabled": True,
            "max_active_topics": 3,
            "heartbeat_sec": 15,
            "lease_ttl_sec": 45,
            "pilot_symbols": frozenset(),
            "clip_pct": 50,
            "max_ui_bars": 50,
            "rest_url": "https://api.bybit.com/v5/market/full_orderbook",
        },
    )

    def boom(**_kwargs):
        raise RuntimeError("subscriber_boom")

    mgr.add_observer(boom)
    # Should not raise
    mgr._notify_observers(
        symbol="BTCUSDT",
        payload=_payload(1, 1),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="live",
        outcome="applied",
    )
    mgr.close()


def test_remove_idempotent_and_timeout_cleanup():
    hub = FullObEventFanout(subscriber_ttl_sec=0.05)
    hub.note_snapshot_ready("ETHUSDT")
    sid = hub.create_subscriber(symbol="ETHUSDT")["subscriber_id"]
    assert hub.remove_subscriber(sid)["removed"] is True
    assert hub.remove_subscriber(sid)["removed"] is False
    sid2 = hub.create_subscriber(symbol="ETHUSDT")["subscriber_id"]
    time.sleep(0.08)
    cleaned = hub.timeout_cleanup()
    assert sid2 in cleaned["removed"]
    assert hub.remove_subscriber(sid2)["removed"] is False


def test_shutdown_removes_all():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    s1 = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    s2 = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    hub.shutdown()
    assert hub.poll_events(subscriber_id=s1)["ok"] is False
    assert hub.poll_events(subscriber_id=s2)["ok"] is False


def test_overflow_fail_closed_no_silent_drop():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=2)["subscriber_id"]
    rt = _runtime(True)
    for i in range(10):
        hub.on_full_ob_message(
            symbol="BTCUSDT",
            payload=_payload(i + 1, i + 1),
            received_at=datetime.now(timezone.utc),
            receive_time_ns=i,
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    cov = hub.heartbeat(sid)["coverage"]
    assert cov["overflow"] is True
    assert cov["coverage_valid"] is False
    assert cov["dropped_count"] > 0
    polled = hub.poll_events(subscriber_id=sid, limit=10)
    assert any(e.get("event_type") == "overflow" or e.get("overflow") for e in polled["events"]) or cov[
        "overflow_count"
    ] > 0
    hub.remove_subscriber(sid)


def test_batch_limit_and_has_more():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=1000)["subscriber_id"]
    rt = _runtime(True)
    for i in range(20):
        hub.on_full_ob_message(
            symbol="BTCUSDT",
            payload=_payload(i + 1, i + 1, bids=[["1", "1"]]),
            received_at=datetime.now(timezone.utc),
            receive_time_ns=i,
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    p1 = hub.poll_events(subscriber_id=sid, limit=5)
    assert p1["ok"]
    assert len(p1["events"]) == 5
    assert p1["has_more"] is True
    p2 = hub.poll_events(subscriber_id=sid, limit=1000)
    assert p2["has_more"] is False
    hub.remove_subscriber(sid)


def test_buffer_phase_not_valid_for_analysis():
    hub = FullObEventFanout()
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    rt = _runtime(False)
    hub.on_full_ob_message(
        symbol="BTCUSDT",
        payload=_payload(1, 1),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="buffer",
        outcome="ok",
        runtime=rt,
    )
    ev = hub.poll_events(subscriber_id=sid, limit=10)["events"]
    assert ev
    assert all(e.get("valid_for_analysis") is False for e in ev)
    hub.remove_subscriber(sid)


def test_gap_marks_invalid_and_generation_bump():
    hub = FullObEventFanout()
    g0 = hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    rt = _runtime(True)
    hub.on_full_ob_message(
        symbol="BTCUSDT",
        payload=_payload(5, 5),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="live",
        outcome="applied",
        runtime=rt,
    )
    g1 = hub.bump_generation("BTCUSDT")
    assert g1 > g0
    hub.on_full_ob_message(
        symbol="BTCUSDT",
        payload=_payload(6, 6),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=2,
        phase="live",
        outcome="gap",
        runtime=rt,
    )
    polled = hub.poll_events(subscriber_id=sid, limit=100)
    assert polled["coverage"]["gap_seen"] is True or any(e.get("gap") for e in polled["events"])
    assert polled["coverage"]["coverage_valid"] is False
    gens = {e["snapshot_generation"] for e in polled["events"]}
    assert len(gens) >= 1
    hub.remove_subscriber(sid)


def test_cursor_fail_closed():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    bad = hub.poll_events(subscriber_id=sid, cursor=99)
    assert bad["ok"] is False
    assert bad["error"] == "cursor_ahead"
    unknown = hub.poll_events(subscriber_id="nope")
    assert unknown["ok"] is False
    hub.remove_subscriber(sid)


def test_socket_ops_via_handle_request():
    mgr = FullBookOnDemandManager(
        market="linear",
        send_chunk=lambda *_a, **_k: None,
        confirmed_topics=[],
        settings={
            "enabled": False,
            "max_active_topics": 3,
            "heartbeat_sec": 15,
            "lease_ttl_sec": 45,
            "pilot_symbols": frozenset(),
            "clip_pct": 50,
            "max_ui_bars": 50,
            "rest_url": "https://api.bybit.com/v5/market/full_orderbook",
        },
    )
    mgr.event_fanout.note_snapshot_ready("BTCUSDT")
    created = mgr.handle_request(
        {"request_id": "1", "operation": "create_subscriber", "symbol": "BTCUSDT", "max_queue": 32}
    )
    assert created["ok"] is True
    sid = created["subscriber_id"]
    # inject
    mgr.event_fanout.on_full_ob_message(
        symbol="BTCUSDT",
        payload=_payload(1, 1),
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="live",
        outcome="applied",
        runtime=_runtime(True),
    )
    polled = mgr.handle_request(
        {"request_id": "2", "operation": "poll_events", "subscriber_id": sid, "limit": 10}
    )
    assert polled["ok"] is True
    assert "coverage" in polled
    assert "events" in polled
    # unknown op still fail-closed on lease path
    unk = mgr.handle_request({"request_id": "3", "operation": "not_a_real_op", "symbol": "BTCUSDT"})
    assert unk["ok"] is False
    removed = mgr.handle_request({"request_id": "4", "operation": "remove_subscriber", "subscriber_id": sid})
    assert removed["ok"] is True
    mgr.close()


def test_concurrent_enqueue_from_threads():
    hub = FullObEventFanout(default_queue_size=5000)
    hub.note_snapshot_ready("BTCUSDT")
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=5000)["subscriber_id"]
    rt = _runtime(True)
    errors: list[BaseException] = []

    def worker(start: int) -> None:
        try:
            for i in range(start, start + 100):
                hub.on_full_ob_message(
                    symbol="BTCUSDT",
                    payload=_payload(i, i, bids=[["1", "1"]]),
                    received_at=datetime.now(timezone.utc),
                    receive_time_ns=i,
                    phase="live",
                    outcome="applied",
                    runtime=rt,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i * 1000,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    st = hub.heartbeat(sid)["coverage"]
    assert st["enqueued_count"] > 0
    hub.remove_subscriber(sid)


def test_performance_enqueue_samples_present():
    hub = FullObEventFanout()
    hub.note_snapshot_ready("BTCUSDT")
    hub.create_subscriber(symbol="BTCUSDT")
    rt = _runtime(True)
    for i in range(200):
        hub.on_full_ob_message(
            symbol="BTCUSDT",
            payload=_payload(i + 1, i + 1, bids=[["1", "1"]]),
            received_at=datetime.now(timezone.utc),
            receive_time_ns=i,
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    p50 = hub.metrics.percentile_ns(50)
    p95 = hub.metrics.percentile_ns(95)
    p99 = hub.metrics.percentile_ns(99)
    assert p50 is not None and p95 is not None and p99 is not None
    assert p50 <= p95 <= p99
    hub.shutdown()
