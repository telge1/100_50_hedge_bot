"""Phase 6E — sequential 15m candle → LMV2 input consumer (no trades).

Uses the same ClickHouse 1m → 15m aggregation as ``build_candles_by_tf``.
Does not import ``backtest.py`` or call ``run_backtest``.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import build_candles_by_tf
from orderbook_analyse.cluster_sweep_research.clickhouse_source import aggregate_timeframe

from .historical_lmv2_input_dry_run import (
    DEFAULT_DATA_SOURCE,
    DEFAULT_MODE,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    HistoricalLmv2InputDryRunError,
    run_historical_lmv2_input_dry_run,
)

FIFTEEN_MINUTES = timedelta(minutes=15)
CONSUMER_ENTRYPOINT = (
    "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1."
    "historical_lmv2_sequential_candle_input.run_sequential_lmv2_candle_input_dry_run"
)
CANDLE_LABEL_CONVENTION = "open_time_is_bar_open_utc_left_labeled"


class SequentialCandleInputError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def decision_close_from_15m_open(open_time: datetime) -> datetime:
    """Map aggregated 15m ``open_time`` (bar open) to LMV2 ``decision_close`` (bar close)."""
    return _as_utc(open_time) + FIFTEEN_MINUTES


def open_time_from_decision_close(decision_close: datetime) -> datetime:
    return _as_utc(decision_close) - FIFTEEN_MINUTES


def _align_decision_close(dc: datetime) -> datetime:
    dc = _as_utc(dc)
    if dc.minute % 15 != 0 or dc.second != 0 or dc.microsecond != 0:
        raise SequentialCandleInputError(
            "DECISION_CLOSE_NOT_ON_15M_GRID",
            "decision_close must align to 15m UTC boundary",
            {"decision_close": _iso_z(dc)},
        )
    return dc


def parse_decision_close(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        return _align_decision_close(datetime.fromisoformat(text))
    return _align_decision_close(value)


def load_doge_15m_candles_from_ch(
    *,
    first_decision_close: datetime | str,
    last_decision_close: datetime | str,
    symbol: str = DEFAULT_SYMBOL,
    client=None,
) -> pd.DataFrame:
    """Load complete 15m bars whose closes fall in [first, last] (inclusive)."""
    sym = str(symbol).upper()
    if sym != DEFAULT_SYMBOL:
        raise SequentialCandleInputError("INVALID_SYMBOL", "only DOGEUSDT supported", {"symbol": sym})
    fc = parse_decision_close(first_decision_close)
    lc = parse_decision_close(last_decision_close)
    if lc < fc:
        raise SequentialCandleInputError("INVALID_WINDOW", "last before first")
    start_open = open_time_from_decision_close(fc)
    end_open = open_time_from_decision_close(lc)
    # 1m fetch: inclusive of last bar's minutes
    fetch_start = start_open
    fetch_end = lc + timedelta(minutes=1)
    candles = build_candles_by_tf(sym, fetch_start, fetch_end, client=client)
    df1 = candles.get("1m")
    if df1 is None or df1.empty:
        raise SequentialCandleInputError(
            "NO_CANDLE_DATA",
            "no 1m candles in window",
            {"fetch_start": _iso_z(fetch_start), "fetch_end": _iso_z(fetch_end)},
        )
    df15 = aggregate_timeframe(df1, "15m")
    if df15.empty:
        raise SequentialCandleInputError("NO_15M_BARS", "aggregation produced no 15m bars")
    rows = []
    for _, row in df15.iterrows():
        ot = pd.Timestamp(row["open_time"]).to_pydatetime()
        dc = decision_close_from_15m_open(ot)
        if fc <= dc <= lc:
            rows.append(
                {
                    "candle_open_utc": _iso_z(_as_utc(ot)),
                    "candle_close_utc": _iso_z(dc),
                    "decision_close": _iso_z(dc),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0.0),
                }
            )
    if not rows:
        raise SequentialCandleInputError("NO_BARS_IN_CLOSE_WINDOW", "no complete 15m bars in close window")
    return pd.DataFrame(rows)


def validate_monotonic_candle_series(candles: Sequence[dict[str, Any]]) -> None:
    seen: set[str] = set()
    prev_dc: datetime | None = None
    for i, c in enumerate(candles):
        dc_s = str(c["decision_close"])
        if dc_s in seen:
            raise SequentialCandleInputError(
                "DUPLICATE_DECISION_CLOSE",
                "duplicate candle close",
                {"decision_close": dc_s, "index": i},
            )
        seen.add(dc_s)
        dc = parse_decision_close(dc_s)
        if prev_dc is not None:
            delta = dc - prev_dc
            if delta != FIFTEEN_MINUTES:
                raise SequentialCandleInputError(
                    "CANDLE_GAP_OR_ORDER",
                    "non-15m step between closes",
                    {"prev": _iso_z(prev_dc), "current": _iso_z(dc), "index": i},
                )
        prev_dc = dc


def membership_transition(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return {
            "decision_close": curr.get("decision_close"),
            "added_pool_ids": sorted(str(m.get("pool_id")) for m in curr.get("active_members") or []),
            "removed_pool_ids": [],
            "unchanged_pool_ids": [],
            "changed_fields": [],
        }
    pm = {str(m.get("pool_id")): m for m in prev.get("active_members") or []}
    cm = {str(m.get("pool_id")): m for m in curr.get("active_members") or []}
    added = sorted(set(cm) - set(pm))
    removed = sorted(set(pm) - set(cm))
    unchanged = []
    changed = []
    for pid in sorted(set(pm) & set(cm)):
        if pm[pid] == cm[pid]:
            unchanged.append(pid)
        else:
            changed.append({"pool_id": pid, "before": pm[pid], "after": cm[pid]})
    return {
        "decision_close": curr.get("decision_close"),
        "added_pool_ids": added,
        "removed_pool_ids": removed,
        "unchanged_pool_ids": unchanged,
        "changed_fields": changed,
    }


def run_sequential_lmv2_candle_input_dry_run(
    *,
    first_decision_close: datetime | str,
    last_decision_close: datetime | str,
    pool_detail: int,
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    data_source: str = DEFAULT_DATA_SOURCE,
    mode: str = DEFAULT_MODE,
    loader: Callable[..., dict[str, Any]],
    loader_kwargs: dict[str, Any] | None = None,
    client=None,
    bridge_parity_fn: Callable[[str, int], dict[str, Any]] | None = None,
    strategy_input_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Walk 15m closes in order; load LMV2 state at each via Phase 6D entry."""
    if timeframe != DEFAULT_TIMEFRAME:
        raise SequentialCandleInputError("INVALID_TIMEFRAME", "only 15m supported")
    df = load_doge_15m_candles_from_ch(
        first_decision_close=first_decision_close,
        last_decision_close=last_decision_close,
        symbol=symbol,
        client=client,
    )
    candle_rows = df.to_dict(orient="records")
    validate_monotonic_candle_series(candle_rows)

    per_close: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    consumer_receipts: list[dict[str, Any]] = []
    prev_state: dict[str, Any] | None = None

    for candle in candle_rows:
        dc = candle["decision_close"]
        real = run_historical_lmv2_input_dry_run(
            decision_close=dc,
            pool_detail=int(pool_detail),
            symbol=symbol,
            timeframe=timeframe,
            data_source=data_source,
            mode=mode,
            loader=loader,
            loader_kwargs=loader_kwargs,
        )
        st = real["lmv2_state"]
        row: dict[str, Any] = {
            "candle": candle,
            "real_entry": {
                "entrypoint": real.get("entrypoint"),
                "integrity": real.get("integrity"),
            },
            "lmv2_state": copy.deepcopy(st),
        }
        if bridge_parity_fn is not None:
            bridge = bridge_parity_fn(dc, int(pool_detail))
            parity_ok = (
                bridge.get("members_hash") == st.get("members_hash")
                and bridge.get("input_state_hash") == st.get("input_state_hash")
                and bridge.get("snapshot_id") == st.get("snapshot_id")
                and list(bridge.get("active_members") or []) == list(st.get("active_members") or [])
            )
            row["bridge_parity"] = {
                "ok": parity_ok,
                "bridge_members_hash": bridge.get("members_hash"),
                "real_members_hash": st.get("members_hash"),
            }
            if not parity_ok:
                row["verdict"] = "FAIL"
            else:
                row["verdict"] = "PASS"
        else:
            row["verdict"] = "PASS"
        if strategy_input_consumer is not None:
            st_before = copy.deepcopy(st)
            consumed = strategy_input_consumer(
                candle=candle,
                lmv2_state=st,
                pool_detail=int(pool_detail),
            )
            if st_before != st:
                row["verdict"] = "FAIL"
                row["consumer_error"] = "STATE_MUTATION_AFTER_CONSUMER"
            receipt = (consumed or {}).get("receipt")
            if not receipt:
                row["verdict"] = "FAIL"
                row["consumer_error"] = "MISSING_RECEIPT"
            else:
                row["strategy_consumer"] = consumed
                consumer_receipts.append(receipt)
                if receipt.get("members_hash") != st.get("members_hash"):
                    row["verdict"] = "FAIL"
                    row["consumer_error"] = "RECEIPT_HASH_MISMATCH"
        per_close.append(row)
        transitions.append(membership_transition(prev_state, st))
        prev_state = st

    all_pass = all(r.get("verdict") == "PASS" for r in per_close)
    out: dict[str, Any] = {
        "consumer_entrypoint": CONSUMER_ENTRYPOINT,
        "candle_time_contract": {
            "label": CANDLE_LABEL_CONVENTION,
            "decision_close_definition": "candle_open_utc + 15 minutes",
            "timezone": "UTC",
            "availability_class": "RESEARCH_HISTORICAL",
        },
        "pool_detail": int(pool_detail),
        "first_decision_close": _iso_z(parse_decision_close(first_decision_close)),
        "last_decision_close": _iso_z(parse_decision_close(last_decision_close)),
        "candle_count": len(per_close),
        "per_close": per_close,
        "membership_transitions": transitions,
        "verdict": "PASS" if all_pass else "FAIL",
    }
    if strategy_input_consumer is not None:
        out["strategy_input_consumer_enabled"] = True
        out["consumer_receipts"] = consumer_receipts
        out["consumer_receipt_count"] = len(consumer_receipts)
        out["exactly_once_consumption"] = len(consumer_receipts) == len(per_close)
        if not out["exactly_once_consumption"]:
            out["verdict"] = "FAIL"
    return out


def describe_candle_time_contract() -> dict[str, Any]:
    return {
        "source": "signal_generator.candles_1m via fetch_candles_1m",
        "aggregation": "aggregate_timeframe (left-labeled, closed=left, complete buckets only)",
        "bar_identity_field": "open_time",
        "bar_identity_meaning": "UTC instant of 15m period start (open)",
        "decision_close_mapping": "open_time + 15 minutes",
        "lmv2_publication_binding": "decision_close at 15m UTC grid (bar close)",
        "engine_1m_semantics_note": "run_backtest uses last 1m with open_time+1m <= as_of (bar close)",
        "not_assumed": "open_time is NOT treated as decision_close",
    }
