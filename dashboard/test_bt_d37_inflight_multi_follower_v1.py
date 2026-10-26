"""BT-D37 — multiple inflight followers must not lose window payload."""

from __future__ import annotations

import threading

from pd1_landscape_viewer_v1.window_response_cache_v1 import (
    begin_inflight,
    clear_cache_for_tests,
    finish_inflight,
    store_cached_window,
    wait_inflight,
)
from test_bt_d34_window_cache_unit_v1 import _sample_payload


def test_wait_inflight_serves_all_followers_from_cache():
    clear_cache_for_tests()
    digest = "d37_multi_follower"
    payload = _sample_payload()
    is_leader, _ = begin_inflight(digest)
    assert is_leader
    store_cached_window(digest, payload)
    finish_inflight(digest, payload, None)

    # Re-use the same event object followers would share after begin_inflight(digest) as non-leader.
    _, wait_ev = begin_inflight(digest + "_follower_only")
    # Manual follower event: leader already completed; cache must satisfy all waiters.
    follower_ev = threading.Event()
    follower_ev.set()

    results: list[dict] = []
    lock = threading.Lock()

    def run():
        r = wait_inflight(digest, follower_ev)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(results) == 3
    for r in results:
        assert r["pools"][0]["pool_id"] == "p1"
