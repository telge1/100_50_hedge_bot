"""Tests for case-scoped Full-OB raw archive."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_case_archive import FullObCaseArchiveHub
from orderbook_analyse.orderbook_v2_live.on_demand_full import FullBookOnDemandManager, FullBookRuntime


def _mgr(tmp_path: Path) -> FullBookOnDemandManager:
    settings = {
        "enabled": True,
        "max_active_topics": 4,
        "heartbeat_sec": 15.0,
        "lease_ttl_sec": 60.0,
        "pilot_symbols": frozenset({"BTCUSDT", "DOGEUSDT"}),
        "clip_pct": 2.0,
        "max_ui_bars": 40,
        "rest_url": "https://api.bybit.com/v5/market/full_orderbook",
    }
    mgr = FullBookOnDemandManager(
        market="linear",
        send_chunk=lambda _c: None,
        confirmed_topics=[],
        settings=settings,
    )
    mgr.case_archive.default_archive_root = tmp_path
    return mgr


def test_case_archive_unknown_symbol_no_static_allowlist(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    sym = "NEWCOINUSDT"
    rt = mgr._get_runtime(sym)
    rt.book.apply_snapshot(
        bids=[["1.0", "10"]],
        asks=[["1.1", "10"]],
        u=1,
        seq=1,
        ts_ms=1,
        mark_ready=True,
    )
    resp = mgr.case_archive.start_case_archive(
        symbol=sym,
        case_id="c1",
        experiment_id="e1",
        archive_root=tmp_path,
    )
    assert resp["ok"] is True
    assert resp["archive_started"] is True
    assert resp["archive_ready"] is True
    assert resp["snapshot_written"] is True
    assert resp["archive_format_version"]
    assert resp["refcount"] >= 1

    # static keepers still registered
    assert mgr.case_archive._refcount["BTCUSDT"].static_keeper is True

    mgr._notify_observers(
        symbol=sym,
        payload={
            "topic": f"orderbook.1.{sym}",
            "type": "delta",
            "ts": 2,
            "data": {"s": sym, "u": 2, "seq": 2, "b": [["1.0", "9"]], "a": []},
        },
        received_at=datetime.now(timezone.utc),
        receive_time_ns=time.time_ns(),
        phase="live",
        outcome="applied",
    )
    time.sleep(0.05)
    fin = mgr.case_archive.finalize_case_archive(resp["recorder_id"])
    assert fin["ok"] is True
    assert fin["archive_path"]
    assert Path(fin["archive_path"]).is_file()
    assert Path(fin["manifest_path"]).is_file()
    # releasing case must not clear static keeper
    assert mgr.case_archive._refcount["BTCUSDT"].static_keeper is True
    mgr.close()


def test_refcount_case_does_not_stop_static(tmp_path: Path) -> None:
    hub = FullObCaseArchiveHub(default_archive_root=tmp_path)
    hub.register_static_keeper("BTCUSDT")
    hub.register_static_keeper("DOGEUSDT")
    assert hub._refcount["BTCUSDT"].count == 1
    # fake manager for attach optional
    class M:
        runtimes = {}
        def add_observer(self, cb):
            pass
    hub.attach(M())
    r = hub.start_case_archive(
        symbol="BTCUSDT",
        case_id="c2",
        experiment_id="e",
        archive_root=tmp_path,
    )
    assert r["ok"]
    assert hub._refcount["BTCUSDT"].count >= 2
    hub.finalize_case_archive(r["recorder_id"])
    assert hub._refcount["BTCUSDT"].static_keeper is True
    assert hub._refcount["BTCUSDT"].count >= 1


def test_continuous_format_no_200_truncation(tmp_path: Path) -> None:
    """Archive must retain >200 levels and use continuous format version."""
    from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import FORMAT_VERSION
    from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import iter_records

    mgr = _mgr(tmp_path)
    sym = "DEPTHUSDT"
    rt = mgr._get_runtime(sym)
    bids = [[str(99.0 - i * 0.01), "1"] for i in range(250)]
    asks = [[str(100.0 + i * 0.01), "1"] for i in range(250)]
    rt.book.apply_snapshot(bids=bids, asks=asks, u=1, seq=1, ts_ms=1, mark_ready=True)
    resp = mgr.case_archive.start_case_archive(
        symbol=sym, case_id="depth1", experiment_id="e", archive_root=tmp_path
    )
    assert resp["ok"]
    assert resp.get("archive_format_version") == FORMAT_VERSION == "full_ob_continuous_raw_archive_v1"
    fin = mgr.case_archive.finalize_case_archive(resp["recorder_id"])
    assert fin["ok"]
    recs = list(iter_records(Path(fin["archive_path"])))
    ckpt = next(r for r in recs if r.get("message_type") == "checkpoint")
    op = ckpt["original_payload"]
    assert len(op["bids"]) >= 250
    assert len(op["asks"]) >= 250
    mgr.close()
