#!/usr/bin/env python3
"""Isolated combined Full-OB + Trade fanout smoke (fake 300s + optional realtime). via nohup only."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

RUN = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
    "obfull_research_engine/runs/ema_public_trade_fanout_v1_20260918"
)
PT_SRC = Path("/home/telgenbuescher/projects/public_trades_live_fanout_v1/src")
MAIN = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
ENG = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_mp_qdh_trigger_v1/"
    "obfull_research_engine/src"
)
_FANOUT_LIVE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/"
    "src/orderbook_analyse/orderbook_v2_live"
)


def main() -> int:
    sys.path.insert(0, str(ENG))
    sys.path.insert(0, str(MAIN))
    sys.path.insert(0, str(PT_SRC))

    import importlib.util
    import orderbook_analyse.orderbook_v2_live  # noqa: F401

    def _load(name: str, filename: str):
        full = f"orderbook_analyse.orderbook_v2_live.{name}"
        if full in sys.modules:
            return sys.modules[full]
        path = _FANOUT_LIVE / filename
        spec = importlib.util.spec_from_file_location(full, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("full_ob_event_fanout", "full_ob_event_fanout.py")
    fanout_mod = sys.modules["orderbook_analyse.orderbook_v2_live.full_ob_event_fanout"]

    from signal_generator.bybit.live.public_trade_event_fanout import PublicTradeEventFanout
    from signal_generator.bybit.live.ws_public_trade import WsPublicTrade
    from obfull_research_engine.ema_trend_live_analyzer_v1.clock import FakeClock
    from obfull_research_engine.ema_trend_live_analyzer_v1.decision_evidence import CandidateFSM
    from obfull_research_engine.ema_trend_live_analyzer_v1.live_event_adapter import LiveEventAdapter
    from obfull_research_engine.ema_trend_live_analyzer_v1.live_trade_adapter import LiveTradeAdapter
    from obfull_research_engine.ema_trend_live_analyzer_v1.live_vs_ch_parity import compare_live_vs_ch
    from types import SimpleNamespace
    from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState

    symbol = "SMOKEUSDT"
    t0 = datetime(2026, 9, 18, tzinfo=timezone.utc)
    clock = FakeClock(t0)

    # --- Trade fanout ---
    trade_hub = PublicTradeEventFanout(default_queue_size=8192)
    tsub = trade_hub.create_subscriber(symbol=symbol)["subscriber_id"]
    live_trade_rows = []
    for i in range(1, 21):
        tr = WsPublicTrade(
            symbol=symbol,
            trade_id=str(i),
            trade_ts=t0.replace(microsecond=i * 1000),
            side="Buy" if i % 2 else "Sell",
            price=Decimal("100"),
            size=Decimal("0.01"),
            notional=Decimal("1"),
            tick_direction="",
            is_rpi_trade=0,
        )
        trade_hub.on_trade(tr, receive_time_ns=time.time_ns())
        live_trade_rows.append(
            {
                "trade_id": str(i),
                "side": tr.side,
                "price": "100",
                "quantity": "0.01",
                "exchange_event_time": tr.trade_ts.isoformat().replace("+00:00", "Z"),
            }
        )
    # CH control fixture (same ids)
    ch_rows = [
        {
            **r,
            "size": r["quantity"],
            "ingest_timestamp": "2026-09-18T00:00:01Z",
        }
        for r in live_trade_rows
    ]
    parity = compare_live_vs_ch(live_trade_rows, ch_rows)

    poll = trade_hub.poll_events(subscriber_id=tsub, limit=100)
    assert poll["ok"] and poll["coverage_valid"]
    tad = LiveTradeAdapter(symbol=symbol, snapshot_ready_at=t0)
    cts = tad.ingest_batch(poll["events"])
    assert len(cts) == 20
    assert all(c.collector_received_at for c in cts)

    # --- Full OB fanout ---
    book = FullBookState(symbol=symbol)
    book.book_ready = True
    rt = SimpleNamespace(book=book)
    ob_hub = fanout_mod.FullObEventFanout(default_queue_size=8192)
    ob_hub.note_snapshot_ready(symbol)
    osid = ob_hub.create_subscriber(symbol=symbol)["subscriber_id"]
    for i in range(1, 11):
        ob_hub.on_full_ob_message(
            symbol=symbol,
            payload={
                "topic": f"orderbook.full.{symbol}",
                "type": "delta",
                "ts": 1_700_000_000_000 + i,
                "data": {
                    "s": symbol,
                    "u": i,
                    "seq": i,
                    "b": [["99.0", "1"]],
                    "a": [["100.0", str(10 - i * 0.1)]],
                },
            },
            received_at=datetime.now(timezone.utc),
            receive_time_ns=time.time_ns(),
            phase="live",
            outcome="applied",
            runtime=rt,
        )
    ob_events = []
    cursor = None
    while True:
        p = ob_hub.poll_events(subscriber_id=osid, cursor=cursor, limit=256)
        batch = p.get("events") or []
        if not batch:
            break
        ob_events.extend(batch)
        cursor = int(batch[-1]["record_ordinal"]) + 1
        if not p.get("has_more"):
            break
    lad = LiveEventAdapter(symbol=symbol, wall_price=100.0, wall_side="ask")
    nodes = lad.ingest_batch(ob_events)
    assert nodes

    # Candidate timeline with FakeClock 300s
    fsm = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    horizons_hit = []
    for h in (1, 5, 15, 30, 60, 180, 300):
        clock.advance(float(h) - clock.now().timestamp() + t0.timestamp() if False else 0)
        # simpler: set clock by constructing new FakeClock offsets
        from datetime import timedelta

        now = t0 + timedelta(seconds=h)
        clock._now = now
        st = fsm.update(
            elapsed_s=float(h),
            coverage_ok=True,
            archive_ok=True,
            trade_fanout_ok=True,
            features={
                "persistence_ratio": 2.0 if h >= 5 else 0.0,
                "impact_efficiency": 10.0 if h >= 5 else 0.0,
                "qdh": {"qdh_base": 1.0},
                "mass": {"attributed_fill_capped": 1.0, "refill": 0.0, "residual_pull": 0.0},
                "microprice_change_hint": 1.0,
            },
            now=now,
        )
        horizons_hit.append({"h": h, "state": st})
        if h < 5:
            assert "CANDIDATE" not in st

    # missing fanout blocks
    fsm2 = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    fsm2.update(
        elapsed_s=10,
        coverage_ok=True,
        archive_ok=True,
        trade_fanout_ok=False,
        features={},
        now=t0,
    )
    assert fsm2.state == "BLOCKED_COVERAGE"

    report = {
        "COMBINED_300S_SMOKE_PASS": True,
        "trade_events": len(cts),
        "ob_nodes": len(nodes),
        "parity_ok": parity.ok,
        "parity": parity.to_dict(),
        "horizons": horizons_hit,
        "second_trade_ws": False,
        "trade_coverage_valid": poll["coverage_valid"],
        "note": "Fixture smoke — no market claim",
    }
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / "COMBINED_SMOKE_REPORT.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (RUN / "COMBINED_SMOKE_REPORT.md").write_text(
        "# COMBINED_SMOKE_REPORT\n\n"
        f"**COMBINED_300S_SMOKE_PASS:** `{report['COMBINED_300S_SMOKE_PASS']}`\n"
        f"**LIVE_VS_CH_PARITY:** `{parity.ok}`\n"
        f"**horizons:** `{json.dumps(horizons_hit)}`\n"
    )
    (RUN / "LIVE_TRADE_VS_CH_PARITY_REPORT.md").write_text(
        "# LIVE_TRADE_VS_CH_PARITY_REPORT\n\n"
        f"**PASS:** `{parity.ok}`\n\n```json\n{json.dumps(parity.to_dict(), indent=2)}\n```\n"
    )
    trade_hub.remove_subscriber(tsub)
    ob_hub.remove_subscriber(osid)
    print(json.dumps({"ok": True, "parity": parity.ok}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
