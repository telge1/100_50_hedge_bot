from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from research.stoch_fade_portfolio_backtest.artifacts import write_run
from research.stoch_fade_portfolio_backtest.dedup import dedup_pairs, trade_pnl_usdt
from research.stoch_fade_portfolio_backtest.simulate import simulate_portfolio


def row(
    sid: str,
    symbol: str,
    tf: str,
    entry: str,
    exit_: str | None,
    outcome: str,
    pnl: float = 1.0,
    direction: str = "LONG",
) -> dict:
    return {
        "signal_id": sid,
        "symbol": symbol,
        "timeframe": tf,
        "direction": direction,
        "entry_time": entry,
        "entry_price": 1.0,
        "tp_price": 1.1,
        "sl_price": 0.9,
        "outcome": outcome,
        "exit_time": exit_,
        "exit_price": 1.1,
        "exit_reason": "TP" if outcome == "WIN" else ("SL" if outcome == "LOSS" else "OPEN"),
        "duration_seconds": 60,
        "pnl_pct_gross": pnl,
    }


def pair(r: dict) -> dict:
    sig = {
        "signal_id": r["signal_id"],
        "symbol": r["symbol"],
        "timeframe": r["timeframe"],
        "direction": r["direction"],
        "entry_time": r["entry_time"],
        "entry_price": r["entry_price"],
        "tp_price": r["tp_price"],
        "sl_price": r["sl_price"],
        "tier_a": True,
    }
    out = dict(r)
    out["initial_sl_price"] = r["sl_price"]
    return {"signal": sig, "outcome": out}


def test_ten_slots_eleventh_blocked():
    rows = [
        row(f"s{i:02d}", f"C{i}USDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "WIN")
        for i in range(11)
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=10, notional=100)
    assert len(sim.accepted) == 10
    assert sim.skipped[0]["skip_reason"] == "NO_FREE_SLOT"
    assert sim.peak_open == 10


def test_exit_before_entry_frees_slot():
    rows = [
        row("a", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "WIN"),
        row("b", "BBBUSDT", "15m", "2026-01-01T00:11:00Z", "2026-01-01T00:20:00Z", "WIN"),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=1, notional=100)
    assert [t["signal_id"] for t in sim.accepted] == ["a", "b"]


def test_same_minute_exit_blocks_slot():
    rows = [
        row("a", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "WIN"),
        row("b", "BBBUSDT", "15m", "2026-01-01T00:10:00Z", "2026-01-01T00:20:00Z", "WIN"),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=1, notional=100)
    assert [t["signal_id"] for t in sim.accepted] == ["a"]
    assert sim.skipped[0]["skip_reason"] == "NO_FREE_SLOT"


def test_symbol_already_open_and_after_exit():
    rows = [
        row("a", "XRPUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "WIN"),
        row("b", "XRPUSDT", "1h", "2026-01-01T00:05:00Z", "2026-01-01T00:20:00Z", "WIN"),
        row("c", "XRPUSDT", "15m", "2026-01-01T00:11:00Z", "2026-01-01T00:30:00Z", "LOSS", pnl=-1.0),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=10, notional=100)
    assert [t["signal_id"] for t in sim.accepted] == ["a", "c"]
    assert sim.skipped[0]["skip_reason"] == "SYMBOL_ALREADY_OPEN"


def test_dedup_timeframe_priority_and_conflict():
    pairs = [
        pair(row("z-low", "ETHUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "WIN", direction="LONG")),
        pair(row("a-high", "ETHUSDT", "4h", "2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z", "LOSS", pnl=-2, direction="SHORT")),
        pair(row("m-mid", "ETHUSDT", "1h", "2026-01-01T00:00:00Z", "2026-01-01T01:30:00Z", "WIN", direction="LONG")),
    ]
    out = dedup_pairs(pairs, 100)
    assert out["kept"][0]["signal_id"] == "a-high"
    assert out["stats"]["dropped_signals"] == 2
    assert out["stats"]["direction_conflict_groups"] == 1
    assert any(d["extra_flag"] == "DUPLICATE_DIRECTION_CONFLICT" for d in out["dropped"])


def test_fixed_notional_no_compounding_and_cash():
    rows = [
        row("a", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "WIN", pnl=2.0),
        row("b", "BBBUSDT", "15m", "2026-01-01T00:11:00Z", "2026-01-01T00:20:00Z", "LOSS", pnl=-1.0),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=10, notional=100)
    assert trade_pnl_usdt(2.0, 100) == 2.0
    assert abs(sim.realized_pnl - 1.0) < 1e-9
    assert abs(sim.free_cash - 1001.0) < 1e-9
    assert sim.reserved == 0
    assert all(abs(t["pnl_usdt"]) in {1.0, 2.0} or abs(t["pnl_usdt"] - 2.0) < 1e-9 or abs(t["pnl_usdt"] + 1) < 1e-9 for t in sim.accepted)


def test_insufficient_cash():
    rows = [row("a", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "OPEN")]
    sim = simulate_portfolio(rows, initial_balance=50, max_slots=10, notional=100)
    assert sim.skipped[0]["skip_reason"] == "INSUFFICIENT_FREE_CASH"


def test_open_blocks_until_end():
    rows = [
        row("a", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", None, "OPEN"),
        row("b", "BBBUSDT", "15m", "2026-01-01T00:01:00Z", "2026-01-01T00:02:00Z", "WIN"),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=1, notional=100)
    assert [t["signal_id"] for t in sim.accepted] == ["a"]
    assert sim.open_at_end[0]["signal_id"] == "a"
    assert sim.reserved == 100
    assert sim.realized_pnl == 0


def test_win_loss_open_counts():
    rows = [
        row("w", "AAAUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "WIN"),
        row("l", "BBBUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "LOSS", pnl=-1),
        row("o", "CCCUSDT", "15m", "2026-01-01T00:00:00Z", None, "OPEN"),
    ]
    sim = simulate_portfolio(rows, initial_balance=1000, max_slots=10, notional=100)
    assert sum(1 for t in sim.accepted if t["outcome"] == "WIN") == 1
    assert sum(1 for t in sim.accepted if t["outcome"] == "LOSS") == 1
    assert sum(1 for t in sim.accepted if t["outcome"] == "OPEN") == 1


def test_input_order_determinism():
    rows = [
        row("b", "BBBUSDT", "15m", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "WIN"),
        row("a", "AAAUSDT", "4h", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "LOSS", pnl=-1),
        row("c", "CCCUSDT", "1h", "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", "WIN"),
    ]
    s1 = simulate_portfolio(rows, initial_balance=1000, max_slots=2, notional=100)
    s2 = simulate_portfolio(list(reversed(rows)), initial_balance=1000, max_slots=2, notional=100)
    assert [t["signal_id"] for t in s1.accepted] == [t["signal_id"] for t in s2.accepted]


def test_write_run_no_duplicate_dir(tmp_path: Path):
    payload = dict(
        out_root=tmp_path,
        run_id="abc123",
        manifest={"ok": True},
        input_audit={},
        summary={},
        equity_curve=[],
        accepted=[],
        skipped=[],
        duplicate_audit=[],
        slot_history=[],
        open_at_end=[],
        breakdowns={"per_symbol": {}, "per_timeframe": {}, "per_slot": {}},
        log_text="x\n",
    )
    write_run(**payload)
    try:
        write_run(**payload)
        raised = False
    except FileExistsError:
        raised = True
    assert raised


def test_source_fingerprint_stable(tmp_path: Path):
    from research.stoch_fade_portfolio_backtest.io_util import file_fingerprint

    p = tmp_path / "outcomes.jsonl"
    p.write_text("{}\n")
    a = file_fingerprint(p)
    b = file_fingerprint(p)
    assert a["sha256"] == b["sha256"]
    assert a["size_bytes"] == b["size_bytes"]
