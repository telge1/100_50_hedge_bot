"""Tests for C3.5c APT paper-forward monitor."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5c_apt_forward_monitor import (
    DEFAULT_OUT,
    SYMBOL,
    TIMEFRAME,
    VARIANT,
    ConfigMismatchError,
    ForwardMonitor,
    MonitorState,
    atomic_write_json,
    frozen_hashes,
    last_complete_15m_open,
    parse_utc,
    ret_pct,
)


def _bars(n: int = 12, start: str = "2026-07-01 00:00:00+00:00") -> pd.DataFrame:
    ts = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    rows = []
    px = 100.0
    for i, t in enumerate(ts):
        o = px
        h = px + 1.0 + (0.2 if i % 3 == 0 else 0.0)
        l = px - 1.0 - (0.2 if i % 4 == 0 else 0.0)
        c = px + (0.5 if i % 2 == 0 else -0.3)
        rows.append(
            {
                "timestamp": t,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000.0,
            }
        )
        px = c
    df = pd.DataFrame(rows)
    df["bar_index"] = range(len(df))
    df["symbol"] = SYMBOL
    df["timeframe"] = TIMEFRAME
    return df


def _trig(frame: pd.DataFrame, i: int, side: str, setup_id: int) -> dict:
    ts = pd.Timestamp(frame.iloc[i]["timestamp"]).tz_convert("UTC").isoformat()
    return {
        "side": side,
        "setup_id": setup_id,
        "trigger_timestamp": ts,
        "trigger_price": float(frame.iloc[i]["close"]),
        "trigger_bar": i,
    }


def _mon(tmp: Path, forward_start: str) -> ForwardMonitor:
    return ForwardMonitor(tmp, forward_start_utc=forward_start, dry_run=False)


def _init(mon: ForwardMonitor, frame: pd.DataFrame, forward_start: str) -> None:
    mon.ensure_dirs()
    mon.state = mon.load_or_init_state(last_complete_open=parse_utc(forward_start))
    # freeze exactly
    mon.state.forward_start_utc = parse_utc(forward_start).isoformat()


def test_frozen_identity() -> None:
    h = frozen_hashes()
    assert h["variant"] == "A6"
    assert h["arming_mode"] == "external_bos"
    assert SYMBOL == "APTUSDT"
    assert TIMEFRAME == "15m"
    assert VARIANT == "A6"
    assert "c35c_apt_forward_monitor" in str(DEFAULT_OUT)


def test_pnl_mirror_and_costs() -> None:
    assert abs(ret_pct("long", 100.0, 101.0) - 1.0) < 1e-12
    assert abs(ret_pct("short", 100.0, 99.0) - (100 / 99 - 1) * 100) < 1e-12


def test_last_complete_15m_open() -> None:
    # 12:07 → last complete open 11:45
    t = parse_utc("2026-07-17T12:07:00Z")
    assert last_complete_15m_open(t) == parse_utc("2026-07-17T11:45:00Z")


def test_fill_next_open_and_pending(tmp_path: Path) -> None:
    frame = _bars(6)
    # forward start before first bar so all triggers after boundary
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    # process first 2 bars without trigger, then trigger on bar 2, fill on bar 3
    triggers = {_trig(frame, 2, "short", 1)["trigger_timestamp"]: _trig(frame, 2, "short", 1)}
    # only feed bars 0..2 first
    status = mon.process_with_synthetic_triggers(frame.iloc[:3].copy(), triggers)
    assert status["active_position"] is None
    assert status["pending_trigger"] is not None
    assert status["pending_trigger"]["side"] == "short"
    # next bar fills
    status2 = mon.process_with_synthetic_triggers(frame.iloc[:4].copy(), triggers)
    assert status2["active_position"] is not None
    assert status2["active_position"]["side"] == "short"
    assert abs(float(status2["active_position"]["entry_price"]) - float(frame.iloc[3]["open"])) < 1e-12
    assert status2["pending_trigger"] is None


def test_opposite_closes_and_reverses(tmp_path: Path) -> None:
    frame = _bars(8)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 4, "short", 2)["trigger_timestamp"]: _trig(frame, 4, "short", 2),
    }
    status = mon.process_with_synthetic_triggers(frame, tmap)
    trades = mon.read_trades()
    assert len(trades) == 1
    assert trades.iloc[0]["side"] == "long"
    assert abs(float(trades.iloc[0]["exit_price"]) - float(frame.iloc[5]["open"])) < 1e-12
    assert status["active_position"]["side"] == "short"
    assert status["n_closed_trades"] == 1


def test_same_direction_ignored(tmp_path: Path) -> None:
    frame = _bars(8)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 4, "long", 2)["trigger_timestamp"]: _trig(frame, 4, "long", 2),
    }
    status = mon.process_with_synthetic_triggers(frame, tmap)
    assert len(mon.read_trades()) == 0
    assert status["active_position"]["side"] == "long"
    # only one position
    events = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text().splitlines() if x]
    assert any(e["event_type"] == "same_direction_signal_ignored" for e in events)


def test_forward_boundary_skips_history(tmp_path: Path) -> None:
    frame = _bars(8)
    # boundary after bar 3 open → triggers on bars 0..3 ignored
    fwd = pd.Timestamp(frame.iloc[3]["timestamp"]).tz_convert("UTC").isoformat()
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "short", 1)["trigger_timestamp"]: _trig(frame, 1, "short", 1),
        _trig(frame, 5, "long", 2)["trigger_timestamp"]: _trig(frame, 5, "long", 2),
    }
    status = mon.process_with_synthetic_triggers(frame, tmap)
    # only later trigger counts → fill on bar 6
    assert status["n_closed_trades"] == 0
    assert status["active_position"] is not None
    assert status["active_position"]["side"] == "long"
    assert abs(float(status["active_position"]["entry_price"]) - float(frame.iloc[6]["open"])) < 1e-12


def test_restart_open_and_pending(tmp_path: Path) -> None:
    frame = _bars(10)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {_trig(frame, 2, "short", 1)["trigger_timestamp"]: _trig(frame, 2, "short", 1)}
    mon.process_with_synthetic_triggers(frame.iloc[:3].copy(), tmap)
    assert mon.state.pending_trigger is not None
    # restart
    mon2 = _mon(tmp_path, fwd)
    mon2.state = mon2.load_or_init_state(last_complete_open=parse_utc(fwd))
    assert mon2.state.pending_trigger is not None
    status = mon2.process_with_synthetic_triggers(frame.iloc[:5].copy(), tmap)
    assert status["active_position"]["side"] == "short"
    # reprocess same bars → no duplicate trades
    status2 = mon2.process_with_synthetic_triggers(frame.iloc[:5].copy(), tmap)
    assert status2["n_closed_trades"] == 0
    assert len(mon2.read_trades()) == 0


def test_restart_no_duplicate_closed_trade(tmp_path: Path) -> None:
    frame = _bars(10)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 4, "short", 2)["trigger_timestamp"]: _trig(frame, 4, "short", 2),
    }
    mon.process_with_synthetic_triggers(frame, tmap)
    assert len(mon.read_trades()) == 1
    mon2 = _mon(tmp_path, fwd)
    mon2.state = mon2.load_or_init_state(last_complete_open=parse_utc(fwd))
    mon2.process_with_synthetic_triggers(frame, tmap)
    assert len(mon2.read_trades()) == 1


def test_mfe_mae_long(tmp_path: Path) -> None:
    frame = _bars(6)
    # craft highs/lows after entry
    frame.loc[3, "high"] = 110.0
    frame.loc[3, "low"] = 95.0
    frame.loc[3, "close"] = 105.0
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {_trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1)}
    status = mon.process_with_synthetic_triggers(frame.iloc[:4].copy(), tmap)
    pos = status["active_position"]
    entry = float(pos["entry_price"])
    assert float(pos["mfe_pct"]) == pytest.approx((110.0 / entry - 1) * 100)
    assert float(pos["mae_pct"]) == pytest.approx((95.0 / entry - 1) * 100)


def test_config_hash_mismatch_stops(tmp_path: Path) -> None:
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, _bars(3), fwd)
    mon.save_state()
    raw = json.loads((tmp_path / "state.json").read_text())
    raw["config_hash"] = "deadbeef"
    atomic_write_json(tmp_path / "state.json", raw)
    mon2 = _mon(tmp_path, fwd)
    with pytest.raises(ConfigMismatchError):
        mon2.load_or_init_state(last_complete_open=parse_utc(fwd))


def test_source_hash_mismatch_stops(tmp_path: Path) -> None:
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, _bars(3), fwd)
    mon.save_state()
    raw = json.loads((tmp_path / "state.json").read_text())
    raw["source_hash"] = "deadbeef"
    atomic_write_json(tmp_path / "state.json", raw)
    mon2 = _mon(tmp_path, fwd)
    with pytest.raises(ConfigMismatchError):
        mon2.load_or_init_state(last_complete_open=parse_utc(fwd))


def test_forward_start_immutable(tmp_path: Path) -> None:
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, _bars(3), fwd)
    mon.save_state()
    mon2 = ForwardMonitor(tmp_path, forward_start_utc="2026-07-01T00:00:00Z")
    with pytest.raises(ConfigMismatchError):
        mon2.load_or_init_state(last_complete_open=parse_utc(fwd))


def test_snapshot_once_at_10(tmp_path: Path) -> None:
    # Need 10 closed trades: alternate long/short triggers every other opportunity
    n = 25
    frame = _bars(n)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {}
    side = "long"
    # triggers on bars 1,3,5,... → fills 2,4,6,... each opposite closes prior
    for i, bar in enumerate(range(1, n - 1, 2)):
        t = _trig(frame, bar, side, i + 1)
        tmap[t["trigger_timestamp"]] = t
        side = "short" if side == "long" else "long"
    mon.process_with_synthetic_triggers(frame, tmap)
    assert mon.state.n_closed_trades >= 10
    snap = tmp_path / "snapshots" / "forward_snapshot_0010.json"
    assert snap.exists()
    # second run must not rewrite (immutable existence)
    mtime = snap.stat().st_mtime
    mon.process_with_synthetic_triggers(frame, tmap)
    assert snap.stat().st_mtime == mtime


def test_atomic_write(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text())["a"] == 1


def test_report_with_open_trade(tmp_path: Path) -> None:
    frame = _bars(5)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {_trig(frame, 1, "short", 1)["trigger_timestamp"]: _trig(frame, 1, "short", 1)}
    status = mon.process_with_synthetic_triggers(frame, tmap)
    assert status["active_position"] is not None
    text = mon.format_status_text(status)
    assert "Active: SHORT" in text
    assert "Forward trades: 0 / 50" in text
    assert (tmp_path / "report.md").exists()
    open_pos = json.loads((tmp_path / "open_position.json").read_text())
    assert open_pos.get("side") == "short"


def test_deterministic_replay_synthetic(tmp_path: Path) -> None:
    frame = _bars(10)
    fwd = "2026-06-30T00:00:00Z"
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 4, "short", 2)["trigger_timestamp"]: _trig(frame, 4, "short", 2),
        _trig(frame, 7, "long", 3)["trigger_timestamp"]: _trig(frame, 7, "long", 3),
    }

    def run(dir_: Path) -> pd.DataFrame:
        mon = _mon(dir_, fwd)
        _init(mon, frame, fwd)
        mon.process_with_synthetic_triggers(frame, tmap)
        return mon.read_trades()

    t1 = run(tmp_path / "a")
    t2 = run(tmp_path / "b")
    cols = [c for c in t1.columns if c != "trade_id"]
    assert t1[cols].astype(str).reset_index(drop=True).equals(t2[cols].astype(str).reset_index(drop=True))


def test_annulled_not_trades_without_trigger(tmp_path: Path) -> None:
    """Without injected triggers, no paper trades — annulled paths never fill."""
    frame = _bars(8)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    status = mon.process_with_synthetic_triggers(frame, {})
    assert status["n_closed_trades"] == 0
    assert status["active_position"] is None
    assert len(mon.read_trades()) == 0


def test_cost_columns_roundtrip(tmp_path: Path) -> None:
    frame = _bars(8)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 4, "short", 2)["trigger_timestamp"]: _trig(frame, 4, "short", 2),
    }
    mon.process_with_synthetic_triggers(frame, tmap)
    tr = mon.read_trades().iloc[0]
    g = float(tr["gross_return_pct"])
    assert abs(float(tr["net_return_0_20_pct"]) - (g - 0.20)) < 1e-12
    assert abs(float(tr["net_return_0_40_pct"]) - (g - 0.40)) < 1e-12


def test_no_lookahead_and_sm_untouched() -> None:
    import research.regime_scanner.pullback_entry_c3_5c_apt_forward_monitor as mod

    src = inspect.getsource(mod)
    assert "shift(-" not in src
    assert "lookahead_on" not in src
    assert "apply_pullback_entry" in src
    assert "baseline_a6" in src


def test_max_one_position(tmp_path: Path) -> None:
    frame = _bars(8)
    fwd = "2026-06-30T00:00:00Z"
    mon = _mon(tmp_path, fwd)
    _init(mon, frame, fwd)
    tmap = {
        _trig(frame, 1, "long", 1)["trigger_timestamp"]: _trig(frame, 1, "long", 1),
        _trig(frame, 3, "long", 2)["trigger_timestamp"]: _trig(frame, 3, "long", 2),
        _trig(frame, 5, "long", 3)["trigger_timestamp"]: _trig(frame, 5, "long", 3),
    }
    status = mon.process_with_synthetic_triggers(frame, tmap)
    assert status["active_position"] is not None
    assert len(mon.read_trades()) == 0
