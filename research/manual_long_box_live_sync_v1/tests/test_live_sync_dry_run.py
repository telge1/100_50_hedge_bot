"""Phase 1 functional tests — isolated drawings, DRY-RUN only."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from research.manual_long_box_live_sync_v1.activation import BoxActivation
from research.manual_long_box_live_sync_v1.config import DEBOUNCE_SEC
from research.manual_long_box_live_sync_v1.drawing_watch import observe_drawings
from research.manual_long_box_live_sync_v1.exchange_dry_run import DryRunExchangeAdapter
from research.manual_long_box_live_sync_v1.persistence import EventLog, StateStore
from research.manual_long_box_live_sync_v1.service import SyncService
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

LIVE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json"
)
DID = "d-test-sync-phase1"


def _long_box_row(
    *,
    drawing_id: str = DID,
    entry: float = 0.05,
    stop: float = 0.049,
    target: float = 0.052,
    start_ts: int = 1787751300,
    end_ts: int = 1787757000,
) -> dict:
    return {
        "drawing_id": drawing_id,
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "created_on_timeframe": "5m",
        "timeframe_scope": "all",
        "visible": True,
        "locked": False,
        "metadata": {},
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
    }


@pytest.fixture()
def workspace(tmp_path):
    drawings = tmp_path / "drawings.json"
    activations = tmp_path / "activations.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(
        json.dumps({"version": 1, "drawings": [_long_box_row()]}),
        encoding="utf-8",
    )
    activations.write_text(
        json.dumps(
            {
                "activations": {
                    DID: {
                        "enabled": True,
                        "symbol": "DOGEUSDT",
                        "notional_usd": 10,
                        "dry_run": True,
                        "qty_doge": 148.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return drawings, activations, state, events


def test_create_then_amend_entry_twice_one_open_order(workspace):
    drawings, activations, state, events = workspace
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
        poll_sec=0.1,
        debounce_sec=0.05,
    )
    r1 = svc.run_once()
    assert r1[0]["ok"] and r1[0]["action"] == "CREATE_LIMIT"
    oid1 = r1[0]["order_id"]

    data = json.loads(drawings.read_text())
    box = data["drawings"][0]
    box["entry_price"] = float(box["entry_price"]) * 1.001
    drawings.write_text(json.dumps(data), encoding="utf-8")

    r2 = svc.run_once()
    assert r2[0]["ok"] and r2[0]["action"] == "AMEND_LIMIT"
    assert r2[0]["order_id"] == oid1
    assert len(svc.exchange.reconcile_open_orders("DOGEUSDT")) == 1

    box["entry_price"] = float(box["entry_price"]) * 1.001
    drawings.write_text(json.dumps(data), encoding="utf-8")
    r3 = svc.run_once()
    assert r3[0]["action"] == "AMEND_LIMIT"
    assert len(svc.exchange.reconcile_open_orders("DOGEUSDT")) == 1


def test_no_op_when_entry_unchanged(workspace):
    drawings, activations, state, events = workspace
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
    )
    svc.run_once()
    r2 = svc.run_once()
    assert r2[0]["action"] == "no_entry_change"


def test_sl_tp_change_documented_not_amended(workspace):
    drawings, activations, state, events = workspace
    eng = ManualLongBoxSyncEngine(
        DryRunExchangeAdapter(), StateStore(state), EventLog(events)
    )
    payload = json.loads(drawings.read_text())
    act = BoxActivation(DID, True, "DOGEUSDT", 50.0, True)
    obs = observe_drawings(drawings)
    assert obs
    eng.process_observation(obs, {DID: act})
    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = float(data["drawings"][0]["target_price"]) * 1.01
    drawings.write_text(json.dumps(data), encoding="utf-8")
    obs2 = observe_drawings(drawings)
    r = eng.process_observation(obs2, {DID: act})[0]
    assert r["action"] == "no_entry_change"
    st = StateStore(state).get(DID)
    assert st and st.last_action == "desired_sl_tp_documented"


def test_not_activated_no_order(workspace):
    drawings, activations, state, events = workspace
    activations.write_text(json.dumps({"activations": {}}), encoding="utf-8")
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
    )
    assert svc.run_once() == []


def test_fill_during_amend_no_second_order(workspace):
    drawings, activations, state, events = workspace
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
    )
    svc.run_once()
    r = svc.engine.handle_fill_during_amend(DID)
    assert r["ok"] and not r["second_open_order"]
    data = json.loads(drawings.read_text())
    data["drawings"][0]["entry_price"] = float(data["drawings"][0]["entry_price"]) * 0.999
    drawings.write_text(json.dumps(data), encoding="utf-8")
    r2 = svc.run_once()[0]
    assert r2.get("action") == "skipped_filled_no_second_order"


def test_service_detects_file_change_latency(workspace):
    drawings, activations, state, events = workspace
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
        poll_sec=0.05,
        debounce_sec=0.08,
    )
    svc.run_once()
    svc.detector.poll()
    t0 = time.time()
    data = json.loads(drawings.read_text())
    data["drawings"][0]["entry_price"] = float(data["drawings"][0]["entry_price"]) * 1.002
    drawings.write_text(json.dumps(data), encoding="utf-8")

    detected = threading.Event()

    def poller():
        for _ in range(80):
            changed, _ = svc.detector.poll()
            if changed:
                svc._on_change()
                detected.set()
                break
            time.sleep(0.02)

    threading.Thread(target=poller, daemon=True).start()
    assert detected.wait(timeout=2.0)
    time.sleep(DEBOUNCE_SEC + 0.15)
    st = StateStore(state).get(DID)
    assert st and st.last_action == "AMEND_LIMIT"
    latency = time.time() - t0
    assert latency < 1.5


def test_state_survives_restart(workspace):
    drawings, activations, state, events = workspace
    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
    )
    oid = svc.run_once()[0]["order_id"]
    svc2 = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
    )
    assert svc2.state.get(DID).exchange_order_id == oid


def test_live_file_manual_long_optional_readonly():
    if not LIVE.is_file():
        pytest.skip("live drawings missing")
    data = json.loads(LIVE.read_text())
    manual = [
        d
        for d in data.get("drawings", [])
        if str(d.get("drawing_id", "")).startswith("d-")
        and d.get("drawing_type") == "long_position"
        and str(d.get("symbol", "")).upper() == "DOGEUSDT"
    ]
    if not manual:
        pytest.skip("no manual d-* long_position in live file (use activations when present)")
    row = manual[0]
    assert row.get("entry_price") is not None
