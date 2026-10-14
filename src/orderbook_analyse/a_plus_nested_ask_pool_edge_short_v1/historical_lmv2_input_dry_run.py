"""Historical LMV2 input-only path for nested-ask backtester (Phase 6D).

Does not import ``backtest.py``, ``run_backtest``, or scanner pool loaders.
Pool state is supplied by an injected loader (Phase 6C bridge from dashboard).
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

STRATEGY_ID = "a_plus_nested_ask_pool_edge_short_v1"
ENTRYPOINT = (
    "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1."
    "historical_lmv2_input_dry_run.run_historical_lmv2_input_dry_run"
)
DEFAULT_DATA_SOURCE = "historical_lmv2_active_publication_state"
DEFAULT_MODE = "input_only"
DEFAULT_SYMBOL = "DOGEUSDT"
DEFAULT_TIMEFRAME = "15m"
VALID_POOL_DETAILS = frozenset({1, 5, 10})

LoaderFn = Callable[..., dict[str, Any]]


class HistoricalLmv2InputDryRunError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _parse_decision_close(decision_close: datetime | str) -> tuple[str, int]:
    if isinstance(decision_close, datetime):
        dt = decision_close
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    else:
        text = str(decision_close).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text).astimezone(timezone.utc)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso, int(dt.timestamp())


def _payload_hash(payload: dict[str, Any]) -> str:
    # Must match Phase 6C bridge ``compute_input_state_hash`` (no members, no self-hash).
    body = {k: v for k, v in payload.items() if k not in ("active_members", "input_state_hash")}
    body["members_hash"] = payload.get("members_hash")
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_bridge_payload(payload: dict[str, Any], *, decision_close_iso: str, pool_detail: int) -> None:
    required = (
        "bridge_version",
        "data_kind",
        "availability_class",
        "scanner_pool_equivalence",
        "symbol",
        "timeframe",
        "pool_detail",
        "pipeline_line_id",
        "config_hash",
        "decision_close",
        "snapshot_id",
        "members_hash",
        "input_state_hash",
        "active_members",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise HistoricalLmv2InputDryRunError(
            "INVALID_LOADER_PAYLOAD",
            "bridge payload missing required fields",
            {"missing": missing},
        )
    if int(payload["pool_detail"]) != int(pool_detail):
        raise HistoricalLmv2InputDryRunError(
            "PD_MISMATCH",
            "loader pool_detail does not match request",
            {"request": pool_detail, "payload": payload["pool_detail"]},
        )
    if str(payload["decision_close"]) != decision_close_iso:
        raise HistoricalLmv2InputDryRunError(
            "DECISION_CLOSE_MISMATCH",
            "loader decision_close does not match request",
            {"request": decision_close_iso, "payload": payload["decision_close"]},
        )
    if payload.get("data_kind") != DEFAULT_DATA_SOURCE:
        raise HistoricalLmv2InputDryRunError(
            "INVALID_DATA_KIND",
            "unexpected data_kind",
            {"data_kind": payload.get("data_kind")},
        )
    if payload.get("scanner_pool_equivalence") != "NOT_CLAIMED":
        raise HistoricalLmv2InputDryRunError(
            "SCANNER_EQUIVALENCE_CLAIM",
            "scanner equivalence must remain NOT_CLAIMED",
        )
    dc = decision_close_iso
    for m in payload.get("active_members") or []:
        known = m.get("known_at")
        if known is not None and str(known) > dc:
            raise HistoricalLmv2InputDryRunError(
                "CAUSALITY_VIOLATION",
                "known_at after decision_close",
                {"pool_id": m.get("pool_id"), "known_at": known, "decision_close": dc},
            )


def run_historical_lmv2_input_dry_run(
    *,
    decision_close: datetime | str,
    pool_detail: int,
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    data_source: str = DEFAULT_DATA_SOURCE,
    mode: str = DEFAULT_MODE,
    loader: LoaderFn,
    loader_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load LMV2 ACTIVE state through the nested-ask entry plane (no trades)."""
    if mode != DEFAULT_MODE:
        raise HistoricalLmv2InputDryRunError("INVALID_MODE", "only input_only supported", {"mode": mode})
    if data_source != DEFAULT_DATA_SOURCE:
        raise HistoricalLmv2InputDryRunError(
            "INVALID_DATA_SOURCE",
            "only historical_lmv2_active_publication_state supported",
            {"data_source": data_source},
        )
    pd = int(pool_detail)
    if pd not in VALID_POOL_DETAILS:
        raise HistoricalLmv2InputDryRunError(
            "INVALID_HISTORICAL_PD",
            "pool_detail must be 1, 5, or 10",
            {"pool_detail": pd},
        )
    sym = str(symbol or DEFAULT_SYMBOL).upper()
    tf = str(timeframe or DEFAULT_TIMEFRAME)
    close_iso, close_unix = _parse_decision_close(decision_close)

    kw = dict(loader_kwargs or {})
    bridge_payload = loader(
        decision_close,
        pool_detail=pd,
        symbol=sym,
        timeframe=tf,
        **kw,
    )
    if not isinstance(bridge_payload, dict):
        raise HistoricalLmv2InputDryRunError("INVALID_LOADER_PAYLOAD", "loader must return dict")

    _validate_bridge_payload(bridge_payload, decision_close_iso=close_iso, pool_detail=pd)
    immutable_members = copy.deepcopy(list(bridge_payload.get("active_members") or []))
    received = copy.deepcopy(bridge_payload)
    received["active_members"] = immutable_members
    received_hash = _payload_hash(received)
    if received_hash != bridge_payload.get("input_state_hash"):
        raise HistoricalLmv2InputDryRunError(
            "INPUT_STATE_HASH_DRIFT",
            "hash mismatch after backtester handoff copy",
            {"bridge": bridge_payload.get("input_state_hash"), "received": received_hash},
        )

    return {
        "strategy_id": STRATEGY_ID,
        "entrypoint": ENTRYPOINT,
        "data_source": data_source,
        "mode": mode,
        "symbol": sym,
        "timeframe": tf,
        "pool_detail": pd,
        "decision_close": close_iso,
        "decision_close_unix": close_unix,
        "time_semantics": {
            "decision_close": "15m bar close UTC (Z)",
            "engine_legacy_bar_close": "naive UTC in run_backtest scan loop",
            "lmv2_publication_binding": "snapshot at decision_close only",
        },
        "lmv2_state": received,
        "integrity": {
            "members_hash": received.get("members_hash"),
            "input_state_hash": received.get("input_state_hash"),
            "input_state_hash_after_handoff": received_hash,
            "snapshot_id": received.get("snapshot_id"),
            "pipeline_line_id": received.get("pipeline_line_id"),
            "config_hash": received.get("config_hash"),
            "availability_class": received.get("availability_class"),
            "scanner_pool_equivalence": received.get("scanner_pool_equivalence"),
            "active_member_count": received.get("active_member_count"),
            "pool_record_conversion": False,
            "engine_pools_loaded": False,
            "trading_executed": False,
        },
        "loader_module": str(kw.get("_loader_module") or "injected"),
    }
