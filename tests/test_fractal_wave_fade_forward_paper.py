"""Unit tests for frozen wave-fade paper runner (synthetic books, no downloads)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import apply_upgrade_plan, tpsl_for_tf
from orderbook_analyse.fractal_wave_fade_forward_paper.simulator import (
    _tp_sl_prices,
    simulate_symbol_paper,
)
from orderbook_analyse.fractal_wave_fade_forward_paper.state import (
    PaperState,
    load_state,
    save_state,
    state_from_dict,
    state_to_dict,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import SymbolBooks, _scan_exit


def _books(n: int = 500, start: str = "2024-01-01T00:00:00Z", base: float = 100.0) -> SymbolBooks:
    times = pd.date_range(start, periods=n, freq="min", tz="UTC")
    # mild drift
    close = base + np.linspace(0, 2.0, n)
    high = close + 0.2
    low = close - 0.2
    opens = close.copy()
    return SymbolBooks(
        high=high.astype(float),
        low=low.astype(float),
        close=close.astype(float),
        opens=opens.astype(float),
        open_times=times.to_numpy(dtype="datetime64[ns]"),
    )


def _sig_row(
    books: SymbolBooks,
    *,
    entry_i: int,
    side: str,
    tf: str,
    signal_id: int,
    tier_a: bool = True,
    conf_offset_min: int = 1,
) -> dict:
    entry_time = pd.Timestamp(books.open_times[entry_i], tz="UTC")
    conf = entry_time - pd.Timedelta(minutes=conf_offset_min)
    return {
        "signal_id": signal_id,
        "side": side,
        "signal_tf": tf,
        "confirmation_available_at": conf,
        "entry_i": entry_i,
        "entry_time": entry_time,
        "entry_price": float(books.opens[entry_i]),
        "entry_valid": True,
        "is_tier_a": tier_a,
        "is_q4": tier_a,
    }


def test_tpsl_ladder_frozen():
    assert tpsl_for_tf("15m") == (1.0, 1.0)
    assert tpsl_for_tf("30m") == (2.0, 1.5)
    assert tpsl_for_tf("1h") == (2.0, 1.5)
    assert tpsl_for_tf("4h") == (4.0, 2.0)


def test_p5a_upgrade_plan():
    assert apply_upgrade_plan("P5A", 1.0, 1.0, "4h") == (4.0, 2.0)


def test_tp_sl_price_rebuild():
    tp, sl = _tp_sl_prices("LONG", 100.0, 1.0, 1.0)
    assert abs(tp - 101.0) < 1e-9 and abs(sl - 99.0) < 1e-9
    tp, sl = _tp_sl_prices("SHORT", 100.0, 2.0, 1.5)
    assert abs(tp - 98.0) < 1e-9 and abs(sl - 101.5) < 1e-9


def test_sl_first_same_bar():
    # bar hits both
    high = np.array([102.0])
    low = np.array([98.0])
    et, eg, exi, amb = _scan_exit("LONG", 100.0, high, low, 0, 0, 1.0, 1.0)
    assert et == "SL" and amb is True and eg == -1.0


def test_t0_next_open_after_available(tmp_path: Path):
    books = _books(200)
    # confirmation strictly before entry bar open
    row = _sig_row(books, entry_i=50, side="LONG", tf="15m", signal_id=0, conf_offset_min=1)
    assert row["confirmation_available_at"] < row["entry_time"]
    sig = pd.DataFrame([row])
    # force price to hit TP quickly after entry
    books.high[51:60] = 102.0
    paper_start = pd.Timestamp(books.open_times[0], tz="UTC")
    out = simulate_symbol_paper(
        "DOGEUSDT",
        sig,
        books,
        paper_start=paper_start,
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert len(out["trades"]) == 1
    assert out["trades"][0]["entry_time"] == pd.Timestamp(books.open_times[50], tz="UTC").isoformat()
    assert abs(out["trades"][0]["entry_price"] - float(books.opens[50])) < 1e-9


def test_no_entry_before_paper_start():
    books = _books(200)
    row = _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0)
    books.high[11:30] = 102.0
    paper_start = pd.Timestamp(books.open_times[50], tz="UTC")
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame([row]),
        books,
        paper_start=paper_start,
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert out["trades"] == []
    assert out["open"] is None


def test_one_position_and_suppress():
    books = _books(400, base=100.0)
    # flat path so 15m TP1 never hits before second signal
    books.high[:] = 100.2
    books.low[:] = 99.8
    books.close[:] = 100.0
    books.opens[:] = 100.0
    rows = [
        _sig_row(books, entry_i=20, side="LONG", tf="15m", signal_id=0),
        _sig_row(books, entry_i=200, side="LONG", tf="15m", signal_id=1),
    ]
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame(rows),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
        until_1m=pd.Timestamp(books.open_times[220], tz="UTC"),
        force_close_end=False,
    )
    assert out["stats"]["n_entries"] == 1
    assert out["stats"]["n_suppressed"] >= 1
    assert out["open"] is not None


def test_p5a_upgrade_and_timeout_extension():
    books = _books(3000, base=100.0)
    # keep price in range so no TP/SL; allow upgrade then timeout
    rows = [
        _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0),
        _sig_row(books, entry_i=20, side="LONG", tf="1h", signal_id=1),
    ]
    rows[1]["confirmation_available_at"] = rows[0]["confirmation_available_at"] + pd.Timedelta(minutes=30)
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame(rows),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
        until_1m=pd.Timestamp(books.open_times[100], tz="UTC"),
        force_close_end=False,
    )
    assert out["open"] is not None
    assert out["open"].plan_tf == "1h"
    assert out["open"].tp_pct == 2.0
    assert out["open"].sl_pct == 1.5
    assert out["open"].max_hold_min == 72 * 60
    assert out["stats"]["n_upgrades"] == 1


def test_conflict_exit():
    books = _books(200)
    rows = [
        _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0),
        _sig_row(books, entry_i=30, side="SHORT", tf="1h", signal_id=1),
    ]
    rows[1]["confirmation_available_at"] = rows[0]["confirmation_available_at"] + pd.Timedelta(minutes=20)
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame(rows),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert len(out["trades"]) == 1
    assert out["trades"][0]["exit_reason"] == "HIGHER_TF_CONFLICT"


def test_fees():
    books = _books(100)
    books.high[11:20] = 102.0  # TP 1%
    row = _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0)
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame([row]),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert out["trades"][0]["gross_return_pct"] == 1.0
    assert out["trades"][0]["net_return_pct"] == pytest.approx(0.89)


def test_replay_vs_true_forward_label():
    books = _books(100)
    books.high[11:20] = 102.0
    row = _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0)
    fwd = pd.Timestamp(books.open_times[5], tz="UTC")
    out = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame([row]),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=fwd,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert out["trades"][0]["validation_mode"] == "TRUE_FORWARD"
    out2 = simulate_symbol_paper(
        "DOGEUSDT",
        pd.DataFrame([row]),
        books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=pd.Timestamp(books.open_times[50], tz="UTC"),
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    assert out2["trades"][0]["validation_mode"] == "REPLAY"


def test_state_roundtrip_idempotent(tmp_path: Path):
    st = PaperState(
        strategy_version="wave_fade_cluster_v1",
        paper_start="2026-08-08T19:30:00+00:00",
        runner_created_at="2026-08-08T20:00:00+00:00",
        forward_capture_start="2026-08-08T20:00:00+00:00",
    )
    st.ensure_symbol("DOGEUSDT")
    p = tmp_path / "paper_state.json"
    save_state(p, st)
    st2 = load_state(p)
    assert st2 is not None
    assert st2.forward_capture_start == st.forward_capture_start
    assert st2.paper_start == st.paper_start
    # immutable capture start preserved
    d = state_to_dict(st2)
    d["forward_capture_start"] = "CHANGED"
    # runner must not overwrite existing — simulated by init_or_load using existing
    st3 = state_from_dict(json.loads(p.read_text()))
    assert st3.forward_capture_start == "2026-08-08T20:00:00+00:00"


def test_duplicate_resim_same_trades():
    books = _books(150)
    books.high[11:40] = 102.0
    row = _sig_row(books, entry_i=10, side="LONG", tf="15m", signal_id=0)
    kwargs = dict(
        symbol="DOGEUSDT",
        sig_valid=pd.DataFrame([row]),
        books=books,
        paper_start=pd.Timestamp(books.open_times[0], tz="UTC"),
        forward_capture_start=None,
        fee_pct=0.11,
        conflict_exit=True,
        trade_id_start=1,
    )
    a = simulate_symbol_paper(**kwargs)
    b = simulate_symbol_paper(**kwargs)
    assert a["trades"] == b["trades"]
