"""Research-only DOGE live shadow (no execution, no orders)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import DEFAULT_OUT_DIR, SCANNER_VERSION
from .runner import build_candles_by_tf, run_scanner

SHADOW_OUT_ROOT = Path(DEFAULT_OUT_DIR).parent / "a_plus_liquidity_pool_signal_scanner_v2_shadow"
DOGE_SYMBOL = "DOGEUSDT"
WARMUP_HOURS = 72


class V2ShadowEventLog:
    """Append-only shadow log; refuses overwrite of existing run dir."""

    STREAMS = (
        "signal_intents",
        "lifecycle_events",
        "gate_audit",
        "pool_selection",
        "wall_context",
        "data_quality",
        "heartbeat",
    )

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        if self.out_dir.exists() and any(self.out_dir.iterdir()):
            raise FileExistsError(f"refusing to overwrite: {self.out_dir}")
        self.out_dir.mkdir(parents=True, exist_ok=False)
        self._paths = {name: self.out_dir / f"{name}.jsonl" for name in self.STREAMS}

    def append(self, stream: str, row: dict[str, Any]) -> None:
        if stream not in self._paths:
            raise KeyError(stream)
        with self._paths[stream].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self.out_dir / "manifest.json"
        if path.exists():
            raise FileExistsError(path)
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


def _wall_snapshot(*, symbol: str, price: float, observed_at: datetime) -> list[dict[str, Any]]:
    """Optional live wall context — snapshot only, not a gate."""
    try:
        from orderbook_analyse.orderbook_v2_live.orderbook_state import OrderbookState

        ob = OrderbookState(symbol=symbol)
        rows: list[dict[str, Any]] = []
        for side, levels in (("BID", ob.bids[:5]), ("ASK", ob.asks[:5])):
            for lv in levels:
                rows.append(
                    {
                        "event_id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "observed_at": observed_at.isoformat(),
                        "side": side,
                        "price": lv.price,
                        "size": lv.size,
                        "first_seen_at": observed_at.isoformat(),
                        "last_seen_at": observed_at.isoformat(),
                        "persistence_seconds": 0,
                        "distance_from_entry_atr": None,
                        "source_quality": "snapshot_only",
                        "tracking_mode": "snapshot_only",
                        "research_only": True,
                    }
                )
        return rows
    except Exception:
        return [
            {
                "event_id": str(uuid.uuid4()),
                "symbol": symbol,
                "observed_at": observed_at.isoformat(),
                "side": "UNKNOWN",
                "price": price,
                "size": None,
                "source_quality": "unavailable",
                "tracking_mode": "snapshot_only",
                "research_only": True,
            }
        ]


def run_doge_shadow_once(
    *,
    end: datetime | None = None,
    out_dir: Path | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Single shadow pass over closed candles up to `end` (research-only)."""
    run_id = int(time.time())
    out = Path(out_dir or SHADOW_OUT_ROOT / str(run_id))
    log = V2ShadowEventLog(out)
    end_ts = end or datetime.utcnow().replace(second=0, microsecond=0)
    start_ts = end_ts - timedelta(hours=WARMUP_HOURS)
    ch = client or get_clickhouse_client()
    candles = build_candles_by_tf(DOGE_SYMBOL, start_ts, end_ts, client=ch)
    result = run_scanner(symbol=DOGE_SYMBOL, candles_by_tf=candles)

    now = end_ts
    for intent in result.get("signal_intents") or []:
        log.append("signal_intents", {**intent, "event_at": intent.get("armed_at"), "research_only": True})
    for ev in result.get("lifecycle_events") or []:
        log.append("lifecycle_events", ev)
    for row in result.get("pool_selection_audit") or []:
        log.append("pool_selection", {**row, "symbol": DOGE_SYMBOL, "research_only": True})
    for sig in result.get("confirmed") or []:
        log.append(
            "gate_audit",
            {
                "signal_id": sig.get("signal_id") or sig.get("setup_id"),
                "gates": sig.get("gates"),
                "reason_codes": sig.get("reason_codes"),
                "research_only": True,
            },
        )
        log.append(
            "data_quality",
            {
                "signal_id": sig.get("signal_id") or sig.get("setup_id"),
                "data_quality": sig.get("data_quality"),
                "research_only": True,
            },
        )
    for wall in _wall_snapshot(symbol=DOGE_SYMBOL, price=float(candles["1m"].iloc[-1]["close"]), observed_at=now):
        log.append("wall_context", wall)
    log.append(
        "heartbeat",
        {
            "event_id": str(uuid.uuid4()),
            "symbol": DOGE_SYMBOL,
            "event_at": now.isoformat(),
            "n_confirmed": result.get("n_confirmed"),
            "n_intents": len(result.get("signal_intents") or []),
            "research_only": True,
        },
    )

    manifest = {
        "run_id": run_id,
        "scanner_version": SCANNER_VERSION,
        "symbol": DOGE_SYMBOL,
        "shadow_start": start_ts.isoformat(),
        "shadow_end": end_ts.isoformat(),
        "research_only": True,
        "no_execution": True,
        "no_orders": True,
        "n_signal_intents": len(result.get("signal_intents") or []),
        "n_confirmed": result.get("n_confirmed"),
        "ladder_audit": result.get("ladder_audit"),
        "out_dir": str(out),
    }
    log.write_manifest(manifest)
    return {"manifest": manifest, "result": result, "out_dir": str(out)}
