"""Full-OB 10k/10k depth archive + replay roundtrip (no truncation)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import FORMAT_VERSION
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import iter_records
from orderbook_analyse.orderbook_v2_live.on_demand_full import FullBookOnDemandManager


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


@pytest.mark.parametrize("tick", [0.000001, 0.001, 0.1, 1.0])
def test_full_depth_10k_archive_replay_roundtrip(tmp_path: Path, tick: float) -> None:
    mgr = _mgr(tmp_path)
    sym = f"D{str(tick).replace('.', 'p')}USDT"[:12].upper()
    rt = mgr._get_runtime(sym)
    n = 10_000
    # Keep all bid prices strictly positive and book uncrossed.
    mid = float((n + 10) * tick)
    def _px(x: float) -> str:
        # Avoid scientific notation / float noise for small ticks
        from decimal import Decimal

        return format(Decimal(str(x)), "f")

    bids = [[_px(mid - (i + 1) * tick), "1"] for i in range(n)]
    asks = [[_px(mid + (i + 1) * tick), "1"] for i in range(n)]
    assert float(bids[-1][0]) > 0
    assert float(bids[0][0]) < float(asks[0][0])
    rt.book.apply_snapshot(bids=bids, asks=asks, u=1, seq=1, ts_ms=1, mark_ready=True)
    assert len(rt.book.bids) >= n
    assert len(rt.book.asks) >= n

    resp = mgr.case_archive.start_case_archive(
        symbol=sym, case_id=f"depth10k-{tick}", experiment_id="e", archive_root=tmp_path
    )
    assert resp["ok"] is True
    assert resp.get("archive_format_version") == FORMAT_VERSION
    assert resp.get("snapshot_written") is True

    # Delta at level ~5000 and ~10000 (outside first 1000)
    idx_5k = 4999
    idx_10k = 9999
    bid_5k = bids[idx_5k][0]
    ask_10k = asks[idx_10k][0]
    far_bid_add = _px(mid - (n + 5) * tick)
    far_ask_add = _px(mid + (n + 5) * tick)
    assert float(far_bid_add) > 0
    payload = {
        "topic": f"orderbook.full.{sym}",
        "type": "delta",
        "ts": 2,
        "cts": 2,
        "data": {
            "s": sym,
            "u": 2,
            "seq": 2,
            "b": [[bid_5k, "0"], [far_bid_add, "3"]],
            "a": [[ask_10k, "7"], [far_ask_add, "4"]],
        },
    }
    rt.book.apply_delta(
        bids=payload["data"]["b"],
        asks=payload["data"]["a"],
        u=2,
        seq=2,
        ts_ms=2,
        enforce_continuity=False,
    )
    mgr._notify_observers(
        symbol=sym,
        payload=payload,
        received_at=datetime.now(timezone.utc),
        receive_time_ns=time.time_ns(),
        phase="live",
        outcome="applied",
    )
    time.sleep(0.05)
    fin = mgr.case_archive.finalize_case_archive(resp["recorder_id"])
    assert fin["ok"] and fin["archive_path"]
    recs = list(iter_records(Path(fin["archive_path"])))
    ckpt = next(r for r in recs if r.get("message_type") == "checkpoint")
    op = ckpt["original_payload"]
    assert len(op["bids"]) >= n
    assert len(op["asks"]) >= n
    assert len(op["bids"]) > 1000 and len(op["asks"]) > 1000

    deltas = [r for r in recs if r.get("message_type") == "delta"]
    assert deltas
    d0 = deltas[0]["original_payload"]["data"]
    b_prices = {str(row[0]) for row in d0.get("b") or []}
    a_prices = {str(row[0]) for row in d0.get("a") or []}
    assert bid_5k in b_prices
    assert ask_10k in a_prices
    assert far_bid_add in b_prices
    assert far_ask_add in a_prices
    assert deltas[0].get("u") == 2
    assert deltas[0].get("seq") == 2
    mgr.close()
