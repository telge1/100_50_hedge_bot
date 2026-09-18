"""Isolated end-to-end smoke: fixture Full-OB → fanout → analyzer → archive → parity.

No second Bybit WS. No live collector restart.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_FANOUT_LIVE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/"
    "src/orderbook_analyse/orderbook_v2_live"
)
_MAIN_OA = Path("/home/telgenbuescher/projects/orderbook_analyse/src")


def _ensure_paths() -> None:
    # Main orderbook_analyse first (has .research); never put fanout src on sys.path.
    for p in (str(_MAIN_OA),):
        if p not in sys.path:
            sys.path.insert(0, p)
    eng = str(Path(__file__).resolve().parents[2])
    if eng not in sys.path:
        sys.path.insert(0, eng)


def _load_fanout_module(mod_name: str, filename: str) -> types.ModuleType:
    """Load a fanout-worktree module into the already-imported orderbook_analyse package."""
    full_name = f"orderbook_analyse.orderbook_v2_live.{mod_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = _FANOUT_LIVE / filename
    spec = importlib.util.spec_from_file_location(full_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_collector() -> tuple[Any, Any, Any]:
    _ensure_paths()
    # Ensure package hierarchy exists from MAIN
    import orderbook_analyse.orderbook_v2_live  # noqa: F401

    fanout_mod = _load_fanout_module("full_ob_event_fanout", "full_ob_event_fanout.py")
    case_mod = _load_fanout_module("full_ob_case_archive", "full_ob_case_archive.py")
    # on_demand_full imports fanout modules by package path — already registered
    odm = _load_fanout_module("on_demand_full", "on_demand_full.py")
    return odm.FullBookOnDemandManager, fanout_mod, case_mod


def run_smoke(*, mode: str, run_dir: Path) -> dict[str, Any]:
    from .clock import FakeClock, RealClock
    from .contract import write_contract_manifest
    from .fanout_client import InProcessFanoutBridge
    from .pipeline import run_isolated_observation
    from .report import write_json, write_markdown_summary
    from .schema import EmaSignalEvent

    FullBookOnDemandManager, _fanout_mod, _case_mod = _load_collector()
    from orderbook_analyse.orderbook_v2_live.full_book_state import full_orderbook_topic

    run_dir.mkdir(parents=True, exist_ok=True)
    archive_root = Path(
        "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/runs/ema_case_archive_tests_v1"
    )
    archive_root.mkdir(parents=True, exist_ok=True)

    settings = {
        "enabled": True,
        "max_active_topics": 4,
        "heartbeat_sec": 15.0,
        "lease_ttl_sec": 120.0,
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
    mgr.case_archive.default_archive_root = archive_root

    symbol = "TESTUSDT"
    rt = mgr._get_runtime(symbol)
    rt.book.apply_snapshot(
        bids=[["99.0", "10"], ["98.5", "5"], ["99.5", "20"]],
        asks=[["100.0", "15"], ["100.5", "8"], ["101.0", "4"]],
        u=1000,
        seq=5000,
        ts_ms=int(time.time() * 1000),
        mark_ready=True,
    )
    rt.subscription_state = "live"
    mgr.event_fanout.note_snapshot_ready(symbol)

    client = InProcessFanoutBridge(fanout=mgr.event_fanout, case_archive=mgr.case_archive)
    signal = EmaSignalEvent(
        signal_id="smoke-1",
        symbol=symbol,
        candle_time=datetime.now(timezone.utc),
        created_on=datetime.now(timezone.utc),
        ema_value=99.0,
        current_price=100.0,
        distance=1.0,
        expected_distance=0.5,
        source_trade_direction="LONG",
        threshold_side="ABOVE_EMA_THRESHOLD",
    )
    state = {"u": 1000, "seq": 5000, "n": 0}

    def inject(*, phase: str, elapsed: float = 0.0, step: float = 0.1) -> None:
        now = datetime.now(timezone.utc)
        if phase == "snapshot":
            payload = {
                "topic": full_orderbook_topic(symbol),
                "type": "snapshot",
                "ts": int(time.time() * 1000),
                "data": {
                    "s": symbol,
                    "u": state["u"],
                    "seq": state["seq"],
                    "b": [["99.0", "10"], ["99.5", "20"]],
                    "a": [["100.0", "15"], ["101.0", "4"]],
                },
            }
            mgr._notify_observers(
                symbol=symbol,
                payload=payload,
                received_at=now,
                receive_time_ns=time.time_ns(),
                phase="resync_ready",
                outcome="checkpoint",
            )
            return
        state["u"] += 1
        state["seq"] += 1
        state["n"] += 1
        bid_q = str(max(1.0, 20.0 - state["n"] * 0.01))
        ask_q = str(max(0.5, 15.0 - state["n"] * 0.02))
        rt.book.apply_delta(
            bids=[["99.5", bid_q]],
            asks=[["100.0", ask_q]],
            u=state["u"],
            seq=state["seq"],
            ts_ms=int(time.time() * 1000),
            enforce_continuity=False,
        )
        payload = {
            "topic": full_orderbook_topic(symbol),
            "type": "delta",
            "ts": int(time.time() * 1000),
            "data": {
                "s": symbol,
                "u": state["u"],
                "seq": state["seq"],
                "b": [["99.5", bid_q]],
                "a": [["100.0", ask_q]],
            },
        }
        mgr._notify_observers(
            symbol=symbol,
            payload=payload,
            received_at=now,
            receive_time_ns=time.time_ns(),
            phase="live",
            outcome="applied",
        )

    trade_rows = []
    base_ts = datetime.now(timezone.utc)
    for i in range(40):
        trade_rows.append(
            {
                "symbol": symbol,
                "trade_id": f"t{i}",
                "trade_ts": base_ts,
                "price": 100.0,
                "size": 0.5,
                "side": "Buy",
            }
        )

    if mode == "fake300":
        clock = FakeClock(_now=datetime.now(timezone.utc))
        result = run_isolated_observation(
            signal=signal,
            client=client,
            clock=clock,
            tick_size=0.1,
            wall_price=100.0,
            trade_rows=trade_rows,
            archive_root=archive_root,
            run_dir=run_dir,
            inject_book_messages=inject,
        )
        smoke_name = "ISOLATED_SMOKE_FAKE300"
    else:
        clock = RealClock()
        result = run_isolated_observation(
            signal=signal,
            client=client,
            clock=clock,
            tick_size=0.1,
            wall_price=100.0,
            trade_rows=trade_rows,
            archive_root=archive_root,
            run_dir=run_dir,
            inject_book_messages=inject,
            realtime_min_seconds=65.0,
        )
        smoke_name = "ISOLATED_SMOKE_REALTIME65"

    mgr.close()

    write_json(run_dir / f"{smoke_name}.json", result.report)
    write_markdown_summary(
        run_dir / "ISOLATED_SMOKE_REPORT.md",
        smoke_name,
        {
            "ok": result.ok,
            "status": result.status,
            "mode": mode,
            "elapsed_s": result.report.get("elapsed_s"),
            "horizons_reached": result.report.get("horizons_reached"),
            "candidate": result.report.get("candidate"),
            "fanout_metrics": result.report.get("fanout_metrics"),
            "archive": result.report.get("archive"),
            "parity": result.report.get("parity"),
        },
    )
    pt = result.report.get("public_trades") or {}
    write_json(
        run_dir / "PUBLIC_TRADES_LATENCY_REPORT.json",
        {
            "mode": "fake_adapter",
            "note": "CH live latency not measured in isolated smoke; FakePublicTradesAdapter used.",
            "coverage": pt,
            "measured_ch_latency": None,
        },
    )
    write_markdown_summary(
        run_dir / "PUBLIC_TRADES_LATENCY_REPORT.md",
        "PUBLIC_TRADES_LATENCY_REPORT",
        {
            "measured_ch_latency": None,
            "adapter": "FakePublicTradesAdapter",
            "status": pt.get("status"),
            "poll_count": pt.get("poll_count"),
        },
    )
    man = write_contract_manifest(
        run_dir / "CONTRACT_MANIFEST.json",
        collector_src_root=Path(
            "/home/telgenbuescher/projects/orderbook_analyse_ema_delta_fanout_v1/src/orderbook_analyse"
        ),
    )
    return {
        "ok": result.ok,
        "status": result.status,
        "mode": mode,
        "contract_hash": man.get("contract_hash"),
        "report_path": str(run_dir / "ISOLATED_SMOKE_REPORT.md"),
        "fanout_metrics": result.report.get("fanout_metrics"),
        "parity_ok": (result.report.get("parity") or {}).get("ok"),
    }
