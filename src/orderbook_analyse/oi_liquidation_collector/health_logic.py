"""Pure reconnect/liveness helpers. No I/O, no trading."""

from __future__ import annotations

from typing import Iterable


class DeadConnection(RuntimeError):
    """Socket or application heartbeat is dead; reconnect required."""


def next_backoff(current: float, *, initial: float = 1.0, cap: float = 4.0) -> float:
    nxt = current * 2.0 if current > 0 else initial
    if nxt < initial:
        nxt = initial
    return min(cap, nxt)


def pong_timed_out(
    *,
    last_ping_mono: float | None,
    last_pong_mono: float | None,
    now_mono: float,
    timeout_sec: float,
) -> bool:
    if last_ping_mono is None:
        return False
    if last_pong_mono is not None and last_pong_mono >= last_ping_mono:
        return False
    return (now_mono - last_ping_mono) >= timeout_sec


def market_data_stale(
    *,
    last_market_mono: float | None,
    now_mono: float,
    stale_sec: float,
) -> bool:
    if last_market_mono is None:
        return False
    return (now_mono - last_market_mono) >= stale_sec


def session_healthy(
    *,
    ws_connected: bool,
    subscription_confirmed: bool,
    ping_ok: bool,
    has_recent_market: bool,
) -> bool:
    return bool(ws_connected and subscription_confirmed and ping_ok and has_recent_market)


def liquidation_stream_healthy(
    *,
    ws_connected: bool,
    subscription_confirmed: bool,
    ping_ok: bool,
    liq_topic_subscribed: bool,
    last_liquidation_at=None,
) -> bool:
    """Absence of liquidations is not a failure."""
    _ = last_liquidation_at
    return bool(ws_connected and subscription_confirmed and ping_ok and liq_topic_subscribed)


def resubscribe_topics(symbols: Iterable[str]) -> list[str]:
    symbols = list(symbols)
    return [f"tickers.{s}" for s in symbols] + [f"allLiquidation.{s}" for s in symbols]


def is_pong_payload(payload: dict) -> bool:
    op = str(payload.get("op") or "")
    ret = str(payload.get("ret_msg") or "").lower()
    return op == "pong" or ret == "pong"


def is_bybit_fatal_error(payload: dict) -> bool:
    op = str(payload.get("op") or "")
    if op in {"ping", "pong", "subscribe"}:
        return False
    if payload.get("success") is False:
        return True
    return op == "error"
