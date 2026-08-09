"""Paper / forward runner orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db.signals import frozen_eff_edges_all_signal_tfs
from orderbook_analyse.fractal_wave_fade_forward_paper import (
    AUDIT_VERSION,
    DEFAULT_OUT_DIR,
    DEFAULT_PAPER_START,
    OPTIONAL_SYMBOLS,
    PRIMARY_SYMBOLS,
    STRATEGY_VERSION,
)
from orderbook_analyse.fractal_wave_fade_forward_paper.data import (
    btc_forward_available,
    ensure_env,
    freshness,
    latest_1m_ts,
    load_books,
    load_signals,
)
from orderbook_analyse.fractal_wave_fade_forward_paper.parity import run_parity
from orderbook_analyse.fractal_wave_fade_forward_paper.simulator import simulate_symbol_paper
from orderbook_analyse.fractal_wave_fade_forward_paper.state import (
    TRADE_COLUMNS,
    PaperState,
    SymbolState,
    append_event,
    load_state,
    load_trades,
    save_state,
    save_trades,
    state_to_dict,
)
from orderbook_analyse.fractal_wave_fade_forward_paper.summary import build_summary
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db import PRIMARY_FEE


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _paths(out_dir: Path) -> dict[str, Path]:
    return {
        "state": out_dir / "paper_state.json",
        "trades": out_dir / "paper_trades.csv",
        "events": out_dir / "paper_events.jsonl",
        "summary": out_dir / "summary.json",
        "equity": out_dir / "equity_curve.csv",
        "parity": out_dir / "parity_report.json",
    }


def init_or_load_state(
    out_dir: Path,
    *,
    paper_start: str,
    conflict_exit: bool,
    fee_pct: float,
) -> PaperState:
    paths = _paths(out_dir)
    existing = load_state(paths["state"])
    if existing is not None:
        # never change forward_capture_start / paper_start retroactively
        return existing
    now = pd.Timestamp.now(tz="UTC").isoformat()
    st = PaperState(
        strategy_version=STRATEGY_VERSION,
        paper_start=_utc(paper_start).isoformat(),
        runner_created_at=now,
        forward_capture_start=now,
        conflict_exit_enabled=conflict_exit,
        fee_pct=fee_pct,
    )
    for sym in PRIMARY_SYMBOLS:
        st.ensure_symbol(sym)
    return st


def active_symbols(paper_start: pd.Timestamp) -> list[tuple[str, str]]:
    """Return (symbol, coverage_flag)."""
    out = [("DOGEUSDT", "DOGE_FORWARD_DATA_READY")]  # refreshed later with stale check
    ok, flag = btc_forward_available(paper_start)
    if ok:
        out.append(("BTCUSDT", flag))
    else:
        out.append(("BTCUSDT", flag))
    return out


def run_symbol(
    symbol: str,
    st: PaperState,
    *,
    edges: dict,
    mode: str,
    until: pd.Timestamp | None,
    force_close_end: bool,
    events_path: Path,
) -> dict[str, Any]:
    books = load_books(symbol)
    sig = load_signals(symbol, books, edges)
    paper_start = _utc(st.paper_start)
    fwd_cap = _utc(st.forward_capture_start) if st.forward_capture_start else None

    fresh = freshness(books)
    latest = latest_1m_ts(books)
    if symbol == "DOGEUSDT":
        if latest < paper_start:
            # DB has not yet reached paper window — not ready for forward PnL
            cov = "DOGE_FORWARD_DATA_STALE"
        elif fresh["stale"] and mode == "FORWARD":
            cov = "DOGE_FORWARD_DATA_STALE"
        else:
            cov = "DOGE_FORWARD_DATA_READY"
    else:
        cov = "BTC_FORWARD_DATA_READY" if latest >= paper_start else "BTC_FORWARD_COVERAGE_UNAVAILABLE"

    ss = st.ensure_symbol(symbol)
    ss.forward_coverage = cov

    if mode == "FORWARD" and fresh["stale"] and symbol == "DOGEUSDT":
        append_event(
            events_path,
            {
                "event_ts": pd.Timestamp.now(tz="UTC").isoformat(),
                "symbol": symbol,
                "event_type": "ERROR",
                "trade_id": None,
                "cluster_id": None,
                "details": {"error": "DATA_STALE", **fresh},
            },
        )
        return {"skipped": True, "reason": "DATA_STALE", "freshness": fresh, "coverage": cov}

    until_ts = until or latest_1m_ts(books)

    def emit(ev: dict) -> None:
        append_event(events_path, ev)

    # Deterministic full resim from paper_start → until (idempotent rewrite)
    result = simulate_symbol_paper(
        symbol,
        sig,
        books,
        paper_start=paper_start,
        forward_capture_start=fwd_cap,
        fee_pct=st.fee_pct,
        conflict_exit=st.conflict_exit_enabled,
        trade_id_start=1,
        until_1m=until_ts,
        force_close_end=force_close_end,
        emit=emit,
    )

    ss.last_processed_1m_ts = result["last_processed_1m_ts"]
    ss.last_signal_available_at = result.get("last_signal_available_at") or ss.last_signal_available_at
    ss.entered_cluster_ids = sorted(result["entered_cluster_ids"])
    ss.n_entries = int(result["stats"].get("n_entries", 0))
    ss.n_upgrades = int(result["stats"].get("n_upgrades", 0))
    ss.n_closed = int(result["stats"].get("n_closed", 0))
    ss.n_signals_seen = int(result["stats"].get("n_universe_signals", 0))
    op = result["open"]
    ss.open_position = op
    if op is None:
        ss.status = "FLAT"
    else:
        ss.status = "OPEN_LONG" if op.side == "LONG" else "OPEN_SHORT"

    return {
        "skipped": False,
        "trades": result["trades"],
        "coverage": cov,
        "freshness": fresh,
        "stats": result["stats"],
        "open": op,
    }


def _write_equity(trades: pd.DataFrame, path: Path) -> None:
    if trades is None or trades.empty:
        return
    df = trades.sort_values("exit_time").copy()
    df["equity"] = 100.0 + df["net_return_pct"].astype(float).cumsum()
    df[["exit_time", "symbol", "net_return_pct", "equity", "validation_mode"]].to_csv(path, index=False)


def run_replay(
    out_dir: Path | None = None,
    *,
    paper_start: str = DEFAULT_PAPER_START,
    conflict_exit: bool = True,
    fee_pct: float = PRIMARY_FEE,
    until: str | None = None,
) -> dict[str, Any]:
    ensure_env()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = _paths(out_dir)

    # Fresh replay: reset events/trades but keep forward_capture if state exists? Spec: replay is technical.
    # Keep runner_created_at / forward_capture_start if present; reset trade artifacts.
    st = init_or_load_state(out_dir, paper_start=paper_start, conflict_exit=conflict_exit, fee_pct=fee_pct)
    st.mode_last_run = "REPLAY"
    st.conflict_exit_enabled = conflict_exit
    st.fee_pct = fee_pct
    # do not change paper_start / forward_capture_start if already set
    if paths["events"].exists():
        paths["events"].write_text("", encoding="utf-8")

    edges = frozen_eff_edges_all_signal_tfs()
    all_trades: list[dict] = []
    coverage_flags = {}
    until_ts = _utc(until) if until else None

    for sym, flag in active_symbols(_utc(st.paper_start)):
        if flag == "BTC_FORWARD_COVERAGE_UNAVAILABLE":
            ss = st.ensure_symbol(sym)
            ss.forward_coverage = flag
            coverage_flags[sym] = flag
            append_event(
                paths["events"],
                {
                    "event_ts": pd.Timestamp.now(tz="UTC").isoformat(),
                    "symbol": sym,
                    "event_type": "ERROR",
                    "trade_id": None,
                    "cluster_id": None,
                    "details": {"error": flag},
                },
            )
            continue
        res = run_symbol(
            sym,
            st,
            edges=edges,
            mode="REPLAY",
            until=until_ts,
            force_close_end=False,
            events_path=paths["events"],
        )
        coverage_flags[sym] = res.get("coverage", flag)
        if not res.get("skipped"):
            all_trades.extend(res["trades"])

    tdf = pd.DataFrame(all_trades)
    if not tdf.empty:
        # ensure columns
        for c in TRADE_COLUMNS:
            if c not in tdf.columns:
                tdf[c] = None
        tdf = tdf[TRADE_COLUMNS]
    save_trades(paths["trades"], tdf)
    _write_equity(tdf, paths["equity"])
    save_state(paths["state"], st)

    summary = build_summary(
        tdf,
        state_to_dict(st),
        extras={"audit_version": AUDIT_VERSION, "coverage_flags": coverage_flags, "run_mode": "REPLAY"},
    )
    paths["summary"].write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return {"state": st, "summary": summary, "paths": {k: str(v) for k, v in paths.items()}, "coverage_flags": coverage_flags}


def run_once(
    out_dir: Path | None = None,
    *,
    paper_start: str = DEFAULT_PAPER_START,
    conflict_exit: bool = True,
    fee_pct: float = PRIMARY_FEE,
) -> dict[str, Any]:
    ensure_env()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = _paths(out_dir)
    st = init_or_load_state(out_dir, paper_start=paper_start, conflict_exit=conflict_exit, fee_pct=fee_pct)
    st.mode_last_run = "FORWARD"

    if st.parity_status != "PAPER_RUNNER_MATCHES_BACKTEST":
        return {
            "blocked": True,
            "reason": "PARITY_REQUIRED",
            "parity_status": st.parity_status,
            "message": "Run parity successfully before FORWARD --once",
        }

    # rewrite events for this pass? append-only for forward. Clear only on first forward after replay.
    edges = frozen_eff_edges_all_signal_tfs()
    all_trades: list[dict] = []
    coverage_flags = {}
    stale = False

    for sym, flag in active_symbols(_utc(st.paper_start)):
        if flag == "BTC_FORWARD_COVERAGE_UNAVAILABLE":
            ss = st.ensure_symbol(sym)
            ss.forward_coverage = flag
            coverage_flags[sym] = flag
            continue
        res = run_symbol(
            sym,
            st,
            edges=edges,
            mode="FORWARD",
            until=None,
            force_close_end=False,
            events_path=paths["events"],
        )
        coverage_flags[sym] = res.get("coverage", flag)
        if res.get("skipped") and res.get("reason") == "DATA_STALE":
            stale = True
            coverage_flags[sym] = "DOGE_FORWARD_DATA_STALE"
            continue
        if not res.get("skipped"):
            all_trades.extend(res["trades"])

    tdf = pd.DataFrame(all_trades)
    if not tdf.empty:
        for c in TRADE_COLUMNS:
            if c not in tdf.columns:
                tdf[c] = None
        tdf = tdf[TRADE_COLUMNS]
    save_trades(paths["trades"], tdf)
    _write_equity(tdf, paths["equity"])
    save_state(paths["state"], st)
    summary = build_summary(
        tdf,
        state_to_dict(st),
        extras={
            "audit_version": AUDIT_VERSION,
            "coverage_flags": coverage_flags,
            "run_mode": "FORWARD",
            "data_stale": stale,
        },
    )
    paths["summary"].write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return {
        "blocked": False,
        "state": st,
        "summary": summary,
        "paths": {k: str(v) for k, v in paths.items()},
        "coverage_flags": coverage_flags,
        "data_stale": stale,
    }


def run_parity_gate(
    out_dir: Path | None = None,
    *,
    window_start: str = "2024-01-01T00:00:00+00:00",
    window_end: str = "2024-04-01T00:00:00+00:00",
) -> dict[str, Any]:
    ensure_env()
    out_dir = out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = _paths(out_dir)
    # Warmup from DOGE history start so open-state path matches full backtest
    report = run_parity(
        "DOGEUSDT",
        window_start=window_start,
        window_end=window_end,
        fee_pct=PRIMARY_FEE,
        conflict_exit=True,
    )
    # Fix parity to use early paper_start inside parity.py - update call
    paths["parity"].write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    st = init_or_load_state(
        out_dir, paper_start=DEFAULT_PAPER_START, conflict_exit=True, fee_pct=PRIMARY_FEE
    )
    st.parity_status = report["status"]
    save_state(paths["state"], st)
    return report


def status(out_dir: Path | None = None) -> dict[str, Any]:
    out_dir = out_dir or DEFAULT_OUT_DIR
    paths = _paths(out_dir)
    st = load_state(paths["state"])
    trades = load_trades(paths["trades"])
    if st is None:
        return {"error": "NO_STATE", "hint": "Run --parity then --replay-from first"}
    summary = build_summary(trades, state_to_dict(st))
    # compact status view
    return {
        "paper_start": st.paper_start,
        "runner_created_at": st.runner_created_at,
        "forward_capture_start": st.forward_capture_start,
        "parity_status": st.parity_status,
        "mode_last_run": st.mode_last_run,
        "fee_pct": st.fee_pct,
        "conflict_exit_enabled": st.conflict_exit_enabled,
        "symbols": {
            sym: {
                "status": ss.status,
                "last_processed_1m_ts": ss.last_processed_1m_ts,
                "forward_coverage": ss.forward_coverage,
                "open": (
                    {
                        "trade_id": ss.open_position.trade_id,
                        "side": ss.open_position.side,
                        "plan_tf": ss.open_position.plan_tf,
                        "tp_pct": ss.open_position.tp_pct,
                        "sl_pct": ss.open_position.sl_pct,
                        "n_upgrades": ss.open_position.n_upgrades,
                    }
                    if ss.open_position
                    else None
                ),
                "closed": ss.n_closed,
            }
            for sym, ss in st.symbols.items()
        },
        "REPLAY": summary.get("REPLAY"),
        "TRUE_FORWARD": summary.get("TRUE_FORWARD"),
    }
