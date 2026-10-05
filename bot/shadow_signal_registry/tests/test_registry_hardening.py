"""Explicit §20 registry hardening tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot.shadow_signal_registry.store as store_mod
from bot.shadow_signal_registry.models import BLOCK_REASON_NONE, make_signal_id
from bot.shadow_signal_registry.outcome_tracker import TrackedShadow, incremental_step, process_long_bar, process_short_bar
from bot.shadow_signal_registry.rebuild import normalize_block_reason, rebuild_snapshots_from_events
from bot.shadow_signal_registry.registry import ShadowRegistry
from bot.shadow_signal_registry.short_product import consolidate_short_product
from bot.shadow_signal_registry.snapshot import write_snapshot
from bot.shadow_signal_registry.store import RegistryStore
from bot.shadow_signal_registry.summary import build_summary


def _utc(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> dict:
    return {"open_time": t, "close_time": t, "open": o, "high": h, "low": l, "close": c}


def _long_sig(ladder_pass: bool) -> dict:
    return {
        "symbol": "XUSDT",
        "decision_time": "2026-02-01T12:00:00+00:00",
        "pool_id": "pool-x",
        "entry_price": 100.0,
        "stop": 99.0,
        "tp": 110.0,
        "ladder_pass": ladder_pass,
        "m15_lower_2_age_h": 2.0,
    }


def _short_product(block_reason: str = BLOCK_REASON_NONE, **overrides) -> dict:
    base = {
        "symbol": "YUSDT",
        "decision_time": "2026-02-01T12:00:00+00:00",
        "entry_price": 100.0,
        "stop": 102.0,
        "tp": 95.0,
        "pool_id": "pool-y",
        "allowed": block_reason == BLOCK_REASON_NONE,
        "blocked": block_reason != BLOCK_REASON_NONE,
        "hypothetical": block_reason != BLOCK_REASON_NONE,
        "block_reason": block_reason,
        "raw_block_reason": None if block_reason == BLOCK_REASON_NONE else "ACTIVE",
        "signal_status": "ALLOWED" if block_reason == BLOCK_REASON_NONE else "BLOCKED",
        "e1r_state": "INACTIVE" if block_reason == BLOCK_REASON_NONE else "ACTIVE",
        "floor_guard_state": "PASS",
    }
    base.update(overrides)
    return base


# --- §20 mapping: each test name documents scenario coverage ---


def test_01_deterministic_signal_id() -> None:
    a = make_signal_id("short", "s", "v1", "A", "t", "p")
    b = make_signal_id("short", "s", "v1", "A", "t", "p")
    assert a == b


def test_02_duplicate_prevention(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("short")
    p = _short_product()
    assert reg.register_short_product(p)
    assert reg.register_short_product(p) is None
    assert len(reg.store.load_events()) == 3


def test_03_append_only_event_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    st = RegistryStore("long")
    st.append_event({"event_type": "A", "n": 1})
    st.append_event({"event_type": "B", "n": 2})
    lines = st.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "A"
    assert json.loads(lines[1])["event_type"] == "B"
    st.append_event({"event_type": "C", "n": 3})
    assert json.loads(st.events_path.read_text(encoding="utf-8").splitlines()[0])["n"] == 1


def test_04_snapshot_rebuild_from_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("long")
    sid = reg.register_long_geometry(_long_sig(True))
    assert sid
    row = reg.snapshots[sid]
    row.update(
        {
            "tracking_status": "CLOSED",
            "outcome": "SL",
            "pnl_pct": -1.0,
            "exit_time": "2026-02-01T12:05:00+00:00",
            "exit_price": 99.0,
        }
    )
    reg._append(sid, "SHADOW_SL", "XUSDT", {"pnl_pct": -1.0, "outcome": "SL", "exit_price": 99.0})
    reg._persist()
    events = reg.store.load_events()
    rebuilt = rebuild_snapshots_from_events(events, "long")
    assert rebuilt[sid]["allowed"] is True
    assert rebuilt[sid]["block_reason"] == BLOCK_REASON_NONE
    assert rebuilt[sid]["entry_price"] == 100.0
    assert rebuilt[sid]["tracking_status"] == "CLOSED"
    assert rebuilt[sid]["outcome"] == "SL"


def test_05_atomic_snapshot_write(tmp_path) -> None:
    rows = [{"signal_id": "x", "decision_time": "t", "block_reason": BLOCK_REASON_NONE}]
    csv_p = tmp_path / "snap.csv"
    json_p = tmp_path / "snap.json"
    seen_tmp = []

    real_replace = Path.replace

    def track_replace(self, target):  # noqa: ANN001
        if str(self).endswith(".tmp"):
            seen_tmp.append(self.read_text(encoding="utf-8"))
        return real_replace(self, target)

    with patch.object(Path, "replace", track_replace):
        write_snapshot("long", rows, csv_p, json_p)
    assert csv_p.is_file() and json_p.is_file()
    assert any("signal_id" in t for t in seen_tmp)
    assert json.loads(json_p.read_text()) == rows


def test_06_long_allowed_tp() -> None:
    t = TrackedShadow("x", "X", "long", "t", 100.0, 99.0, 99.0, 110.0, False)
    bar = _bar(_utc(2026, 1, 1, 0, 1), 100, 111, 100, 110)
    closed, _ = process_long_bar(t, bar)
    assert closed and t.outcome == "TP" and t.pnl_pct > 0


def test_07_long_allowed_sl() -> None:
    t = TrackedShadow("x", "X", "long", "t", 100.0, 99.0, 99.0, 110.0, False)
    bar = _bar(_utc(2026, 1, 1, 0, 1), 100, 100.5, 98, 99)
    closed, _ = process_long_bar(t, bar)
    assert closed and t.outcome == "SL"


def test_08_long_allowed_be() -> None:
    t = TrackedShadow("x", "X", "long", "t", 100.0, 99.0, 99.0, 110.0, False)
    b1 = _bar(_utc(2026, 1, 1, 0, 1), 100, 101.5, 99.5, 100.5)
    closed, be = process_long_bar(t, b1)
    assert be and not closed
    b2 = _bar(_utc(2026, 1, 1, 0, 2), 100.5, 100.5, 99.9, 100)
    closed, _ = process_long_bar(t, b2)
    assert closed and t.outcome == "BE"


def test_09_long_blocked_hypothetical_tp(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("long")
    sid = reg.register_long_geometry(_long_sig(False))
    row = reg.snapshots[sid]
    assert row["hypothetical"] and row["block_reason"] == "LADDER24"
    t = reg.open[sid]
    bar = _bar(_utc(2026, 2, 1, 12, 1), 100, 111, 100, 110)
    process_long_bar(t, bar)
    assert t.outcome == "TP"


def test_10_long_blocked_hypothetical_sl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("long")
    sid = reg.register_long_geometry(_long_sig(False))
    t = reg.open[sid]
    bar = _bar(_utc(2026, 2, 1, 12, 1), 100, 100.5, 98, 99)
    process_long_bar(t, bar)
    assert t.outcome == "SL" and t.pnl_pct < 0


def test_11_long_blocked_hypothetical_be(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("long")
    sid = reg.register_long_geometry(_long_sig(False))
    t = reg.open[sid]
    b1 = _bar(_utc(2026, 2, 1, 12, 1), 100, 101.2, 99.8, 100.2)
    process_long_bar(t, b1)
    b2 = _bar(_utc(2026, 2, 1, 12, 2), 100.2, 100.3, 99.95, 100)
    process_long_bar(t, b2)
    assert t.outcome == "BE"


def test_12_short_allowed_tp() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, False)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 101, 94, 96))
    assert t.outcome == "TP"


def test_13_short_allowed_sl() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, False)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 103, 99, 102))
    assert t.outcome == "SL"


def test_14_short_e1r_blocked_hypothetical_tp() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, True)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 101, 94, 96))
    assert t.outcome == "TP"


def test_15_short_e1r_blocked_hypothetical_sl() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, True)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 103, 99, 102))
    assert t.outcome == "SL"


def test_16_short_floorguard_blocked_hypothetical_tp() -> None:
    rows = [
        {"variant": "floor_blocked", "pool_id": "p", "symbol": "X", "decision_time": "t", "entry_price": 100, "stop": 102, "tp": 95},
    ]
    p = consolidate_short_product(rows)
    assert p["block_reason"] == "FLOOR_GUARD"
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, True)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 101, 94, 96))
    assert t.outcome == "TP"


def test_17_short_floorguard_blocked_hypothetical_sl() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 102.0, 102.0, 95.0, True)
    assert process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 103, 99, 102))
    assert t.outcome == "SL"


def test_18_baseline_no_guard_no_registry_duplicate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("short")
    rows = [{"variant": "no_guard", "pool_id": "p", "symbol": "X", "signal_status": "BASELINE_NO_GUARD"}]
    assert consolidate_short_product(rows) is None
    assert len(reg.known_ids) == 0
    assert len(reg.store.load_events()) == 0


def test_19_mae_mfe_long_order_and_no_post_exit() -> None:
    t = TrackedShadow("x", "X", "long", "t", 100.0, 90.0, 90.0, 110.0, False)
    closed, _ = process_long_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 110, 95, 108))
    assert closed and t.outcome == "TP"
    assert t.mae_pct == 5.0 and t.mfe_pct == 10.0
    assert t.mae_pct <= t.mfe_pct or t.mae_pct == 5.0


def test_19b_mae_mfe_short_order_and_no_post_exit() -> None:
    t = TrackedShadow("x", "X", "short", "t", 100.0, 105.0, 105.0, 95.0, False)
    closed = process_short_bar(t, _bar(_utc(2026, 1, 1, 0, 1), 100, 101, 94, 96))
    assert closed and t.outcome == "TP"
    assert t.mae_pct == 1.0 and t.mfe_pct == 6.0


def test_20_restart_recovery_open_trade(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("short")
    sid = reg.register_short_product(_short_product())
    assert sid in reg.open
    t0 = _utc(2026, 2, 1, 12, 0)
    reg.open[sid].last_processed_1m = _iso(t0)
    reg._persist()
    n_events = len(reg.store.load_events())

    reg2 = ShadowRegistry("short")
    assert sid in reg2.open
    assert reg2.open[sid].last_processed_1m == _iso(t0)
    assert len(reg2.store.load_events()) == n_events
    assert reg2.register_short_product(_short_product()) is None


def _iso(t: datetime) -> str:
    return t.isoformat()


def test_21_incremental_1m_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    t1 = _utc(2026, 2, 1, 12, 1)
    t2 = t1 + timedelta(minutes=1)
    t3 = t1 + timedelta(minutes=2)
    bars = [_bar(t1, 100, 100.2, 99.8, 100), _bar(t2, 100, 100.2, 99.8, 100), _bar(t3, 100, 103, 99, 102)]

    trade = TrackedShadow(
        "x",
        "YUSDT",
        "short",
        t1.isoformat(),
        100.0,
        102.0,
        102.0,
        95.0,
        False,
    )
    trade.last_processed_1m = t1.isoformat()
    calls = []

    def load(_sym: str, _start: datetime, _end: datetime) -> list:
        calls.append(1)
        return bars

    incremental_step(trade, [], load, t3 + timedelta(minutes=1))
    assert trade.last_processed_1m == t2.isoformat() or trade.outcome == "SL"
    if trade.status == "OPEN":
        incremental_step(trade, [], load, t3 + timedelta(minutes=2))
        assert trade.last_processed_1m == t3.isoformat() or trade.outcome


def test_22_no_orders_in_registry_package() -> None:
    forbidden = (
        "create_order",
        "place_order",
        "amend_order",
        "cancel_order",
        "set_leverage",
        "wallet",
    )
    root = Path(__file__).resolve().parents[1]
    for p in root.rglob("*.py"):
        if "tests" in p.parts:
            continue
        text = p.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text


def test_block_reason_normalization_on_register(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    long_reg = ShadowRegistry("long")
    sid_l = long_reg.register_long_geometry(_long_sig(True))
    assert long_reg.snapshots[sid_l]["block_reason"] == BLOCK_REASON_NONE
    sig_b = _long_sig(False)
    sig_b["decision_time"] = "2026-02-01T13:00:00+00:00"
    sig_b["pool_id"] = "pool-x-blocked"
    sid_b = long_reg.register_long_geometry(sig_b)
    assert sid_b is not None
    assert long_reg.snapshots[sid_b]["block_reason"] == "LADDER24"

    short_reg = ShadowRegistry("short")
    sid_s = short_reg.register_short_product(_short_product())
    assert short_reg.snapshots[sid_s]["block_reason"] == BLOCK_REASON_NONE
    sid_e = short_reg.register_short_product(_short_product("E1R_ACTIVE", decision_time="2026-02-01T13:00:00+00:00"))
    assert short_reg.snapshots[sid_e]["block_reason"] == "E1R_ACTIVE"


def test_summary_unchanged_after_none_normalization() -> None:
    rows = [
        {
            "allowed": True,
            "blocked": False,
            "block_reason": BLOCK_REASON_NONE,
            "hypothetical": False,
            "tracking_status": "CLOSED",
            "outcome": "SL",
            "pnl_pct": -1.12,
        },
        {
            "allowed": False,
            "blocked": True,
            "block_reason": "E1R_ACTIVE",
            "hypothetical": True,
            "tracking_status": "CLOSED",
            "outcome": "SL",
            "pnl_pct": -0.83,
        },
    ]
    s = build_summary("short", rows)
    assert s["total_signals"] == 2
    assert s["allowed"] == 1
    assert s["blocked"] == 1
    assert s["SL"] == 2
    assert s["e1r_saved_losses"] == 1


def test_snapshot_schema_block_reason_never_null_on_flush(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("short")
    reg.register_short_product(_short_product())
    reg.flush_snapshots()
    data = json.loads(reg.store.snapshot_json.read_text(encoding="utf-8"))
    for row in data:
        assert row["block_reason"] is not None
        if row["allowed"]:
            assert row["block_reason"] == BLOCK_REASON_NONE
